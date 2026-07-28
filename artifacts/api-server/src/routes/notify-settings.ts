// Per-device notification preferences for the signed-in user. These used to
// live only in the browser's localStorage; persisting them here lets the
// choices follow the user across browsers, devices, and storage clears.

import { Router, type IRouter } from "express";
import { z } from "zod";
import { db, notifySettings } from "@workspace/db";
import { and, eq, notInArray, notLike } from "drizzle-orm";
import { authenticate, type AuthedRequest } from "../middleware/authenticate.js";

const router: IRouter = Router();
// Scope auth to this router's own path. This router is mounted without a path
// prefix in routes/index.ts, so a path-less `router.use(authenticate)` would
// run on EVERY /api request that reaches it (401-ing public routes like
// /api/support-info and /api/downloads). Scoping to "/notify-settings" fixes it.
router.use("/notify-settings", authenticate);

const SettingsBody = z.object({
  transaction: z.boolean(),
  login: z.boolean(),
  onlineOffline: z.boolean(),
  // ms timestamp when notifications were first enabled; null clears the cutoff.
  enabledAt: z.number().int().positive().nullable().optional(),
});

const DeviceIdParam = z.string().trim().min(1).max(256);

// Returns every saved device's preferences for the caller as a map keyed by
// device id, e.g. { "<deviceId>": { transaction, login, onlineOffline } }.
router.get("/notify-settings", async (req: AuthedRequest, res) => {
  if (!req.user) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  const rows = await db
    .select()
    .from(notifySettings)
    .where(eq(notifySettings.tgUid, req.user.tgUid!));
  const settings: Record<
    string,
    { transaction: boolean; login: boolean; onlineOffline: boolean; enabledAt?: number | null }
  > = {};
  for (const r of rows) {
    settings[r.deviceId] = {
      transaction: r.transaction,
      login: r.login,
      onlineOffline: r.onlineOffline,
      enabledAt: r.enabledAt ?? null,
    };
  }
  res.json({ settings });
});

// Upserts the preferences for a single device belonging to the caller.
router.put("/notify-settings/:deviceId", async (req: AuthedRequest, res) => {
  if (!req.user) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  const idParsed = DeviceIdParam.safeParse(req.params.deviceId);
  if (!idParsed.success) {
    res.status(400).json({ error: "invalid_device_id" });
    return;
  }
  const parsed = SettingsBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "invalid_body" });
    return;
  }
  const { transaction, login, onlineOffline, enabledAt } = parsed.data;
  await db
    .insert(notifySettings)
    .values({
      tgUid: req.user.tgUid!,
      deviceId: idParsed.data,
      transaction,
      login,
      onlineOffline,
      enabledAt: enabledAt ?? null,
      updatedAt: new Date(),
    })
    .onConflictDoUpdate({
      target: [notifySettings.tgUid, notifySettings.deviceId],
      set: { transaction, login, onlineOffline, enabledAt: enabledAt ?? null, updatedAt: new Date() },
    });
  res.json({ ok: true });
});

// Deletes the saved preferences for a single device belonging to the caller.
// Called when the user removes/disconnects that device so its row does not
// linger as an orphan (and cannot resurface if the device id is reused).
router.delete("/notify-settings/:deviceId", async (req: AuthedRequest, res) => {
  if (!req.user) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  const idParsed = DeviceIdParam.safeParse(req.params.deviceId);
  if (!idParsed.success) {
    res.status(400).json({ error: "invalid_device_id" });
    return;
  }
  await db
    .delete(notifySettings)
    .where(
      and(
        eq(notifySettings.tgUid, req.user.tgUid!),
        eq(notifySettings.deviceId, idParsed.data),
      ),
    );
  res.json({ ok: true });
});

const PruneBody = z.object({
  keep: z.array(DeviceIdParam),
});

// Maintenance pass: deletes preferences for every device the caller no longer
// has. The client sends the list of device ids it still knows about (`keep`);
// any stored row whose device id is not in that list is pruned. An empty list
// clears all of the caller's saved preferences.
router.post("/notify-settings/prune", async (req: AuthedRequest, res) => {
  if (!req.user) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  const parsed = PruneBody.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "invalid_body" });
    return;
  }
  const keep = parsed.data.keep;
  const tgUid = req.user.tgUid!;
  // The keep list only ever contains device ids, so this prune must never
  // touch folder-scoped rows (id prefixed "folder:"); those are managed
  // independently from the folder cards and would otherwise be wiped.
  if (keep.length === 0) {
    await db
      .delete(notifySettings)
      .where(
        and(
          eq(notifySettings.tgUid, tgUid),
          notLike(notifySettings.deviceId, "folder:%"),
        ),
      );
  } else {
    await db
      .delete(notifySettings)
      .where(
        and(
          eq(notifySettings.tgUid, tgUid),
          notInArray(notifySettings.deviceId, keep),
          notLike(notifySettings.deviceId, "folder:%"),
        ),
      );
  }
  res.json({ ok: true });
});

export default router;
