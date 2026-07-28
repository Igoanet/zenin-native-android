/**
 * Safe stub routes for secondary features from the original Zenin frontend.
 * These endpoints exist so the unchanged UI does not crash, but they return
 * minimal/empty data until the feature is fully implemented.
 */

import { Router } from "express";
import { authenticate } from "../middleware/authenticate.js";
import { db } from "@workspace/db";
import { loginEvents, panelConfigs } from "@workspace/db/schema";
import { eq, and, isNotNull, desc } from "drizzle-orm";
import type { Request, Response } from "express";

const router = Router();

// ─── Auth credentials (used by the original "credentials" page) ───────────

router.get("/auth/credentials", authenticate, async (req: Request, res: Response) => {
  res.json({
    userId: req.user!.userId,
    password: "",
    token: undefined,
  });
});

router.get("/auth/login-history", authenticate, async (req: Request, res: Response) => {
  const events = await db
    .select()
    .from(loginEvents)
    .where(eq(loginEvents.userId, req.user!.id))
    .orderBy(desc(loginEvents.createdAt))
    .limit(50);

  res.json({
    events: events.map((e) => ({
      id: e.id,
      ip: e.ipAddress,
      userAgent: e.userAgent,
      city: e.city,
      region: e.region,
      country: e.country,
      occurredAt: e.createdAt,
      terminatedAt: e.terminatedAt,
    })),
  });
});

router.post("/auth/login-history/delete", authenticate, async (req: Request, res: Response) => {
  const { ids } = req.body as { ids?: number[] };
  if (Array.isArray(ids) && ids.length > 0) {
    await db
      .delete(loginEvents)
      .where(
        and(
          eq(loginEvents.userId, req.user!.id),
          isNotNull(loginEvents.terminatedAt),
        ),
      );
  }
  res.json({ ok: true });
});

// ─── SMS messages (bulk loader) ──────────────────────────────────────────

router.get("/sms/messages", authenticate, (_req: Request, res: Response) => {
  res.json({ messages: [] });
});

// ─── Device sync trigger ──────────────────────────────────────────────────

router.post("/devices/sync", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true, queued: true });
});

router.post("/devices/notes", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true });
});

router.post("/devices/purge-account", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true, purged: true });
});

// ─── Panel forwarding configuration ───────────────────────────────────────

router.post("/panel/forward", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true });
});

router.post("/panel/forward-edit", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true });
});

// NOTE: /notify-settings and /notify-channels are handled by their own real
// routers (notify-settings.ts, notify-channels.ts), registered before this
// stub router in routes/index.ts. Do NOT add stubs for them here.

// ─── Telegram bot management ──────────────────────────────────────────────

router.get("/bot/channels", authenticate, (_req: Request, res: Response) => {
  res.json({ channels: [] });
});

router.delete("/bot/channels/:id", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true });
});

router.get("/bot/keys", authenticate, (_req: Request, res: Response) => {
  res.json({ keys: [] });
});

router.post("/bot/keys/:id/revoke", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true });
});

router.get("/bot/role-events", authenticate, (_req: Request, res: Response) => {
  res.json({ events: [] });
});

// ─── Auto verify / invite forwarding ──────────────────────────────────────

router.get("/auto-verify/users", authenticate, (_req: Request, res: Response) => {
  res.json({ users: [] });
});

router.post("/auto-verify/notify-forward", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true });
});

// ─── Share / permission checks ────────────────────────────────────────────

router.post("/share/check-permission", authenticate, (_req: Request, res: Response) => {
  res.json({ allowed: true });
});

router.get("/share/view/:token", (_req: Request, res: Response) => {
  res.status(403).json({ error: "Shared views are not enabled in this build" });
});

// ─── AI helpers ───────────────────────────────────────────────────────────

router.post("/ai/chat", authenticate, (req: Request, res: Response) => {
  res.setHeader("content-type", "text/event-stream");
  res.setHeader("cache-control", "no-cache");
  res.setHeader("connection", "keep-alive");
  res.write(`data: ${JSON.stringify({ chunk: "AI assistant is not configured yet.", done: false })}\n\n`);
  res.write(`data: ${JSON.stringify({ done: true })}\n\n`);
  res.end();
});

router.post("/ai/extract-codes", authenticate, (_req: Request, res: Response) => {
  res.json({ giftCards: [], walletRefunds: [], redeemCodes: [] });
});

// ─── Number lookup ────────────────────────────────────────────────────────

router.get("/lookup/num", authenticate, (req: Request, res: Response) => {
  const q = req.query.q as string;
  res.json({
    query: q ?? "",
    name: "",
    carrier: "",
    location: "",
    spamRisk: 0,
  });
});

router.get("/lookup/ping", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true });
});

// ─── Storage wipe / report / support ────────────────────────────────────

router.delete("/storage/clear-all", authenticate, async (req: Request, res: Response) => {
  await db.delete(panelConfigs).where(eq(panelConfigs.ownerId, req.user!.id));
  res.json({ ok: true, cleared: true });
});

router.post("/report", authenticate, (_req: Request, res: Response) => {
  res.json({ ok: true, ticket: "" });
});

router.get("/support-info", (_req: Request, res: Response) => {
  res.json({
    text: "For support, contact @ZeninPortalBot on Telegram.",
    url: "https://t.me/ZeninPortalBot",
  });
});

export default router;
