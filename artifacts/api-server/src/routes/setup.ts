/**
 * First-run setup endpoint.
 *
 * POST /api/setup
 *   Creates the initial owner account.
 *   Only works when there are ZERO users in the database.
 *   Returns an error if an account already exists.
 *
 * This is the ONLY unauthenticated write endpoint.
 */

import { Router } from "express";
import type { Request, Response } from "express";
import { db } from "@workspace/db";
import { users } from "@workspace/db/schema";
import { hashPassword, generateId, signToken } from "../lib/auth.js";

const router = Router();
const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

router.post("/", async (req: Request, res: Response) => {
  const { username, userId, name, password } = req.body as {
    username?: string;
    userId?: string;
    name?: string;
    password?: string;
  };

  if (!username || !userId || !name || !password) {
    res.status(400).json({ error: "username, userId, name, and password are required" });
    return;
  }

  if (username.length > 50 || userId.length > 50 || name.length > 100) {
    res.status(400).json({ error: "username and userId must be ≤50 characters, name ≤100" });
    return;
  }

  if (password.length < 8) {
    res.status(400).json({ error: "Password must be at least 8 characters" });
    return;
  }

  try {
    // Only allow setup when no users exist
    const existing = await db.select({ id: users.id }).from(users).limit(1);
    if (existing.length > 0) {
      res.status(403).json({
        error: "Setup already completed. Use /auth/login to sign in.",
      });
      return;
    }

    const { hash, salt } = await hashPassword(password);

    const [user] = await db
      .insert(users)
      .values({
        id: generateId(),
        username: username.trim(),
        userId: userId.trim(),
        name: name.trim(),
        role: "owner",
        passwordHash: hash,
        passwordSalt: salt,
        accessGranted: true,
        accessExpiresAt: null, // Owner never expires
      })
      .returning({
        id: users.id,
        username: users.username,
        userId: users.userId,
        name: users.name,
        role: users.role,
      });

    // Auto-login after setup
    const token = signToken(user.id, 0, SESSION_SECRET);

    res.status(201).json({
      ok: true,
      message: "Owner account created successfully.",
      token,
      user,
    });
  } catch (err) {
    console.error("setup error:", err);
    res.status(500).json({ error: "Internal server error" });
  }
});

// GET /api/setup — check if setup is needed
router.get("/", async (_req: Request, res: Response) => {
  const existing = await db.select({ id: users.id }).from(users).limit(1);
  res.json({ needsSetup: existing.length === 0 });
});

export default router;
