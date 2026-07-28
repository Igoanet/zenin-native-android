/**
 * Telegram Bot API — outbound helpers only.
 *
 * Inbound bot handling (commands, menus, membership checks, notifications,
 * panel sections, auto-verify) is performed by the Python/Pyrogram service
 * in telegram-bots/main.py which runs as a separate process.
 *
 * Requires TELEGRAM_BOT_TOKEN env var.
 * If not set, OTP sending falls back to console logging (dev mode).
 *
 * sendTelegramMessage THROWS on failure so callers can catch it.
 * (Previously it returned false — callers that forgot to check the return
 * value would silently succeed while the message was never delivered.)
 */

const BASE_URL = "https://api.telegram.org";

export async function sendTelegramMessage(
  chatId: string | number,
  text: string,
): Promise<void> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    console.warn(
      `[TELEGRAM] TELEGRAM_BOT_TOKEN not set — message to ${chatId} NOT sent.\n` +
      `[TELEGRAM DEV] Text: ${text.slice(0, 120)}`,
    );
    return; // dev-only fallback: don't throw, just warn
  }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 10_000);
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML" }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
  } catch (err: unknown) {
    clearTimeout(timer);
    const msg = (err as { name?: string; message?: string }).name === "AbortError"
      ? "Telegram request timed out after 10 s"
      : `Telegram network error: ${(err as Error).message}`;
    throw new Error(msg);
  }

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    // 403 = bot blocked by user or never started; 400 = bad chat_id
    throw new Error(
      `Telegram API ${res.status} sending to chat ${chatId}: ${body.slice(0, 300)}`,
    );
  }
}

export async function sendOtp(
  chatId: string | null | undefined,
  otp: string,
  appName = "ZENIN",
): Promise<void> {
  if (!chatId) {
    // No Telegram ID linked — log code to server console (dev / no-tg users)
    console.log(`[OTP] No chatId — code for dev/debug: ${otp}`);
    return;
  }

  const text =
    `🔐 <b>${appName} — Verification Code</b>\n` +
    `<code>━━━━━━━━━━━━━━━━━━━</code>\n\n` +
    `Your one-time login code is:\n\n` +
    `<code>  ${otp}  </code>\n\n` +
    `<code>━━━━━━━━━━━━━━━━━━━</code>\n` +
    `⏱ Expires in <b>5 minutes</b>\n` +
    `🔒 <i>Never share this code with anyone.</i>`;

  await sendTelegramMessage(chatId, text);
}
