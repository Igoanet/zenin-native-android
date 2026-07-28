export type AvSmsEvent = {
  id: string;
  userChatId: number;
  channelChatId: number;
  channelTitle: string;
  text: string;
  sender?: string;
  ts: number;
};

type Subscriber = (event: AvSmsEvent) => void;

const subscribers = new Map<number, Set<Subscriber>>();
const recent = new Map<number, AvSmsEvent[]>();
const RECENT_CAP = 20;
const pending = new Map<string, AvSmsEvent>();

const PENDING_TTL_MS = 5 * 60 * 1000; // 5 minutes

// Prune un-ACK'd events that are older than PENDING_TTL_MS.
// Without this the Map grows forever because consumePending() is only called
// when the dashboard explicitly ACKs; missed events (tab closed, network blip)
// would otherwise stay in memory for the lifetime of the process.
setInterval(() => {
  const cutoff = Date.now() - PENDING_TTL_MS;
  for (const [id, ev] of pending) {
    if (ev.ts < cutoff) pending.delete(id);
  }
}, PENDING_TTL_MS).unref();

let counter = 0;
function nextId(): string {
  counter = (counter + 1) % 1_000_000;
  return `${Date.now().toString(36)}-${counter.toString(36)}`;
}

export function publishSmsEvent(
  input: Omit<AvSmsEvent, "id" | "ts">,
): AvSmsEvent {
  const event: AvSmsEvent = { id: nextId(), ts: Date.now(), ...input };
  const buf = recent.get(event.userChatId) ?? [];
  buf.push(event);
  while (buf.length > RECENT_CAP) buf.shift();
  recent.set(event.userChatId, buf);
  pending.set(event.id, event);
  const subs = subscribers.get(event.userChatId);
  if (subs) {
    for (const sub of subs) {
      try {
        sub(event);
      } catch {
        /* ignore */
      }
    }
  }
  return event;
}

export function subscribeSms(userChatId: number, fn: Subscriber): () => void {
  let set = subscribers.get(userChatId);
  if (!set) {
    set = new Set();
    subscribers.set(userChatId, set);
  }
  set.add(fn);
  return () => {
    const s = subscribers.get(userChatId);
    if (!s) return;
    s.delete(fn);
    if (s.size === 0) subscribers.delete(userChatId);
  };
}

export function consumePending(id: string): AvSmsEvent | undefined {
  const ev = pending.get(id);
  if (ev) pending.delete(id);
  return ev;
}

export function clearPendingFor(userChatId: number): void {
  for (const [id, ev] of pending) {
    if (ev.userChatId === userChatId) pending.delete(id);
  }
}

export function getRecentFor(userChatId: number): AvSmsEvent[] {
  return recent.get(userChatId)?.slice() ?? [];
}

export function subscriberCount(userChatId: number): number {
  return subscribers.get(userChatId)?.size ?? 0;
}
