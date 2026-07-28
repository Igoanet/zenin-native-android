import express, { type Express, type Request, type Response } from "express";
import cors from "cors";
import cookieParser from "cookie-parser";
import pinoHttp from "pino-http";
import rateLimit from "express-rate-limit";
import path from "path";
import { fileURLToPath } from "url";
import router from "./routes/index.js";
import { logger } from "./lib/logger.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app: Express = express();

// Trust the Replit proxy so express-rate-limit can read X-Forwarded-For safely.
app.set("trust proxy", 1);

// ─── Logging ──────────────────────────────────────────────────────────────

app.use(
  pinoHttp({
    logger,
    serializers: {
      req(req) {
        return { id: req.id, method: req.method, url: req.url?.split("?")[0] };
      },
      res(res) {
        return { statusCode: res.statusCode };
      },
    },
  }),
);

// ─── CORS ─────────────────────────────────────────────────────────────────

const allowedOrigins = (process.env.ALLOWED_ORIGINS ?? "*")
  .split(",")
  .map((s) => s.trim());

app.use(
  cors({
    origin: (origin, callback) => {
      if (allowedOrigins.includes("*") || !origin || allowedOrigins.includes(origin)) {
        callback(null, true);
      } else {
        callback(new Error("Not allowed by CORS"));
      }
    },
    credentials: true,
    methods: ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Authorization"],
  }),
);

// ─── Security Headers ─────────────────────────────────────────────────────

app.use((_req: Request, res: Response, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("X-XSS-Protection", "1; mode=block");
  res.setHeader("Referrer-Policy", "strict-origin-when-cross-origin");
  res.setHeader(
    "Content-Security-Policy",
    "default-src 'self'; connect-src 'self' wss: https://*.firebaseio.com https://*.googleapis.com; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:;",
  );
  next();
});

// ─── Body Parsing ─────────────────────────────────────────────────────────

app.use(express.json({ limit: "5mb" }));
app.use(express.urlencoded({ extended: true, limit: "5mb" }));
app.use(cookieParser());

// ─── Global Rate Limiter ──────────────────────────────────────────────────
// 300 requests per minute per IP (general API)

app.use(
  "/api",
  rateLimit({
    windowMs: 60 * 1000,
    max: 300,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: "Too many requests, please slow down." },
    skip: (req) => req.path === "/health",
  }),
);

// ─── Stricter Rate Limiter for Auth Endpoints ─────────────────────────────
// 10 login attempts per minute per IP

app.use(
  "/api/auth/login",
  rateLimit({
    windowMs: 60 * 1000,
    max: 10,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: "Too many login attempts, please try again later." },
  }),
);

app.use(
  "/api/auth/otp",
  rateLimit({
    windowMs: 60 * 1000,
    max: 10,
    standardHeaders: true,
    legacyHeaders: false,
    message: { error: "Too many OTP attempts, please try again later." },
  }),
);

// ─── Routes ───────────────────────────────────────────────────────────────

app.use("/api", router);

// ─── Frontend (ZENIN Dashboard) ───────────────────────────────────────────
// Built ZENIN web app served at /zenin — same origin as the API so all
// /api/... fetch calls resolve correctly without any proxy or CORS config.
{
  const publicDir = path.join(process.cwd(), "public");
  app.use("/zenin", express.static(publicDir, { index: false }));
  app.get(["/zenin", "/zenin/*splat"], (_req: Request, res: Response) => {
    res.sendFile(path.join(publicDir, "index.html"));
  });
}

// ─── Downloads (EXE / APK binaries) ─────────────────────────────────────
// process.cwd() = artifacts/api-server/ at runtime.
// Accessible at /api/downloads/<filename> — no source code, no auth needed.
{
  const downloadsDir = path.join(process.cwd(), "downloads");
  app.use("/api/downloads", express.static(downloadsDir, {
    setHeaders(res: import("http").ServerResponse, filePath: string) {
      res.setHeader("Content-Disposition", `attachment; filename="${path.basename(filePath)}"`);
      res.setHeader("Cache-Control", "no-store");
    },
  }));
}

// ─── 404 Handler ─────────────────────────────────────────────────────────

app.use((_req: Request, res: Response) => {
  res.status(404).json({ error: "Not found" });
});

// ─── Error Handler ────────────────────────────────────────────────────────

app.use((err: Error, _req: Request, res: Response, _next: unknown) => {
  logger.error(err);
  res.status(500).json({ error: "Internal server error" });
});

export default app;
