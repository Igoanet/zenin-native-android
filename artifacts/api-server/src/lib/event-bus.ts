/**
 * In-process SSE pub/sub bus.
 * Each authenticated user connects to GET /api/events.
 * The bus routes events to the correct response stream(s) by user ID.
 */

import type { Response } from "express";

export type SseEvent = { type: string } & Record<string, unknown>;

interface SseClient {
  userId: string;
  res: Response;
}

const clients: SseClient[] = [];

/**
 * Register a response object as an SSE client for a given user.
 * Returns an unsubscribe function — call it on request close.
 */
export function sseSubscribe(userId: string, res: Response): () => void {
  const client: SseClient = { userId, res };
  clients.push(client);
  return () => {
    const idx = clients.indexOf(client);
    if (idx !== -1) clients.splice(idx, 1);
  };
}

/**
 * Emit an SSE event to all active connections for a given user.
 * Dead clients (write fails) are removed immediately — no ghost accumulation.
 */
export function sseEmit(userId: string, event: SseEvent): void {
  const msg = `data: ${JSON.stringify(event)}\n\n`;
  const dead: SseClient[] = [];
  for (const c of clients) {
    if (c.userId === userId) {
      try {
        c.res.write(msg);
      } catch {
        dead.push(c); // mark for immediate removal
      }
    }
  }
  for (const c of dead) {
    const idx = clients.indexOf(c);
    if (idx !== -1) clients.splice(idx, 1);
  }
}

/**
 * Broadcast an event to ALL connected clients.
 * Dead clients (write fails) are removed immediately.
 */
export function sseBroadcast(event: SseEvent): void {
  const msg = `data: ${JSON.stringify(event)}\n\n`;
  const dead: SseClient[] = [];
  for (const c of clients) {
    try {
      c.res.write(msg);
    } catch {
      dead.push(c);
    }
  }
  for (const c of dead) {
    const idx = clients.indexOf(c);
    if (idx !== -1) clients.splice(idx, 1);
  }
}

/** Current connection count (useful for health checks). */
export function sseClientCount(): number {
  return clients.length;
}
