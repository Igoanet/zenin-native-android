/**
 * GET /api/events — Server-Sent Events stream for live dashboard updates.
 *
 * Auth via ?token= query param because EventSource doesn't support headers.
 *
 * On connect: starts a Firebase watcher for every active panel owned by the user.
 * The watcher pushes events via sseEmit:
 *   { type: "device_update", panelId, device }  — status/battery/field change
 *   { type: "new_sms",       panelId, deviceId, messages }  — new SMS arrived
 *   { type: "login",         eventId }           — new login event (other routes)
 *   { type: "panel" }                            — panel config changed (other routes)
 */

import { Router } from "express";
import { verifyToken } from "../lib/auth.js";
import { db } from "@workspace/db";
import { users, panelConfigs } from "@workspace/db/schema";
import { eq, and } from "drizzle-orm";
import { sseSubscribe } from "../lib/event-bus.js";
import { subscribePanelWatcher, unsubscribePanelWatcher } from "../lib/device-watcher.js";
import { decrypt } from "../lib/crypto.js";

const router = Router();
const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

router.get("/events", async (req, res) => {
  const rawToken = req.query["token"];
  const token = typeof rawToken === "string" ? rawToken : null;

  if (!token) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }

  // Verify JWT
  const result = verifyToken(token, SESSION_SECRET);
  if (!result.ok) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }

  // Load user + their active panels from DB
  let userId: string | null = null;
  let panels: Array<{ id: string; firebaseUrl: string; firebaseSecret: string }> = [];

  try {
    const [user] = await db
      .select({ id: users.id, accessGranted: users.accessGranted })
      .from(users)
      .where(eq(users.id, result.payload.userId))
      .limit(1);

    if (!user || !user.accessGranted) {
      res.status(401).json({ error: "unauthorized" });
      return;
    }
    userId = user.id;

    panels = await db
      .select({
        id: panelConfigs.id,
        firebaseUrl: panelConfigs.firebaseUrl,
        firebaseSecret: panelConfigs.firebaseSecret,
      })
      .from(panelConfigs)
      .where(
        and(
          eq(panelConfigs.ownerId, userId),
          eq(panelConfigs.isActive, true),
        ),
      );
  } catch {
    res.status(500).json({ error: "internal" });
    return;
  }

  // Set SSE headers — MUST be set before any writes
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache, no-transform");
  res.setHeader("X-Accel-Buffering", "no"); // Disable nginx buffering
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();

  // Confirm connection
  res.write(": connected\n\n");

  // Register this response with the event bus (for device_update / new_sms / etc.)
  const unsub = sseSubscribe(userId, res);

  // Start Firebase watchers for every active panel.
  // Each watcher immediately pushes the current cached device state to this user.
  const decryptedPanels = panels.map((p) => {
    try {
      return { id: p.id, firebaseUrl: p.firebaseUrl, secret: decrypt(p.firebaseSecret, SESSION_SECRET) };
    } catch {
      return null;
    }
  }).filter((p): p is { id: string; firebaseUrl: string; secret: string } => p !== null);

  for (const panel of decryptedPanels) {
    subscribePanelWatcher(userId, panel.id, panel.firebaseUrl, panel.secret);
  }

  // Heartbeat every 25 s to survive proxy timeouts
  const heartbeat = setInterval(() => {
    try {
      res.write(": heartbeat\n\n");
    } catch {
      /* ignore — cleanup handled by close event */
    }
  }, 25_000);

  req.on("close", () => {
    clearInterval(heartbeat);
    unsub();
    for (const panel of decryptedPanels) {
      unsubscribePanelWatcher(userId!, panel.id);
    }
  });
});

export default router;
