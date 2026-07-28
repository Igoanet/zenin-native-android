/**
 * Authentication middleware.
 * Reads the session token from Authorization header, zenin_token cookie, or ?token= query.
 * Verifies signature + expiry, checks tokenVersion against DB.
 *
 * PERFORMANCE: Uses a 60-second in-memory session cache to avoid hitting
 * Supabase/Postgres on every request. Each cache entry maps a token to its
 * verified AuthUser. Cache is invalidated when the token expires or on logout.
 */

import type { Request, Response, NextFunction } from "express";
import { verifyToken } from "../lib/auth.js";
import { db } from "@workspace/db";
import { users } from "@workspace/db/schema";
import { eq } from "drizzle-orm";

const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

// ─── Session Cache ──────────────────────────────────────────────────────────
// TTL is 60 seconds — short enough that role/access changes propagate quickly.

const SESSION_CACHE_TTL_MS = 60_000;

interface CacheEntry {
  user: AuthUser;
  expiresAt: number;
}

const sessionCache = new Map<string, CacheEntry>();

// Sweep expired entries every 5 minutes so the map doesn't grow unboundedly.
setInterval(
  () => {
    const now = Date.now();
    for (const [key, entry] of sessionCache) {
      if (now > entry.expiresAt) sessionCache.delete(key);
    }
  },
  5 * 60 * 1000,
).unref();

export function invalidateSessionCache(token: string): void {
  sessionCache.delete(token);
}

export function invalidateAllSessionsForUser(userId: string): void {
  for (const [key, entry] of sessionCache) {
    if (entry.user.id === userId) sessionCache.delete(key);
  }
}

// ─── AuthUser type ──────────────────────────────────────────────────────────

export interface AuthUser {
  id: string;
  userId: string;
  name: string;
  role: string;
  tgChatId: string | null | undefined;
  /** Telegram UID — the canonical account identifier used by the bot system */
  tgUid: number | null;
  tokenVersion: number;
}

/**
 * A narrowed Request type for route handlers that sit behind the `authenticate`
 * middleware. The middleware guarantees `req.user` is populated before the
 * handler runs, so `user` is non-optional here.
 */
export type AuthedRequest = Request & { user: AuthUser };

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      user?: AuthUser;
    }
  }
}

// ─── Token extraction ───────────────────────────────────────────────────────

function extractToken(req: Request): string | null {
  // 1. Authorization: Bearer <token>
  const auth = req.headers.authorization;
  if (auth?.startsWith("Bearer ")) return auth.slice(7);

  // 2. Cookie: zenin_token=<token>
  const cookie = req.cookies?.zenin_token as string | undefined;
  if (cookie) return cookie;

  // 3. Query param ?token=<token> (for SSE connections that can't set headers)
  const query = req.query.token as string | undefined;
  if (query) return query;

  return null;
}

// ─── Main middleware ─────────────────────────────────────────────────────────

export async function authenticate(
  req: Request,
  res: Response,
  next: NextFunction,
): Promise<void> {
  const token = extractToken(req);

  if (!token) {
    res.status(401).json({ error: "No token provided" });
    return;
  }

  // Fast-path: token in cache
  const cached = sessionCache.get(token);
  if (cached && Date.now() < cached.expiresAt) {
    req.user = cached.user;
    next();
    return;
  }

  // Verify JWT signature + expiry
  const result = verifyToken(token, SESSION_SECRET);
  if (!result.ok) {
    sessionCache.delete(token); // Remove any stale cache entry
    res.status(401).json({ error: result.reason });
    return;
  }

  const { userId, tokenVersion } = result.payload;

  try {
    const [user] = await db
      .select()
      .from(users)
      .where(eq(users.id, userId))
      .limit(1);

    if (!user) {
      res.status(401).json({ error: "User not found" });
      return;
    }

    if (!user.accessGranted) {
      res.status(403).json({ error: "Access not granted" });
      return;
    }

    if (user.accessExpiresAt && new Date() > user.accessExpiresAt) {
      res.status(403).json({ error: "Access expired" });
      return;
    }

    if (user.tokenVersion !== tokenVersion) {
      sessionCache.delete(token);
      res.status(401).json({ error: "Token invalidated" });
      return;
    }

    const authUser: AuthUser = {
      id: user.id,
      userId: user.userId,
      name: user.name,
      role: user.role,
      tgChatId: user.tgChatId,
      tgUid: user.tgUid ?? null,
      tokenVersion: user.tokenVersion,
    };

    // Cache the result
    sessionCache.set(token, {
      user: authUser,
      expiresAt: Date.now() + SESSION_CACHE_TTL_MS,
    });

    req.user = authUser;
    next();
  } catch (err) {
    console.error("authenticate middleware error:", err);
    res.status(500).json({ error: "Internal server error" });
  }
}

export function requireRole(...roles: string[]) {
  return (req: Request, res: Response, next: NextFunction): void => {
    if (!req.user) {
      res.status(401).json({ error: "Unauthorized" });
      return;
    }
    if (!roles.includes(req.user.role)) {
      res.status(403).json({ error: "Insufficient permissions" });
      return;
    }
    next();
  };
}
