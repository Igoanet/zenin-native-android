import { randomBytes } from "node:crypto";
import type { RoleValue } from "@workspace/db";

const KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const KEY_LENGTH = 12;

export function genKeyCode(): string {
  let out = "";
  const bytes = randomBytes(KEY_LENGTH);
  for (let i = 0; i < KEY_LENGTH; i++) {
    out += KEY_ALPHABET[bytes[i]! % KEY_ALPHABET.length];
  }
  return out;
}

// Role hierarchy: owner can create any non-owner key, admins can only create user keys.
export function canCreateKey(
  creatorRole: RoleValue,
  targetRole: RoleValue,
): boolean {
  if (creatorRole === "owner") {
    return targetRole !== "owner" && targetRole !== "management";
  }
  if (creatorRole === "dev_admin" || creatorRole === "base_admin") {
    return targetRole === "user";
  }
  return false;
}
