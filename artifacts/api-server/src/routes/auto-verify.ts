import { Router, type IRouter } from "express";
import {
  listConnectedUsers,
  checkKeyAdminStatus,
  findUserByKey,
  notifySmsResult,
  forwardNotification,
  type ResolvedKey,
  type SmsNotifyResult,
} from "../telegram-bot/pyBridge";
import {
  subscribeSms,
  consumePending,
  clearPendingFor,
  getRecentFor,
  publishSmsEvent,
} from "../telegram-bot/autoVerifyBus";
import { BRIDGE_SECRET } from "../telegram-bot/bridgeConfig";

const router: IRouter = Router();

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error("delivery_check_timed_out"));
    }, ms);
    timer.unref?.();
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      },
    );
  });
}

router.get("/auto-verify/users", async (_req, res) => {
  try {
    res.json({ users: await listConnectedUsers() });
  } catch {
    res.status(503).json({ error: "bot_offline" });
  }
});

// Fallback: when the bridge/Python bot is offline, verify admin status directly
// via the Telegram Bot API using the Portal Bot token from environment.
// The bot ID is parsed from the token (format: {id}:{hash}).
const _RAW_BOT_TOKEN = process.env["TG_BOT_TOKEN"] ?? "";
const _BOT_ID_MATCH = _RAW_BOT_TOKEN.match(/^(\d+):/);
const PORTAL_BOT_TOKEN = _RAW_BOT_TOKEN;
const PORTAL_BOT_ID = _BOT_ID_MATCH ? parseInt(_BOT_ID_MATCH[1]) : 0;

async function checkAdminDirect(chatId: string): Promise<{
  exists: boolean;
  isAdmin: boolean;
  channelTitle?: string;
} | null> {
  if (!PORTAL_BOT_TOKEN || !PORTAL_BOT_ID) return null;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 7000);
    try {
      const memberUrl = `https://api.telegram.org/bot${PORTAL_BOT_TOKEN}/getChatMember?chat_id=${encodeURIComponent(chatId)}&user_id=${PORTAL_BOT_ID}`;
      const memberRes = await fetch(memberUrl, { signal: ctrl.signal });
      if (!memberRes.ok) return null;
      const memberData = (await memberRes.json()) as {
        ok: boolean;
        result?: { status?: string };
      };
      if (!memberData.ok) return null;
      const status = memberData.result?.status ?? "";
      const isAdmin = status === "administrator" || status === "creator";

      let channelTitle = "";
      try {
        const chatUrl = `https://api.telegram.org/bot${PORTAL_BOT_TOKEN}/getChat?chat_id=${encodeURIComponent(chatId)}`;
        const chatRes = await fetch(chatUrl, { signal: ctrl.signal });
        if (chatRes.ok) {
          const chatData = (await chatRes.json()) as {
            ok: boolean;
            result?: { title?: string };
          };
          channelTitle = chatData.result?.title ?? "";
        }
      } catch {
        /* ignore — title is optional */
      }
      return { exists: true, isAdmin, channelTitle };
    } finally {
      clearTimeout(timer);
    }
  } catch {
    return null;
  }
}

router.get("/auto-verify/key-status", async (req, res) => {
  const key =
    typeof req.query["key"] === "string" ? req.query["key"].trim() : "";
  if (!key) {
    res.status(400).json({ error: "key required" });
    return;
  }
  try {
    // Try bridge first — the Python bot may be able to verify this.
    const bridgeResult = await checkKeyAdminStatus(key);
    if (bridgeResult.exists) {
      res.json(bridgeResult);
      return;
    }
    // Bridge returned exists:false — could be offline or genuinely not found.
    // Fall back to a direct Telegram API check so verification still works
    // even when the Python bot service is temporarily down.
    const direct = await checkAdminDirect(key);
    if (direct) {
      res.json(direct);
      return;
    }
    // Both failed — return bridge result (exists: false, isAdmin: false).
    res.json(bridgeResult);
  } catch {
    res.status(503).json({ error: "bot_offline" });
  }
});

router.post("/auto-verify/internal/publish", (req, res) => {
  if (req.headers["x-internal-secret"] !== BRIDGE_SECRET) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }
  const body = (req.body ?? {}) as {
    userChatId?: unknown;
    channelChatId?: unknown;
    channelTitle?: unknown;
    text?: unknown;
    sender?: unknown;
  };
  const userChatId =
    typeof body.userChatId === "number"
      ? body.userChatId
      : Number(body.userChatId);
  const channelChatId =
    typeof body.channelChatId === "number"
      ? body.channelChatId
      : Number(body.channelChatId);
  const channelTitle =
    typeof body.channelTitle === "string" ? body.channelTitle : "";
  const text = typeof body.text === "string" ? body.text : "";
  const sender =
    typeof body.sender === "string" && body.sender.trim()
      ? body.sender.trim()
      : undefined;
  if (
    !Number.isFinite(userChatId) ||
    !Number.isFinite(channelChatId) ||
    !text
  ) {
    res.status(400).json({ error: "invalid_body" });
    return;
  }
  const event = publishSmsEvent({
    userChatId,
    channelChatId,
    channelTitle,
    text,
    ...(sender ? { sender } : {}),
  });
  res.json({ id: event.id });
});

router.get("/auto-verify/stream", async (req, res) => {
  const key = String(req.query["key"] ?? "").trim();
  let resolved: ResolvedKey | null;
  try {
    resolved = key ? await findUserByKey(key) : null;
  } catch {
    res.status(503).json({ error: "bot_offline" });
    return;
  }
  if (!resolved) {
    res.status(401).json({ error: "invalid or missing key" });
    return;
  }
  const userChatId = resolved.userChatId;

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache, no-transform");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders?.();

  const send = (evtName: string, data: unknown) => {
    res.write(`event: ${evtName}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };

  const openedAt = Date.now();
  send("ready", { ts: openedAt });

  for (const ev of getRecentFor(userChatId).slice(-3)) {
    send("sms", ev);
  }

  const unsubscribe = subscribeSms(userChatId, (event) => {
    send("sms", event);
  });

  const keepalive = setInterval(() => {
    try {
      res.write(`: keepalive ${Date.now()}\n\n`);
    } catch {
      /* ignore */
    }
  }, 25_000);

  const cleanup = () => {
    clearInterval(keepalive);
    unsubscribe();
    clearPendingFor(userChatId);
    try {
      res.end();
    } catch {
      /* ignore */
    }
  };
  req.on("close", cleanup);
  req.on("aborted", cleanup);
});

router.get("/auto-verify/recent", async (req, res) => {
  const key = String(req.query["key"] ?? "").trim();
  let resolved: ResolvedKey | null;
  try {
    resolved = key ? await findUserByKey(key) : null;
  } catch {
    res.status(503).json({ error: "bot_offline" });
    return;
  }
  if (!resolved) {
    res.status(401).json({ error: "invalid or missing key" });
    return;
  }
  res.json({ events: getRecentFor(resolved.userChatId).slice(-10) });
});

router.post("/auto-verify/ack", async (req, res) => {
  const body = (req.body ?? {}) as {
    key?: unknown;
    id?: unknown;
    ok?: unknown;
    to?: unknown;
    simSlot?: unknown;
    error?: unknown;
    message?: unknown;
    deviceNumber?: unknown;
    deviceId?: unknown;
    confirmDelivery?: unknown;
    from_number?: unknown;
  };
  const key = typeof body.key === "string" ? body.key.trim() : "";
  let resolved: ResolvedKey | null;
  try {
    resolved = key ? await findUserByKey(key) : null;
  } catch {
    res.status(503).json({ error: "bot_offline" });
    return;
  }
  if (!resolved) {
    res.status(401).json({ error: "invalid or missing key" });
    return;
  }
  const id = typeof body.id === "string" ? body.id : "";
  const ok = body.ok === true;
  const to = typeof body.to === "string" ? body.to : "";
  const simSlot = typeof body.simSlot === "number" ? body.simSlot : undefined;
  const error = typeof body.error === "string" ? body.error : undefined;
  const message = typeof body.message === "string" ? body.message : undefined;
  const deviceNumber =
    typeof body.deviceNumber === "number" ? body.deviceNumber : undefined;
  const deviceId =
    typeof body.deviceId === "string" ? body.deviceId : undefined;
  if (!id) {
    res.status(400).json({ error: "id is required" });
    return;
  }
  const pending = consumePending(id);
  if (!pending) {
    res.json({ acked: false });
    return;
  }
  if (pending.userChatId !== resolved.userChatId) {
    res.status(403).json({ error: "ack does not belong to caller" });
    return;
  }
  const from_number =
    typeof body.from_number === "string" && body.from_number.trim()
      ? body.from_number.trim()
      : (pending.sender ?? undefined);
  if (body.confirmDelivery === true) {
    const ACK_DELIVERY_TIMEOUT_MS = 10_000;
    let delivery: SmsNotifyResult;
    try {
      delivery = await withTimeout(
        notifySmsResult({
          userChatId: pending.userChatId,
          ok,
          to,
          simSlot,
          error,
          message,
          deviceNumber,
          deviceId,
          from_number,
        }),
        ACK_DELIVERY_TIMEOUT_MS,
      );
    } catch (err) {
      delivery = {
        delivered: false,
        messageId: null,
        error: err instanceof Error ? err.message : "delivery_check_failed",
      };
    }
    res.json({ acked: true, delivery });
    return;
  }
  void notifySmsResult({
    userChatId: pending.userChatId,
    ok,
    to,
    simSlot,
    error,
    message,
    deviceNumber,
    deviceId,
    from_number,
  });
  res.json({ acked: true });
});

router.post("/auto-verify/notify-forward", async (req, res) => {
  const body = (req.body ?? {}) as {
    key?: unknown;
    text?: unknown;
    buttons?: unknown;
  };
  const key = typeof body.key === "string" ? body.key.trim() : "";
  const text = typeof body.text === "string" ? body.text : "";
  if (!key || !text.trim()) {
    res.status(400).json({ error: "key and text are required" });
    return;
  }
  const buttons = Array.isArray(body.buttons)
    ? (body.buttons as unknown[]).filter(
        (b): b is Record<string, unknown> =>
          !!b &&
          typeof b === "object" &&
          typeof (b as Record<string, unknown>).text === "string",
      )
    : undefined;
  const FORWARD_TIMEOUT_MS = 10_000;
  try {
    const result = await withTimeout(
      forwardNotification({ key, text, buttons }),
      FORWARD_TIMEOUT_MS,
    );
    if (!result.ok) {
      res
        .status(502)
        .json({ ok: false, error: result.error ?? "forward_failed" });
      return;
    }
    if (typeof result.messageId !== "number") {
      res.status(502).json({ ok: false, error: "delivery_unconfirmed" });
      return;
    }
    res.json({ ok: true, messageId: result.messageId });
  } catch {
    res.status(503).json({ error: "bot_offline" });
  }
});

export default router;
