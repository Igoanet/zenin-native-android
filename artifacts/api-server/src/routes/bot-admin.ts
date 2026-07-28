// Owner / admin REST endpoints powering the dashboard "Bot Access" page.
// Mirrors the slash commands in bot3.ts.

import { Router, type IRouter } from "express";
import { z } from "zod";
import { eq, and, desc, getTableColumns } from "drizzle-orm";
import { randomUUID } from "node:crypto";
import {
  db,
  requiredChannels,
  accessKeys,
  users,
  roleEvents,
  ROLE_VALUES,
  type RoleValue,
} from "@workspace/db";
import { authenticate, type AuthedRequest } from "../middleware/authenticate.js";
import { genKeyCode, canCreateKey } from "../telegram-bot/keyAccess.js";
import { getChat } from "../telegram-bot/pyBridge.js";

const router: IRouter = Router();
router.use(authenticate);

function requireOwner(req: AuthedRequest): boolean {
  return req.user?.role === "owner";
}
function requireAdminish(req: AuthedRequest): boolean {
  const r = req.user?.role;
  return r === "owner" || r === "dev_admin" || r === "base_admin";
}

// ─── Required channels (owner only) ──────────────────────────────────────
router.get("/bot/channels", async (req: AuthedRequest, res) => {
  if (!requireAdminish(req)) {
    res.status(403).json({ error: "forbidden" });
    return;
  }
  const rows = await db
    .select()
    .from(requiredChannels)
    .orderBy(requiredChannels.createdAt);
  res.json({ channels: rows });
});

const addChannelSchema = z.object({
  chatRef: z.string().min(2).max(64), // @username or -100… id
  title: z.string().min(1).max(120),
});

router.post("/bot/channels", async (req: AuthedRequest, res) => {
  if (!requireOwner(req)) {
    res.status(403).json({ error: "owner_only" });
    return;
  }
  const parsed = addChannelSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "invalid_body" });
    return;
  }
  const raw = parsed.data.chatRef.trim();
  const chatRef: string | number = /^-?\d+$/.test(raw)
    ? Number(raw)
    : raw.startsWith("@")
      ? raw
      : `@${raw}`;
  let chat: {
    id: number;
    username: string | null;
    inviteLink: string | null;
  } | null;
  try {
    chat = await getChat(chatRef);
  } catch {
    res.status(503).json({ error: "bot_offline" });
    return;
  }
  if (!chat || typeof chat.id !== "number") {
    res.status(400).json({ error: "lookup_failed", detail: "no chat" });
    return;
  }
  const inviteLink =
    chat.inviteLink ?? (chat.username ? `https://t.me/${chat.username}` : null);
  await db
    .insert(requiredChannels)
    .values({
      chatId: chat.id,
      title: parsed.data.title,
      inviteLink,
      // Admin users coming through the panel must have a Telegram UID.
      addedByTgUid: req.user!.tgUid!,
    })
    .onConflictDoUpdate({
      target: requiredChannels.chatId,
      set: { title: parsed.data.title, inviteLink },
    });
  res.json({ chatId: chat.id, title: parsed.data.title, inviteLink });
});

router.delete("/bot/channels/:chatId", async (req: AuthedRequest, res) => {
  if (!requireOwner(req)) {
    res.status(403).json({ error: "owner_only" });
    return;
  }
  const chatIdRaw = req.params["chatId"];
  const id = Number(typeof chatIdRaw === "string" ? chatIdRaw : "");
  if (!Number.isFinite(id)) {
    res.status(400).json({ error: "bad_id" });
    return;
  }
  const r = await db
    .delete(requiredChannels)
    .where(eq(requiredChannels.chatId, id))
    .returning({ chatId: requiredChannels.chatId });
  if (r.length === 0) {
    res.status(404).json({ error: "not_found" });
    return;
  }
  res.json({ ok: true });
});

// ─── Access keys (owner + admins) ────────────────────────────────────────
// User-role keys: expiry is required, min 1 h, max 56 days (2 × 28-day months).
const MAX_USER_KEY_MS = 56 * 24 * 60 * 60 * 1000;
const MIN_USER_KEY_MS = 60 * 60 * 1000;

const adminRoles = ["user", "base_admin", "dev_admin"] as const;
const newKeySchema = z.object({
  role: z.enum(adminRoles),
  label: z.string().max(80).default(""),
  // durationSeconds: how long access lasts after the key is claimed.
  // 0 or null => unlimited (allowed for admin/dev-admin keys only).
  durationSeconds: z.number().int().min(0).nullable().default(null),
});

router.get("/bot/keys", async (req: AuthedRequest, res) => {
  if (!requireAdminish(req)) {
    res.status(403).json({ error: "forbidden" });
    return;
  }
  const r = req.user!;
  const selectShape = {
    ...getTableColumns(accessKeys),
    claimerName: users.name,
    claimerUsername: users.tgUsername,
    claimerUserId: users.userId,
  };
  const rows =
    r.role === "owner"
      ? await db
          .select(selectShape)
          .from(accessKeys)
          .leftJoin(users, eq(accessKeys.redeemedByTgUid, users.tgUid))
          .orderBy(desc(accessKeys.createdAt))
          .limit(200)
      : await db
          .select(selectShape)
          .from(accessKeys)
          .leftJoin(users, eq(accessKeys.redeemedByTgUid, users.tgUid))
          .where(eq(accessKeys.createdByTgUid, r.tgUid ?? 0))
          .orderBy(desc(accessKeys.createdAt))
          .limit(200);
  res.json({ keys: rows });
});

router.post("/bot/keys", async (req: AuthedRequest, res) => {
  if (!requireAdminish(req)) {
    res.status(403).json({ error: "forbidden" });
    return;
  }
  const parsed = newKeySchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "invalid_body" });
    return;
  }
  const role = parsed.data.role as RoleValue;
  if (!ROLE_VALUES.includes(role)) {
    res.status(400).json({ error: "bad_role" });
    return;
  }
  if (!canCreateKey(req.user!.role as RoleValue, role)) {
    res.status(403).json({ error: "role_not_allowed" });
    return;
  }
  // User-role keys must have a duration between 1 h and 56 days.
  // Admin/dev-admin keys are always unlimited (durationSeconds ignored → 0).
  const isUserRole = role === "user";
  const durationSeconds = isUserRole ? (parsed.data.durationSeconds ?? 0) : 0;
  if (isUserRole) {
    if (!durationSeconds || durationSeconds <= 0) {
      res.status(400).json({ error: "duration_required_for_user_keys" });
      return;
    }
    const ttlMs = durationSeconds * 1000;
    if (ttlMs < MIN_USER_KEY_MS) {
      res.status(400).json({ error: "duration_too_short", min: "1 hour" });
      return;
    }
    if (ttlMs > MAX_USER_KEY_MS) {
      res.status(400).json({ error: "duration_too_long", max: "2 months (56 days)" });
      return;
    }
  }
  const label = (parsed.data.label ?? "").trim();
  const code = genKeyCode();
  const id = randomUUID();
  await db.insert(accessKeys).values({
    id,
    code,
    role: role as Exclude<RoleValue, "management">,
    label,
    durationSeconds,
    expiresAt: null,
    createdByTgUid: req.user!.tgUid,
    createdByRole: req.user!.role,
  });
  res.json({ id, code, role, label, durationSeconds });
});

router.post("/bot/keys/:id/revoke", async (req: AuthedRequest, res) => {
  if (!requireAdminish(req)) {
    res.status(403).json({ error: "forbidden" });
    return;
  }
  const idRaw = req.params["id"];
  const id = typeof idRaw === "string" ? idRaw : "";
  const u = req.user!;
  const r =
    u.role === "owner"
      ? await db
          .update(accessKeys)
          .set({ revoked: true })
          .where(eq(accessKeys.id, id))
          .returning({ id: accessKeys.id })
      : await db
          .update(accessKeys)
          .set({ revoked: true })
          .where(
            and(
              eq(accessKeys.id, id),
              eq(accessKeys.createdByTgUid, u.tgUid ?? 0),
            ),
          )
          .returning({ id: accessKeys.id });
  if (r.length === 0) {
    res.status(404).json({ error: "not_found" });
    return;
  }
  res.json({ ok: true });
});

// ─── Role events feed (owner + admins) ───────────────────────────────────
// Append-only audit log of every role change (key redemption, bot
// /promote, future website promotion). Surfaced in the dashboard Activity
// tab so promotions done from the bot side are visible to panel admins.
router.get("/bot/role-events", async (req: AuthedRequest, res) => {
  if (!requireAdminish(req)) {
    res.status(403).json({ error: "forbidden" });
    return;
  }
  const rows = await db
    .select()
    .from(roleEvents)
    .orderBy(desc(roleEvents.ts))
    .limit(200);
  res.json({ events: rows });
});

export default router;
