/**
 * Expo Push Notification sender.
 *
 * Sends push notifications to registered devices via the Expo Push API.
 * Called from device-watcher.ts when a new_sms event fires.
 *
 * Expo Push API docs: https://docs.expo.dev/push-notifications/sending-notifications/
 */
import { db, pushTokens } from "@workspace/db";
import { inArray } from "drizzle-orm";

const EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send";

interface ExpoPushMessage {
  to: string | string[];
  title: string;
  body: string;
  data?: Record<string, unknown>;
  sound?: "default" | null;
  badge?: number;
  priority?: "default" | "normal" | "high";
}

/**
 * Send a push notification to all registered devices for a set of user IDs.
 * Fire-and-forget — errors are logged but never thrown.
 */
export async function sendNewSmsPush(opts: {
  userIds: string[];
  deviceName: string;
  sender: string;
  message: string;
  deviceId: string;
  panelId: string;
}): Promise<void> {
  if (!opts.userIds.length) return;

  try {
    // Fetch all push tokens for these users
    const rows = await db
      .select({ token: pushTokens.token })
      .from(pushTokens)
      .where(inArray(pushTokens.userId, opts.userIds));

    if (!rows.length) return;

    const tokens = rows.map((r) => r.token);

    const payload: ExpoPushMessage = {
      to: tokens,
      title: `📩 New SMS — ${opts.deviceName}`,
      body: `${opts.sender}: ${opts.message.slice(0, 120)}`,
      data: { deviceId: opts.deviceId, panelId: opts.panelId },
      sound: "default",
      priority: "high",
    };

    const resp = await fetch(EXPO_PUSH_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      console.warn("[push] Expo API error:", resp.status, text.slice(0, 200));
    } else {
      console.log(`[push] Sent to ${tokens.length} token(s) for ${opts.deviceName}`);
    }
  } catch (err) {
    console.warn("[push] sendNewSmsPush failed (non-fatal):", err instanceof Error ? err.message : err);
  }
}
