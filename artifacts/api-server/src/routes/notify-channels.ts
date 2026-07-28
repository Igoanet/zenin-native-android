import { Router, type IRouter } from "express";
import { readFile } from "fs/promises";
import path from "path";
import { authenticate, type AuthedRequest } from "../middleware/authenticate.js";

const router: IRouter = Router();

// CWD when running via pnpm is the package dir (artifacts/api-server/),
// so go up two levels to reach the workspace root, then into telegram-bots/.
const STORE_PATH = path.resolve("../../telegram-bots/data/store.json");

type RawNotifyKey = {
  key: string;
  chatId: number;
  category: string;
  title?: string;
};

function isRawNotifyKey(k: unknown): k is RawNotifyKey {
  if (!k || typeof k !== "object") return false;
  const r = k as Record<string, unknown>;
  return (
    typeof r["key"] === "string" &&
    typeof r["chatId"] === "number" &&
    typeof r["category"] === "string"
  );
}

async function loadNotifyKeysForUser(tgUid: number): Promise<RawNotifyKey[]> {
  try {
    const raw = await readFile(STORE_PATH, "utf-8");
    const store = JSON.parse(raw) as Record<string, unknown>;
    const notifyKeys = store["notifyKeys"];
    if (!notifyKeys || typeof notifyKeys !== "object") return [];
    const userKeys = (notifyKeys as Record<string, unknown>)[String(tgUid)];
    if (!Array.isArray(userKeys)) return [];
    return userKeys.filter(isRawNotifyKey);
  } catch {
    return [];
  }
}

// Scope auth to this router's own path. This router is mounted without a path
// prefix in routes/index.ts, so a path-less `router.use(authenticate)` would
// run on EVERY /api request that reaches it (401-ing public routes like
// /api/support-info and /api/downloads). Scoping to "/notify-channels" fixes it.
router.use("/notify-channels", authenticate);

router.get("/notify-channels", async (req: AuthedRequest, res) => {
  if (!req.user) {
    res.status(401).json({ error: "unauthorized" });
    return;
  }

  if (!req.user.tgUid) {
    res.json({ keys: [], grouped: {} });
    return;
  }
  const keys = await loadNotifyKeysForUser(req.user.tgUid);

  const grouped: Record<
    string,
    Array<{ key: string; chatId: number; title: string }>
  > = {};
  for (const k of keys) {
    if (!grouped[k.category]) grouped[k.category] = [];
    grouped[k.category].push({
      key: k.key,
      chatId: k.chatId,
      title: k.title || "",
    });
  }

  res.json({ keys, grouped });
});

export default router;
