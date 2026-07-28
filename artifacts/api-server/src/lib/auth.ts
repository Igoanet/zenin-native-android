/**
 * Authentication utilities
 * - HMAC-SHA256 session tokens (stateless, version-invalidatable)
 * - Argon2id password hashing (primary)
 * - scrypt password hashing (legacy — detected by hash format, rehashed on login)
 * - No external runtime dependencies beyond @node-rs/argon2 + built-in crypto
 */

import {
  createHmac,
  randomBytes,
  scrypt as _scrypt,
  timingSafeEqual,
  type ScryptOptions,
} from "node:crypto";
import * as argon2 from "@node-rs/argon2";

// ─── scrypt (legacy) ────────────────────────────────────────────────────────
// Kept for backward-compatible verification of old hashes.

function scryptAsync(
  password: string,
  salt: string,
  keylen: number,
  options: ScryptOptions,
): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    _scrypt(password, salt, keylen, options, (err, derivedKey) => {
      if (err) reject(err);
      else resolve(derivedKey as Buffer);
    });
  });
}

const SCRYPT_N = 16384;
const SCRYPT_R = 8;
const SCRYPT_P = 1;

async function verifyScrypt(
  password: string,
  hash: string,
  salt: string,
): Promise<boolean> {
  try {
    const derivedKey = await scryptAsync(password, salt, 64, {
      N: SCRYPT_N,
      r: SCRYPT_R,
      p: SCRYPT_P,
    });
    const hashBuf = Buffer.from(hash, "hex");
    if (derivedKey.length !== hashBuf.length) return false;
    return timingSafeEqual(derivedKey, hashBuf);
  } catch {
    return false;
  }
}

// ─── Argon2id (primary) ────────────────────────────────────────────────────
// Argon2id is the recommended password hashing algorithm (OWASP 2023).
// The @node-rs/argon2 package uses prebuilt native bindings.

const ARGON2_OPTIONS: argon2.Options = {
  memoryCost: 65536, // 64 MB
  timeCost: 3,       // 3 iterations
  parallelism: 2,
  // Use numeric literal 2 (= Argon2id) — ambient const enums cannot be
  // accessed when isolatedModules is enabled (TS2748).
  algorithm: 2 as argon2.Algorithm,
};

/** Hashes a password with Argon2id. Returns the PHC-formatted hash string. */
export async function hashPassword(
  password: string,
): Promise<{ hash: string; salt: string }> {
  // @node-rs/argon2 handles salt internally and returns a PHC string.
  // We store the full PHC hash in the `hash` field and an empty salt
  // so the DB schema doesn't require changes. The salt is embedded in
  // the PHC string.
  const phcHash = await argon2.hash(password, ARGON2_OPTIONS);
  return { hash: phcHash, salt: "argon2id" };
}

/**
 * Verify a password against a stored hash.
 * Detects format:
 *   - PHC string starting with "$argon2" → use @node-rs/argon2
 *   - 128-char hex string                → legacy scrypt
 * Returns { valid, needsRehash } so callers can transparently upgrade.
 */
export async function verifyPassword(
  password: string,
  hash: string,
  salt: string,
): Promise<boolean> {
  // Argon2id PHC string
  if (hash.startsWith("$argon2")) {
    try {
      return await argon2.verify(hash, password);
    } catch {
      return false;
    }
  }

  // Legacy scrypt (128-char hex)
  return verifyScrypt(password, hash, salt);
}

/**
 * Returns true if the stored hash should be upgraded to Argon2id.
 * Call after a successful login to transparently rehash.
 */
export function needsRehash(hash: string): boolean {
  return !hash.startsWith("$argon2");
}

// ─── Session Tokens ────────────────────────────────────────────────────────
// Format: <userId>.<tokenVersion>.<exp>.<sig>
// sig = HMAC-SHA256(<userId>.<tokenVersion>.<exp>, SESSION_SECRET)

const TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60; // 30 days

export function signToken(
  userId: string,
  tokenVersion: number,
  secret: string,
): string {
  const exp = Math.floor(Date.now() / 1000) + TOKEN_TTL_SECONDS;
  const payload = `${userId}.${tokenVersion}.${exp}`;
  const sig = createHmac("sha256", secret).update(payload).digest("base64url");
  return `${payload}.${sig}`;
}

export interface TokenPayload {
  userId: string;
  tokenVersion: number;
  exp: number;
}

export type TokenVerifyResult =
  | { ok: true; payload: TokenPayload }
  | { ok: false; reason: "invalid" | "expired" | "malformed" };

export function verifyToken(
  token: string,
  secret: string,
): TokenVerifyResult {
  const parts = token.split(".");
  if (parts.length !== 4) return { ok: false, reason: "malformed" };

  const [userId, versionStr, expStr, sig] = parts;
  if (!userId || !versionStr || !expStr || !sig)
    return { ok: false, reason: "malformed" };

  const payload = `${userId}.${versionStr}.${expStr}`;
  const expectedSig = createHmac("sha256", secret)
    .update(payload)
    .digest("base64url");

  const sigBuf = Buffer.from(sig);
  const expBuf = Buffer.from(expectedSig);
  if (
    sigBuf.length !== expBuf.length ||
    !timingSafeEqual(sigBuf, expBuf)
  ) {
    return { ok: false, reason: "invalid" };
  }

  const exp = parseInt(expStr, 10);
  if (isNaN(exp) || Math.floor(Date.now() / 1000) > exp) {
    return { ok: false, reason: "expired" };
  }

  const tokenVersion = parseInt(versionStr, 10);
  if (isNaN(tokenVersion)) return { ok: false, reason: "malformed" };

  return { ok: true, payload: { userId, tokenVersion, exp } };
}

// ─── OTP Generation ────────────────────────────────────────────────────────

export function generateOtp(): string {
  const n = randomBytes(3).readUIntBE(0, 3) % 1_000_000;
  return n.toString().padStart(6, "0");
}

export function generateId(): string {
  return randomBytes(16).toString("hex");
}

export function generateNumericId(): string {
  // 15 digits: timestamp ms (13) + 2 random digits — stays within JS safe integer range.
  return `${Date.now()}${Math.floor(Math.random() * 100).toString().padStart(2, "0")}`;
}

// ─── Pre-Auth Tokens (for login-capacity evict flow) ──────────────────────
// Short-lived signed tokens that authorize evicting an existing session.

const PREAUTH_TTL_SECONDS = 5 * 60; // 5 minutes

export function signPreAuthToken(userId: string, secret: string): string {
  const exp = Math.floor(Date.now() / 1000) + PREAUTH_TTL_SECONDS;
  const payload = `preauth.${userId}.${exp}`;
  const sig = createHmac("sha256", secret).update(payload).digest("base64url");
  return `${payload}.${sig}`;
}

export function verifyPreAuthToken(
  token: string,
  secret: string,
): { userId: string } | null {
  const parts = token.split(".");
  if (parts.length !== 4 || parts[0] !== "preauth") return null;

  const [, userId, expStr, sig] = parts;
  if (!userId || !expStr || !sig) return null;

  const payload = `preauth.${userId}.${expStr}`;
  const expectedSig = createHmac("sha256", secret)
    .update(payload)
    .digest("base64url");

  const sigBuf = Buffer.from(sig);
  const expBuf = Buffer.from(expectedSig);
  if (sigBuf.length !== expBuf.length || !timingSafeEqual(sigBuf, expBuf)) {
    return null;
  }

  const exp = parseInt(expStr, 10);
  if (isNaN(exp) || Math.floor(Date.now() / 1000) > exp) return null;

  return { userId };
}
