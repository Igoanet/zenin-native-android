/**
 * AES-256-GCM symmetric encryption for storing Firebase secrets in the DB.
 * Key is derived from SESSION_SECRET using scrypt.
 * All operations are synchronous (scryptSync) for simplicity.
 */

import {
  createCipheriv,
  createDecipheriv,
  randomBytes,
  scryptSync,
} from "node:crypto";

const SALT = "zenin-panel-aes-salt-v1";
const KEY_LEN = 32; // AES-256

function deriveKey(secret: string): Buffer {
  return scryptSync(secret, SALT, KEY_LEN, { N: 4096, r: 8, p: 1 });
}

/**
 * Encrypt plaintext using AES-256-GCM.
 * Returns "iv:authTag:ciphertext" in hex, colon-separated.
 */
export function encrypt(plaintext: string, secret: string): string {
  const key = deriveKey(secret);
  const iv = randomBytes(12); // 96-bit IV for GCM
  const cipher = createCipheriv("aes-256-gcm", key, iv);
  const encrypted = Buffer.concat([
    cipher.update(plaintext, "utf8"),
    cipher.final(),
  ]);
  const tag = cipher.getAuthTag();
  return [
    iv.toString("hex"),
    tag.toString("hex"),
    encrypted.toString("hex"),
  ].join(":");
}

/**
 * Decrypt a value produced by `encrypt()`.
 * Throws if the secret is wrong or the data is tampered.
 */
export function decrypt(encryptedStr: string, secret: string): string {
  const parts = encryptedStr.split(":");
  if (parts.length !== 3) throw new Error("Invalid encrypted format");
  const [ivHex, tagHex, encHex] = parts;
  const key = deriveKey(secret);
  const iv = Buffer.from(ivHex, "hex");
  const tag = Buffer.from(tagHex, "hex");
  const enc = Buffer.from(encHex, "hex");
  const decipher = createDecipheriv("aes-256-gcm", key, iv);
  decipher.setAuthTag(tag);
  return decipher.update(enc).toString("utf8") + decipher.final("utf8");
}
