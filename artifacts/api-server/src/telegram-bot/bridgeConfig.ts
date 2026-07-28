import { readFileSync } from "node:fs";
import { join } from "node:path";

// __dirname in the built bundle is <workspace>/artifacts/api-server/dist
// so three levels up reaches the workspace root.
const WORKSPACE_ROOT = join(__dirname, "../../..");
const RUNTIME_DIR = join(WORKSPACE_ROOT, "telegram-bots", ".runtime");
const SECRET_FILE = join(RUNTIME_DIR, "bridge_secret");

function loadBridgeSecret(): string {
  const env =
    process.env["BOT_BRIDGE_SECRET"] ?? process.env["INTERNAL_BRIDGE_SECRET"];
  if (env) return env;
  try {
    return readFileSync(SECRET_FILE, "utf-8").trim();
  } catch {
    return "dev-bridge-secret-not-set";
  }
}

export const BRIDGE_SECRET = loadBridgeSecret();
export const BRIDGE_SOCKET =
  process.env["BOT_BRIDGE_SOCKET"] ?? join(RUNTIME_DIR, "bridge.sock");
