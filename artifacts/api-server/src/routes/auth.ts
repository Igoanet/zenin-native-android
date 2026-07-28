/**
 * Authentication routes
 *
 * POST /api/auth/login          — step 1: verify credentials, send OTP
 * POST /api/auth/otp/verify     — step 2: verify OTP, issue session token
 * GET  /api/auth/me             — get current user info
 * GET  /api/auth/sessions       — list active sessions
 * POST /api/auth/logout         — terminate current session
 * DELETE /api/auth/sessions/:id — evict a specific session
 * POST /api/auth/change-password
 */

import { Router } from "express";
import { db } from "@workspace/db";
import { users, loginEvents, otpSessions } from "@workspace/db/schema";
import { eq, and, isNull, desc, inArray, or } from "drizzle-orm";
import {
  verifyPassword,
  hashPassword,
  needsRehash,
  signToken,
  signPreAuthToken,
  verifyPreAuthToken,
  generateOtp,
  generateId,
  generateNumericId,
} from "../lib/auth.js";
import { sendOtp } from "../lib/telegram.js";
import { authenticate } from "../middleware/authenticate.js";
import { accountSend } from "../telegram-bot/pyBridge.js";
import type { Request, Response } from "express";

const router = Router();

const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";
const OTP_TTL_MS = 5 * 60 * 1000; // 5 minutes
const SKIP_OTP = process.env.SKIP_OTP === "true";
const MAX_ACTIVE_SESSIONS = 2;
const MAX_OTP_ATTEMPTS = 5;

// ─── Helper: format user for the original frontend auth contract ─────────

function panelUser(user: typeof users.$inferSelect) {
  return {
    userId: user.userId,
    name: user.name,
    role: user.role,
    // Prefer the dedicated tgUid bigint column (populated by bot onboarding);
    // fall back to the legacy tg_chat_id text column for older accounts.
    tgUid: user.tgUid ?? (parseInt(user.tgChatId || "0", 10) || 0),
  };
}

/** Resolve the Telegram chat_id to use for OTP delivery.
 *
 * Priority: tgUid (bigint) → tgChatId (legacy text)
 *
 * tgUid is set by the Python bot when the user actually interacts with it
 * and is always correct. tgChatId is a legacy text column that may have been
 * populated with wrong values (e.g. the panel user_id instead of the real
 * Telegram chat_id). Always prefer tgUid when available.
 */
function resolveOtpChatId(user: typeof users.$inferSelect): string | null {
  if (user.tgUid)             return String(user.tgUid);
  if (user.tgChatId?.trim()) return user.tgChatId.trim();
  return null;
}

// ─── Helper: send post-login DM to user's Telegram ──────────────────────

function sendLoginSuccessNotification(params: {
  tgChatId: number;
  userId: string;
  name: string;
  ip: string;
  geo: { city: string; region: string; country: string };
}): void {
  const { tgChatId, userId, name, ip, geo } = params;
  if (!tgChatId) return;

  const locationParts = [geo.city, geo.region, geo.country].filter(Boolean);
  const location = locationParts.length ? locationParts.join(", ") : "Unknown";

  const now = new Date();
  const timeStr = now.toUTCString().replace("GMT", "UTC");

  const text =
    `✅ <b>New Panel Login</b>\n` +
    `<code>━━━━━━━━━━━━━━━━━━━</code>\n\n` +
    `👤 <b>User:</b> <code>${userId}</code>  (${name})\n` +
    `🌐 <b>IP Address:</b> <code>${ip || "Unknown"}</code>\n` +
    `📍 <b>Location:</b> ${location}\n` +
    `🕐 <b>Time:</b> ${timeStr}\n\n` +
    `<code>━━━━━━━━━━━━━━━━━━━</code>\n` +
    `<i>If this wasn't you, change your password immediately.</i>`;

  // Fire-and-forget — login latency must not be affected
  accountSend(tgChatId, text).catch((err) => {
    console.error("[login-notify] failed to send post-login DM:", err);
  });
}

// ─── Helper: get geo info ────────────────────────────────────────────────

async function getGeo(ip: string): Promise<{ city: string; region: string; country: string }> {
  if (!ip || ip === "127.0.0.1" || ip === "::1") {
    return { city: "Local", region: "", country: "" };
  }
  try {
    const res = await fetch(`http://ip-api.com/json/${ip}?fields=city,regionName,country`, {
      signal: (() => { const c = new AbortController(); setTimeout(() => c.abort(), 3000); return c.signal; })(),
    });
    if (!res.ok) return { city: "", region: "", country: "" };
    const j = await res.json() as { city?: string; regionName?: string; country?: string };
    return { city: j.city ?? "", region: j.regionName ?? "", country: j.country ?? "" };
  } catch {
    return { city: "", region: "", country: "" };
  }
}

// ─── POST /api/auth/login ────────────────────────────────────────────────

router.post("/login", async (req: Request, res: Response) => {
  const { username, password } = req.body as {
    username?: string;
    password?: string;
  };

  if (!username || !password) {
    res.status(400).json({ error: "username and password are required" });
    return;
  }

  if (username.length > 50) {
    res.status(400).json({ error: "username must be 50 characters or less" });
    return;
  }

  // Always use generic error to prevent user enumeration
  const genericError = "Invalid credentials";

  try {
    // Accept either the web `username` field OR the bot-issued panel `userId`
    // (bot-created accounts have username = null but always have a userId).
    const trimmed = username.trim();
    const [user] = await db
      .select()
      .from(users)
      .where(or(eq(users.username, trimmed), eq(users.userId, trimmed)))
      .limit(1);

    if (!user) {
      // Perform a dummy hash to equalize timing
      await verifyPassword(password, "a".repeat(128), "a".repeat(64));
      res.status(401).json({ error: genericError });
      return;
    }

    const valid = await verifyPassword(password, user.passwordHash, user.passwordSalt);
    if (!valid) {
      res.status(401).json({ error: genericError });
      return;
    }

    // Transparent Argon2id rehash — upgrade legacy scrypt hashes silently.
    // Fire-and-forget: runs in the background so login latency is unaffected.
    if (needsRehash(user.passwordHash)) {
      hashPassword(password)
        .then(({ hash, salt }) =>
          db
            .update(users)
            .set({ passwordHash: hash, passwordSalt: salt, updatedAt: new Date() })
            .where(eq(users.id, user.id)),
        )
        .catch((err) => {
          console.error("argon2id rehash failed (non-fatal):", err);
        });
    }

    if (!user.accessGranted) {
      res.status(403).json({ error: "management_no_panel" });
      return;
    }

    if (user.accessExpiresAt && new Date() > user.accessExpiresAt) {
      res.status(403).json({ error: "Account access has expired" });
      return;
    }

    // Resolve the chat_id to send the OTP to.
    // tgUid (bot-onboarded) or tgChatId (panel-created) both work — same value.
    const otpChatId = resolveOtpChatId(user);

    // OTP is always required on every login.
    // If the user has no Telegram identity we cannot deliver the code — block.
    if (!otpChatId) {
      if (SKIP_OTP) {
        // Dev-mode only: no Telegram, OTP globally disabled — issue token directly.
        const sessionId = generateNumericId();
        const ip = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim()
          ?? req.socket.remoteAddress ?? "";
        const geo = await getGeo(ip);
        await db.insert(loginEvents).values({
          id: sessionId, userId: user.id, ipAddress: ip,
          userAgent: req.headers["user-agent"] ?? "",
          city: geo.city, region: geo.region, country: geo.country,
        });
        const token = signToken(user.id, user.tokenVersion, SESSION_SECRET);
        sendLoginSuccessNotification({
          tgChatId: Number((user.tgUid ?? parseInt(user.tgChatId || "0", 10)) || 0),
          userId: user.userId ?? user.id,
          name: user.name ?? "",
          ip,
          geo,
        });
        res.json({ token, user: panelUser(user), loginEventId: Number(sessionId) });
        return;
      }
      res.status(403).json({
        error: "otp_no_telegram",
        detail: "Account has no Telegram linked. Contact your administrator.",
      });
      return;
    }

    // Dev-mode only: Telegram present but OTP globally disabled — issue token.
    if (SKIP_OTP) {
      const sessionId = generateNumericId();
      const ip = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim()
        ?? req.socket.remoteAddress ?? "";
      const geo = await getGeo(ip);
      await db.insert(loginEvents).values({
        id: sessionId, userId: user.id, ipAddress: ip,
        userAgent: req.headers["user-agent"] ?? "",
        city: geo.city, region: geo.region, country: geo.country,
      });
      const token = signToken(user.id, user.tokenVersion, SESSION_SECRET);
      sendLoginSuccessNotification({
        tgChatId: Number((user.tgUid ?? parseInt(user.tgChatId || "0", 10)) || 0),
        userId: user.userId ?? user.id,
        name: user.name ?? "",
        ip,
        geo,
      });
      res.json({ token, user: panelUser(user), loginEventId: Number(sessionId) });
      return;
    }

    // ── Send OTP via Telegram ────────────────────────────────────────────────
    const otp = generateOtp();
    const otpId = generateId();

    await db.insert(otpSessions).values({
      id: otpId,
      userId: user.id,
      code: otp,
      expiresAt: new Date(Date.now() + OTP_TTL_MS),
    });

    try {
      await sendOtp(otpChatId, otp);
    } catch (err) {
      const detail = (err as Error).message ?? "";
      console.error(`[OTP] Failed to send to ${otpChatId}:`, err);
      // "chat not found" (400) = user has never sent /start to the bot.
      // "bot was blocked" (403) = user blocked the bot.
      const isBotNotStarted =
        detail.includes("chat not found") || detail.includes("bot was blocked");
      res.status(500).json({
        error: isBotNotStarted ? "otp_bot_not_started" : "otp_send_failed",
        detail,
      });
      return;
    }

    res.json({ otpPending: true, otpId });
  } catch (err) {
    console.error("login error:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// ─── POST /api/auth/otp/verify ────────────────────────────────────────────

router.post("/otp/verify", async (req: Request, res: Response) => {
  const { otpId, otp } = req.body as { otpId?: string; otp?: string };

  if (!otpId || !otp) {
    res.status(400).json({ error: "otpId and otp are required" });
    return;
  }

  try {
    const [otpSession] = await db
      .select()
      .from(otpSessions)
      .where(eq(otpSessions.id, otpId))
      .limit(1);

    if (!otpSession || otpSession.used || new Date() > otpSession.expiresAt) {
      res.status(401).json({ error: "otp_expired" });
      return;
    }

    const attempts = otpSession.attempts + 1;
    await db
      .update(otpSessions)
      .set({ attempts })
      .where(eq(otpSessions.id, otpId));

    if (attempts > MAX_OTP_ATTEMPTS) {
      res.status(401).json({ error: "otp_too_many_attempts" });
      return;
    }

    if (otpSession.code !== otp.trim()) {
      res.status(401).json({
        error: "otp_invalid",
        attemptsLeft: Math.max(0, MAX_OTP_ATTEMPTS - attempts),
      });
      return;
    }

    // Mark OTP as used
    await db
      .update(otpSessions)
      .set({ used: true })
      .where(eq(otpSessions.id, otpId));

    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, otpSession.userId))
      .limit(1);

    if (!user) {
      res.status(401).json({ error: "User not found" });
      return;
    }

    // Re-check capacity at verify time (sessions may have been created since OTP was sent)
    const activeSessions = await db
      .select()
      .from(loginEvents)
      .where(
        and(
          eq(loginEvents.userId, user.id),
          isNull(loginEvents.terminatedAt),
        ),
      )
      .orderBy(desc(loginEvents.createdAt));

    if (activeSessions.length >= MAX_ACTIVE_SESSIONS) {
      const preAuthId = signPreAuthToken(user.id, SESSION_SECRET);
      res.status(409).json({
        error: "login_capacity_full",
        activeSessions: activeSessions.map((s) => ({
          id: Number(s.id),
          ip: s.ipAddress,
          userAgent: s.userAgent,
          city: s.city,
          region: s.region,
          country: s.country,
          occurredAt: s.createdAt,
        })),
        preAuthId,
      });
      return;
    }

    const sessionId = generateNumericId();
    const ip = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim()
      ?? req.socket.remoteAddress
      ?? "";
    const geo = await getGeo(ip);

    await db.insert(loginEvents).values({
      id: sessionId,
      userId: user.id,
      ipAddress: ip,
      userAgent: req.headers["user-agent"] ?? "",
      city: geo.city,
      region: geo.region,
      country: geo.country,
    });

    const token = signToken(user.id, user.tokenVersion, SESSION_SECRET);

    // Fire-and-forget post-login notification (IP + geo) to user's Telegram DM
    sendLoginSuccessNotification({
      tgChatId: Number((user.tgUid ?? parseInt(user.tgChatId || "0", 10)) || 0),
      userId: user.userId ?? user.id,
      name: user.name ?? "",
      ip,
      geo,
    });

    res.json({
      token,
      user: panelUser(user),
      loginEventId: Number(sessionId),
    });
  } catch (err) {
    console.error("OTP verify error:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// ─── POST /api/auth/login/evict-and-login ─────────────────────────────────

router.post("/login/evict-and-login", async (req: Request, res: Response) => {
  const { preAuthId, evictEventId } = req.body as {
    preAuthId?: string;
    evictEventId?: number;
  };

  if (!preAuthId || !evictEventId) {
    res.status(400).json({ error: "preAuthId and evictEventId are required" });
    return;
  }

  const preAuth = verifyPreAuthToken(preAuthId, SESSION_SECRET);
  if (!preAuth) {
    res.status(401).json({ error: "Pre-auth token invalid or expired" });
    return;
  }

  try {
    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, preAuth.userId))
      .limit(1);

    if (!user) {
      res.status(401).json({ error: "Invalid credentials" });
      return;
    }

    // Evict the chosen session (must belong to this user)
    const evictResult = await db
      .update(loginEvents)
      .set({ terminatedAt: new Date() })
      .where(
        and(
          eq(loginEvents.id, String(evictEventId)),
          eq(loginEvents.userId, user.id),
          isNull(loginEvents.terminatedAt),
        ),
      )
      .returning({ id: loginEvents.id });

    if (evictResult.length === 0) {
      res.status(400).json({ error: "Session not found or already terminated" });
      return;
    }

    // OTP was already verified before the user reached the evict screen —
    // create the new session directly without sending another code.
    const sessionId = generateNumericId();
    const ip = (req.headers["x-forwarded-for"] as string)?.split(",")[0]?.trim()
      ?? req.socket.remoteAddress ?? "";
    const geo = await getGeo(ip);
    await db.insert(loginEvents).values({
      id: sessionId, userId: user.id, ipAddress: ip,
      userAgent: req.headers["user-agent"] ?? "",
      city: geo.city, region: geo.region, country: geo.country,
    });
    const token = signToken(user.id, user.tokenVersion, SESSION_SECRET);
    sendLoginSuccessNotification({
      tgChatId: Number((user.tgUid ?? parseInt(user.tgChatId || "0", 10)) || 0),
      userId: user.userId ?? user.id,
      name: user.name ?? "",
      ip,
      geo,
    });
    res.json({ token, user: panelUser(user), loginEventId: Number(sessionId) });
  } catch (err) {
    console.error("evict-and-login error:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// ─── GET /api/auth/me ─────────────────────────────────────────────────────

router.get("/me", authenticate, async (req: Request, res: Response) => {
  const user = req.user!;
  const [full] = await db
    .select()
    .from(users)
    .where(eq(users.id, user.id))
    .limit(1);

  if (!full) {
    res.status(404).json({ error: "User not found" });
    return;
  }

  res.json({ user: panelUser(full) });
});

// ─── PATCH /api/auth/me ───────────────────────────────────────────────────

router.patch("/me", authenticate, async (req: Request, res: Response) => {
  const { name } = req.body as { name?: string };

  if (!name || !name.trim()) {
    res.status(400).json({ error: "name is required" });
    return;
  }

  const [full] = await db
    .update(users)
    .set({ name: name.trim(), updatedAt: new Date() })
    .where(eq(users.id, req.user!.id))
    .returning();

  if (!full) {
    res.status(404).json({ error: "User not found" });
    return;
  }

  res.json({ user: panelUser(full) });
});

// ─── GET /api/auth/sessions ───────────────────────────────────────────────

router.get("/sessions", authenticate, async (req: Request, res: Response) => {
  const rows = await db
    .select()
    .from(loginEvents)
    .where(
      and(
        eq(loginEvents.userId, req.user!.id),
        isNull(loginEvents.terminatedAt),
      ),
    )
    .orderBy(desc(loginEvents.createdAt))
    .limit(10);

  const sessions = rows.map((s) => ({
    id: Number(s.id),
    ip: s.ipAddress,
    userAgent: s.userAgent,
    city: s.city,
    region: s.region,
    country: s.country,
    occurredAt: s.createdAt,
  }));

  res.json({ sessions });
});

// ─── POST /api/auth/logout ────────────────────────────────────────────────

router.post("/logout", authenticate, async (req: Request, res: Response) => {
  // Terminate all active sessions for this user (simple approach)
  await db
    .update(loginEvents)
    .set({ terminatedAt: new Date() })
    .where(
      and(
        eq(loginEvents.userId, req.user!.id),
        isNull(loginEvents.terminatedAt),
      ),
    );

  res.json({ ok: true });
});

// ─── DELETE /api/auth/sessions/:id ───────────────────────────────────────

router.delete("/sessions/:id", authenticate, async (req: Request, res: Response) => {
  const id = String(req.params.id);
  await db
    .update(loginEvents)
    .set({ terminatedAt: new Date() })
    .where(
      and(
        eq(loginEvents.id, id),
        eq(loginEvents.userId, req.user!.id),
      ),
    );

  res.json({ ok: true });
});

// ─── POST /api/auth/sessions/terminate ────────────────────────────────────

router.post("/sessions/terminate", authenticate, async (req: Request, res: Response) => {
  const { ids } = req.body as { ids?: number[] };

  if (!Array.isArray(ids) || ids.length === 0) {
    res.status(400).json({ error: "ids array is required" });
    return;
  }

  const idStrings = ids.map((id) => String(id));

  await db
    .update(loginEvents)
    .set({ terminatedAt: new Date() })
    .where(
      and(
        eq(loginEvents.userId, req.user!.id),
        isNull(loginEvents.terminatedAt),
        inArray(loginEvents.id, idStrings),
      ),
    );

  res.json({ ok: true });
});

// ─── POST /api/auth/change-password ──────────────────────────────────────

router.post("/change-password", authenticate, async (req: Request, res: Response) => {
  const { currentPassword, newPassword } = req.body as {
    currentPassword?: string;
    newPassword?: string;
  };

  if (!currentPassword || !newPassword) {
    res.status(400).json({ error: "currentPassword and newPassword are required" });
    return;
  }

  if (newPassword.length < 8) {
    res.status(400).json({ error: "New password must be at least 8 characters" });
    return;
  }

  try {
    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, req.user!.id))
      .limit(1);

    if (!user) {
      res.status(404).json({ error: "User not found" });
      return;
    }

    const valid = await verifyPassword(currentPassword, user.passwordHash, user.passwordSalt);
    if (!valid) {
      res.status(401).json({ error: "Current password is incorrect" });
      return;
    }

    const { hash, salt } = await hashPassword(newPassword);

    await db
      .update(users)
      .set({
        passwordHash: hash,
        passwordSalt: salt,
        tokenVersion: user.tokenVersion + 1, // Invalidates all existing tokens
        updatedAt: new Date(),
      })
      .where(eq(users.id, user.id));

    res.json({ ok: true, message: "Password changed. Please log in again." });
  } catch (err) {
    console.error("change-password error:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

export default router;
