"""Central configuration for the Zenin Telegram bot service (Pyrogram, BOT MODE only).

All three bots authenticate with api_id + api_hash + bot_token. We never use a
phone number / user (MTProto userbot) login.
"""
from __future__ import annotations

import os


def _load_dotenv() -> None:
    """Load telegram-bots/.env into the process environment.

    Simple, dependency-free parser. A non-empty value in .env overrides any
    existing environment value, so the .env file is the authoritative config
    source. Blank entries are skipped, so anything left empty in .env falls
    back to an existing Replit Secret / environment variable.
    """
    path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and val:
            # Token/secret/hash keys must come from Replit Secrets only.
            # Skipping them here means a stale .env credential can never shadow
            # a freshly-rotated Replit Secret.
            if key.endswith("_TOKEN") or key.endswith("_SECRET") or key.endswith("_HASH"):
                continue
            os.environ.setdefault(key, val)


_load_dotenv()


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} environment variable is required but was not set")
    return v


# Telegram app credentials (shared by all three bots). BOT MODE only.
#
# Telegram's api_id is a short integer and api_hash is a 32-char hex string.
# The two secrets are currently stored swapped, so detect by *format* instead
# of trusting the variable names — this works no matter which secret holds
# which value.
def _resolve_api_creds() -> tuple[int, str]:
    a = _req("API_ID")
    b = _req("API_HASH")

    def is_hex32(s: str) -> bool:
        return len(s) == 32 and all(c in "0123456789abcdefABCDEF" for c in s)

    # api_hash is the 32-char hex one; api_id is the purely-numeric one.
    if a.isdigit() and is_hex32(b):
        return int(a), b
    if b.isdigit() and is_hex32(a):
        return int(b), a
    # Ambiguous — trust declared names and let int() raise if truly wrong.
    return int(a), b


API_ID, API_HASH = _resolve_api_creds()

# Bot tokens — two bots, two tokens.
#
# PortalBot  (BOT3_TOKEN) → TG_BOT_TOKEN  — main user bot
# PromoBot   (BOT2_TOKEN) → TG_BOT2_TOKEN — silent member checker + owner panel
#
# BOT1_TOKEN is kept for backwards compatibility but is not used in the current
# single-bot architecture (Auto Verify runs inside PortalBot, not a separate client).
BOT1_TOKEN = os.environ.get("BOT1_TOKEN") or None                                               # unused (integrated into PortalBot)
BOT2_TOKEN = os.environ.get("BOT2_TOKEN") or os.environ.get("TG_BOT2_TOKEN") or None           # optional — Member Checker merged into PortalBot
# Accept both TG_BOT_TOKEN (legacy) and TELEGRAM_BOT_TOKEN (Replit Secret name).
BOT3_TOKEN = (os.environ.get("BOT3_TOKEN")
              or os.environ.get("TG_BOT_TOKEN")
              or os.environ.get("TELEGRAM_BOT_TOKEN")
              or _req("TELEGRAM_BOT_TOKEN"))                                                     # PortalBot — Account / Panel / everything

# Optional dedicated "notification bot": forwards dashboard notifications OUT to
# users' bound channels. Stored as a Replit Secret (NOTIFY_BOT_TOKEN); BOT4_TOKEN
# in .env is accepted as an alternative. When unset, the bridge falls back to
# forwarding via bot1 (legacy behaviour).
NOTIFY_BOT_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN") or os.environ.get("BOT4_TOKEN") or None

# Optional dedicated "panel bot" (bot5): backs up and forwards every linked
# Firebase panel (and every failed APK) from the dashboard's Panel Linked
# section to the configured section channels. Stored as a Replit Secret
# (PANEL_BOT_TOKEN); BOT5_TOKEN in .env is accepted as an alternative. When
# unset, the panel bot does not start and the /panel-send bridge endpoint
# reports the bot as unavailable.
PANEL_BOT_TOKEN = os.environ.get("PANEL_BOT_TOKEN") or os.environ.get("BOT5_TOKEN") or None

# Shared Postgres (same DB the Node dashboard/API uses).
DATABASE_URL = _req("DATABASE_URL")

# Internal bridge runtime (Node <-> Python).
_RUNTIME_DIR = os.path.join(os.path.dirname(__file__), ".runtime")
_DEFAULT_SECRET_FILE = os.path.join(_RUNTIME_DIR, "bridge_secret")
_DEFAULT_SOCKET = os.path.join(_RUNTIME_DIR, "bridge.sock")


# Shared secret guarding both directions. Auto-generated on first run and
# persisted to a runtime file so it survives restarts without any manual setup.
def _resolve_bridge_secret() -> str:
    v = os.environ.get("BOT_BRIDGE_SECRET")
    if v:
        return v
    path = os.environ.get("BOT_BRIDGE_SECRET_FILE", _DEFAULT_SECRET_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            s = fh.read().strip()
        if s:
            return s
    except OSError:
        pass
    import secrets as _secrets
    s = _secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(s)
    return s


BRIDGE_SECRET = _resolve_bridge_secret()

# The bridge binds a Unix domain socket (local filesystem only). We never bind a
# TCP port because Replit auto-exposes any listening TCP port externally.
BRIDGE_SOCKET = os.environ.get("BOT_BRIDGE_SOCKET", _DEFAULT_SOCKET)

# Where Python pushes SMS events for the dashboard SSE bus (Node side).
# Goes through the shared in-container proxy which routes /api -> api-server.
NODE_API_BASE = os.environ.get("NODE_API_BASE", "http://127.0.0.1:80/api")

# Owner of the Auto Verify bot (bot1/bot2 JSON store). Configurable via .env
# (OWNER_ID); defaults to the original hardcoded value.
HARDCODED_OWNER_ID = int(os.environ.get("OWNER_ID", "8357650199"))

# "Management" Telegram UID for bot3 (above owner). Configurable via .env
# (MANAGER_ID); defaults to the original hardcoded value.
HARDCODED_MANAGEMENT_ID = int(os.environ.get("MANAGER_ID", "8357650199"))

# Owner cap enforced in bot3.
MAX_OWNERS = 3
OWNER_CAP_LOCK_KEY = 824073001


# True when running inside a published Replit Deployment (production) OR on the
# VPS (IS_PRODUCTION=true in docker-compose), false in the dev workspace. The
# panel bot's one-time "live" announcement and "bot is back online" DMs only
# fire in production so ordinary dev restarts never spam real users.
IS_DEPLOYMENT = bool(os.environ.get("REPLIT_DEPLOYMENT") or os.environ.get("IS_PRODUCTION"))


def _resolve_deployment_id() -> str:
    """Stable-per-deployment identifier used to dedup the panel-bot live
    announcement so it is sent once per deployment, not on every restart.

    Prefer an explicit identifier from the environment; otherwise fall back to a
    content hash of the bot source. A plain container restart keeps the same id
    (same files), while a fresh publish with changed code yields a new id.
    """
    for name in ("PANEL_BOT_DEPLOYMENT_ID", "REPLIT_DEPLOYMENT_ID", "DEPLOYMENT_ID"):
        v = os.environ.get(name)
        if v:
            return v
    import hashlib

    here = os.path.dirname(__file__)
    h = hashlib.sha256()
    try:
        for fn in sorted(os.listdir(here)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(here, fn), "rb") as fh:
                h.update(fn.encode("utf-8"))
                h.update(fh.read())
    except OSError:
        return "unknown"
    return h.hexdigest()[:16]


DEPLOYMENT_ID = _resolve_deployment_id()

BOT_NAME = "𝗔𝗨𝗧𝗢 𝗩𝗘𝗥𝗜𝗙𝗬  𝗕𝗢𝗧"
SESSIONS_DIR = os.path.join(os.path.dirname(__file__), ".sessions")

# Download URL for the ZENIN Android APK.
# Defaults to the GitHub Release so it works even before Railway has the file.
APK_URL = os.environ.get(
    "APK_URL",
    "https://github.com/Igoanet/zenin-native-android/releases/download/native-latest/ZENIN-Native.apk",
)

# Public-facing dashboard URL sent as a .url shortcut file after account creation
# or promotion.  Set DASHBOARD_URL in telegram-bots/.env to your Cloudflare
# tunnel URL (or any final domain).  Falls back to the raw VPS IP if unset.
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://13.60.208.8")
