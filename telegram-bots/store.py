"""JSON-file persistence for the AUTO VERIFY bot (bot1) and Member Checker (bot2).

Faithful port of the previous Node `storage.ts`. The Python service is now the
single owner of this store (single process, single event loop), so no locking
is required as long as callers never `await` in the middle of a mutate+save.
"""
from __future__ import annotations

import json
import os
import random
import string
import tempfile
import time
from typing import Any, Optional

from config import HARDCODED_OWNER_ID

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
STORE_FILE = os.path.join(DATA_DIR, "bot-store.json")

_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
_TOKEN_ALPHABET = string.digits + string.ascii_lowercase  # base36

_cache: Optional[dict[str, Any]] = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def generate_key_string() -> str:
    return "".join(random.choice(_KEY_ALPHABET) for _ in range(16))


def _empty() -> dict[str, Any]:
    return {
        "ownerChatId": HARDCODED_OWNER_ID,
        "requiredChannels": [],
        "tokens": {},
        "users": {},
        "updateOffset": 0,
        "supportButton": {"text": "💬 Zenin Support", "url": "https://t.me/igoan"},
        "notifyKeys": {},
        "accountBotStarters": {},
    }


def load_store() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        if not os.path.exists(STORE_FILE):
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(STORE_FILE, "w", encoding="utf-8") as f:
                json.dump(_empty(), f, indent=2)
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = _empty()
        data.update(raw if isinstance(raw, dict) else {})
        # Owner is hardcoded — never trust a stale value from disk.
        data["ownerChatId"] = HARDCODED_OWNER_ID
        sb = data.get("supportButton")
        if not sb or not sb.get("text") or not sb.get("url"):
            data["supportButton"] = _empty()["supportButton"]
        # Backfill `key` on any older UserKey records.
        for st in data.get("users", {}).values():
            if isinstance(st, dict) and st.get("kind") == "connected":
                for k in st.get("keys", []) or []:
                    if not k.get("key"):
                        k["key"] = generate_key_string()
        _cache = data
    except Exception:
        _cache = _empty()
    return _cache


def save_store() -> None:
    if _cache is None:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=2)
        os.replace(tmp, STORE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ─── tokens ──────────────────────────────────────────────────────────────
def mint_token() -> str:
    store = load_store()
    t = "".join(random.choice(_TOKEN_ALPHABET) for _ in range(24)).upper()
    store["tokens"][t] = {"createdAt": _now_ms()}
    save_store()
    return t


def revoke_token(token: str) -> tuple[bool, Optional[int]]:
    store = load_store()
    key = token.strip().upper()
    t = store["tokens"].get(key)
    if not t:
        return (False, None)
    disconnected: Optional[int] = None
    used_by = t.get("usedBy")
    if used_by:
        u = store["users"].get(str(used_by))
        if u and u.get("kind") == "connected":
            store["users"][str(used_by)] = {"kind": "awaiting_token"}
            disconnected = used_by
    del store["tokens"][key]
    save_store()
    return (True, disconnected)


def consume_token(token: str, chat_id: int) -> str:
    store = load_store()
    t = store["tokens"].get(token.strip().upper())
    if not t:
        return "invalid"
    if t.get("usedBy") and t.get("usedBy") != chat_id:
        return "owned_by_other"
    t["usedBy"] = chat_id
    if not t.get("usedAt"):
        t["usedAt"] = _now_ms()
    save_store()
    return "ok"


def get_token_status(token: str) -> dict[str, Any]:
    store = load_store()
    t = store["tokens"].get(token.strip().upper())
    if not t:
        return {"state": "unknown"}
    used_by = t.get("usedBy")
    if not used_by:
        return {"state": "pending"}
    user = store["users"].get(str(used_by))
    if user and user.get("kind") == "connected" and user.get("keys"):
        first = user["keys"][0]
        return {
            "state": "completed",
            "chatId": used_by,
            "key": first["key"],
            "channelTitle": first["title"],
        }
    return {"state": "consumed", "chatId": used_by}


# ─── keys / connected users ──────────────────────────────────────────────
def remove_keys_for_channel(channel_chat_id: int) -> list[int]:
    store = load_store()
    affected: list[int] = []
    for chat_id_str, st in store["users"].items():
        if not st or st.get("kind") != "connected":
            continue
        before = len(st.get("keys", []))
        nxt = [k for k in st.get("keys", []) if k.get("chatId") != channel_chat_id]
        if len(nxt) != before:
            store["users"][chat_id_str] = {"kind": "connected", "token": st.get("token", ""), "keys": nxt}
            affected.append(int(chat_id_str))
    if affected:
        save_store()
    return affected


def find_users_by_channel_chat_id(channel_chat_id: int) -> list[dict[str, Any]]:
    store = load_store()
    out: list[dict[str, Any]] = []
    for chat_id_str, st in store["users"].items():
        if not st or st.get("kind") != "connected":
            continue
        match = next((k for k in st.get("keys", []) if k.get("chatId") == channel_chat_id), None)
        if match:
            out.append({"userChatId": int(chat_id_str), "channelTitle": match["title"]})
    return out


def list_all_channel_keys() -> list[dict[str, Any]]:
    store = load_store()
    out: list[dict[str, Any]] = []
    for chat_id_str, st in store["users"].items():
        if not st or st.get("kind") != "connected":
            continue
        for k in st.get("keys", []):
            out.append({"userChatId": int(chat_id_str), "channelChatId": k["chatId"], "channelTitle": k["title"]})
    return out


def find_user_by_key(key: str) -> Optional[dict[str, Any]]:
    needle = key.strip()
    if not needle:
        return None
    store = load_store()
    for chat_id_str, st in store["users"].items():
        if not st or st.get("kind") != "connected":
            continue
        match = next((k for k in st.get("keys", []) if k.get("key") == needle), None)
        if match:
            return {"userChatId": int(chat_id_str), "channelChatId": match["chatId"], "channelTitle": match["title"]}
    # Notification keys (per-category, managed via bot4).
    for chat_id_str, arr in store.get("notifyKeys", {}).items():
        match = next((k for k in (arr or []) if k.get("key") == needle), None)
        if match:
            return {
                "userChatId": int(chat_id_str),
                "channelChatId": match["chatId"],
                "channelTitle": match["title"],
                "category": match.get("category"),
            }
    return None


# ─── notification keys (per-user, category-scoped; managed via bot4) ───────
NOTIFY_CATEGORIES = ("transaction", "login", "onlineOffline")
MAX_NOTIFY_KEYS_PER_CATEGORY = 1


def list_notify_keys(chat_id: int, category: Optional[str] = None) -> list[dict[str, Any]]:
    arr = load_store().get("notifyKeys", {}).get(str(chat_id), []) or []
    if category is None:
        return list(arr)
    return [k for k in arr if k.get("category") == category]


def count_notify_keys(chat_id: int, category: str) -> int:
    return len(list_notify_keys(chat_id, category))


def get_notify_key(chat_id: int, key: str) -> Optional[dict[str, Any]]:
    return next((k for k in list_notify_keys(chat_id) if k.get("key") == key), None)


def add_notify_key(
    chat_id: int, category: str, channel_chat_id: int, title: str
) -> Optional[dict[str, Any]]:
    """Append a key for (chat_id, category), enforcing the per-category cap.

    Returns None if the category is invalid or already at MAX_NOTIFY_KEYS_PER_CATEGORY.
    The check + append run synchronously (no awaits), so this is the authoritative,
    interleave-safe enforcement point for the cap; callers' pre-checks are UX only.
    """
    if category not in NOTIFY_CATEGORIES:
        return None
    s = load_store()
    arr = s.setdefault("notifyKeys", {}).setdefault(str(chat_id), [])
    if sum(1 for k in arr if k.get("category") == category) >= MAX_NOTIFY_KEYS_PER_CATEGORY:
        return None
    entry = {
        "key": generate_key_string(),
        "category": category,
        "chatId": channel_chat_id,
        "title": title,
        "createdAt": _now_ms(),
    }
    arr.append(entry)
    save_store()
    return entry


def update_notify_key(
    chat_id: int, key: str, channel_chat_id: int, title: str
) -> Optional[dict[str, Any]]:
    s = load_store()
    arr = s.get("notifyKeys", {}).get(str(chat_id), []) or []
    for k in arr:
        if k.get("key") == key:
            k["chatId"] = channel_chat_id
            k["title"] = title
            k["key"] = generate_key_string()
            k["createdAt"] = _now_ms()
            save_store()
            return k
    return None


def remove_notify_key(chat_id: int, key: str) -> bool:
    s = load_store()
    arr = s.get("notifyKeys", {}).get(str(chat_id), []) or []
    nxt = [k for k in arr if k.get("key") != key]
    if len(nxt) == len(arr):
        return False
    s.setdefault("notifyKeys", {})[str(chat_id)] = nxt
    save_store()
    return True


def list_connected_users() -> list[dict[str, Any]]:
    store = load_store()
    out: list[dict[str, Any]] = []
    for chat_id_str, st in store["users"].items():
        if st and st.get("kind") == "connected":
            for k in st.get("keys", []):
                out.append({"chatId": int(chat_id_str), "key": k["key"], "channelTitle": k["title"]})
    return out


# ─── user state / owner ──────────────────────────────────────────────────
def get_user_state(chat_id: int) -> dict[str, Any]:
    return load_store()["users"].get(str(chat_id)) or {"kind": "idle"}


def set_user_state(chat_id: int, state: dict[str, Any]) -> None:
    store = load_store()
    store["users"][str(chat_id)] = state
    save_store()


def owner_chat_id() -> Optional[int]:
    return load_store().get("ownerChatId")


def is_owner(chat_id: int) -> bool:
    oc = owner_chat_id()
    return oc is not None and oc == chat_id


def required_channels() -> list[dict[str, Any]]:
    return load_store()["requiredChannels"]


def support_button() -> dict[str, str]:
    return load_store()["supportButton"]


# ─── channel / support management (owner-managed via bot2) ────────────────
def add_required_channel(entry: dict[str, Any]) -> None:
    s = load_store()
    s["requiredChannels"].append(entry)
    save_store()


def remove_required_channel_at(index: int) -> Optional[dict[str, Any]]:
    s = load_store()
    chans = s["requiredChannels"]
    if 0 <= index < len(chans):
        removed = chans.pop(index)
        save_store()
        return removed
    return None


def set_support_button(text: str, url: str) -> None:
    s = load_store()
    s["supportButton"] = {"text": text, "url": url}
    save_store()


# ─── account-bot starters (everyone who opened bot3; audience for bot2 broadcast) ─
def record_account_bot_starter(chat_id: int) -> None:
    """Remember that `chat_id` has opened the account bot for the first time.

    Stores a millisecond timestamp of the first-ever start, once and only once.
    Repeat starts never overwrite the stored value, so this is purely a
    first-seen registration record — not ongoing usage analytics.

    The stored set is the broadcast audience for owner DMs.
    """
    s = load_store()
    starters = s.setdefault("accountBotStarters", {})
    key = str(chat_id)
    if key not in starters:
        starters[key] = _now_ms()
        save_store()


def list_account_bot_starters() -> list[int]:
    """Return every Telegram UID that has opened the account bot."""
    s = load_store()
    out: list[int] = []
    for k in (s.get("accountBotStarters") or {}).keys():
        try:
            out.append(int(k))
        except (TypeError, ValueError):
            continue
    return out


def set_bot_heartbeat(bot_id: str) -> None:
    """Record the current time as the last known-alive timestamp for bot_id.

    Written every 60 s by the heartbeat loop in main.py while the service is
    running. On startup, the online-notification functions compare now against
    this value: if the gap is < 3 minutes the restart was recent (normal
    deploy/crash-recovery) and the notification is suppressed.
    """
    s = load_store()
    s.setdefault("botHeartbeats", {})[bot_id] = _now_ms()
    save_store()


def get_bot_heartbeat_ms(bot_id: str) -> Optional[int]:
    """Return the last heartbeat timestamp (ms) for bot_id, or None if never set."""
    return load_store().get("botHeartbeats", {}).get(bot_id)
