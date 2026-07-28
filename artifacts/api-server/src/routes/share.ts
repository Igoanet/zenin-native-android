/**
 * Share Routes
 *
 * POST /api/share/generate-link — server-side share-token generation (used by mobile)
 */
import { Router } from "express";
import { db } from "@workspace/db";
import { panelConfigs } from "@workspace/db/schema";
import { and, eq } from "drizzle-orm";
import { decrypt } from "../lib/crypto.js";
import { authenticate } from "../middleware/authenticate.js";

const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

// ── Same AES-GCM constants as client-side share-link.ts ──────────────────────
// IMPORTANT: if you change PASS/SALT here you MUST change them in share-link.ts too.
const SHARE_PASS = "SCARY_PANEL_SHARE_v1_!!AES256!!";
const SHARE_SALT = new TextEncoder().encode("scary_panel_salt_2025");

let _shareKey: CryptoKey | null = null;
async function getShareKey(): Promise<CryptoKey> {
  if (_shareKey) return _shareKey;
  // Node 18+ exposes globalThis.crypto.subtle (Web Crypto API)
  const raw = await globalThis.crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(SHARE_PASS),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  _shareKey = await globalThis.crypto.subtle.deriveKey(
    { name: "PBKDF2", salt: SHARE_SALT, iterations: 100_000, hash: "SHA-256" },
    raw,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt"],
  );
  return _shareKey;
}

async function encryptShareToken(payload: object): Promise<string> {
  const key = await getShareKey();
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const data = new TextEncoder().encode(JSON.stringify(payload));
  const ct = await globalThis.crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, data);
  const out = new Uint8Array(iv.byteLength + ct.byteLength);
  out.set(iv, 0);
  out.set(new Uint8Array(ct), iv.byteLength);
  // base64url (no padding) — matches client-side toBase64url()
  return Buffer.from(out)
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=/g, "");
}

function newLinkId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

const shareRouter = Router();

/**
 * POST /api/share/generate-link
 * Body: { panelId: string; deviceId: string; deviceName?: string }
 * Returns: { token: string }
 *   Mobile constructs the full URL: https://<domain>/zenin/share/<token>
 */
shareRouter.post("/share/generate-link", authenticate, async (req, res) => {
  const userId = req.user!.id;
  const { panelId, deviceId, deviceName } = req.body as {
    panelId?: string;
    deviceId?: string;
    deviceName?: string;
  };

  if (!panelId || !deviceId) {
    res.status(400).json({ error: "panelId and deviceId required" });
    return;
  }

  const [panel] = await db
    .select({
      id: panelConfigs.id,
      firebaseUrl: panelConfigs.firebaseUrl,
      firebaseSecret: panelConfigs.firebaseSecret,
      name: panelConfigs.name,
    })
    .from(panelConfigs)
    .where(and(eq(panelConfigs.ownerId, userId), eq(panelConfigs.id, panelId)))
    .limit(1);

  if (!panel) {
    res.status(404).json({ error: "panel_not_found" });
    return;
  }

  const secret = decrypt(panel.firebaseSecret, SESSION_SECRET);
  const linkId = newLinkId();

  const scope = {
    type: "device",
    accId: panel.id,
    accName: panel.name ?? "",
    accUrl: panel.firebaseUrl,
    accKey: secret,
    devId: deviceId,
    devName: deviceName ?? deviceId,
    devPath: `clients/${deviceId}`,
    linkId,
    accIdx: 0,
    devIdx: 0,
  };

  const token = await encryptShareToken(scope);
  res.json({ token });
});

export default shareRouter;
