/**
 * Push Token Routes
 *
 * POST /api/push-token — register or refresh an Expo push token for the
 *                        current user. Upserts by (userId, token).
 * DELETE /api/push-token — remove all push tokens for the current user.
 */
import { Router, type Request, type Response } from "express";
import { db, pushTokens } from "@workspace/db";
import { and, eq } from "drizzle-orm";
import { authenticate, type AuthedRequest } from "../middleware/authenticate.js";

const router = Router();

router.post("/push-token", authenticate, async (req: AuthedRequest, res: Response) => {
  const { token, platform = "android" } = req.body as {
    token?: string;
    platform?: string;
  };

  if (!token || typeof token !== "string" || !token.startsWith("ExponentPushToken[")) {
    res.status(400).json({ error: "invalid_token", message: "Must be a valid Expo push token" });
    return;
  }

  const userId = req.user!.id;

  await db
    .insert(pushTokens)
    .values({ userId, token, platform, updatedAt: new Date() })
    .onConflictDoUpdate({
      target: [pushTokens.userId, pushTokens.token],
      set: { platform, updatedAt: new Date() },
    });

  res.json({ ok: true });
});

router.delete("/push-token", authenticate, async (req: AuthedRequest, res: Response) => {
  const userId = req.user!.id;
  const { token } = req.body as { token?: string };

  if (token) {
    await db
      .delete(pushTokens)
      .where(and(eq(pushTokens.userId, userId), eq(pushTokens.token, token)));
  } else {
    await db.delete(pushTokens).where(eq(pushTokens.userId, userId));
  }

  res.json({ ok: true });
});

export default router;
