"""BOT 4 — Notification bot (Pyrogram, bot mode).

Dedicated bot that forwards the Zenin dashboard's panel notifications OUT to
users' bound channels (see bridge._post_to_channel). It answers /start and lets
each granted user manage notification keys, grouped by alert type.

Access rules (requested):
  1. Every user who opens the bot must first JOIN all required channels
     (verified via bot2, the only client that is channel admin). Until then
     they cannot proceed.
  2. Only users who have access to the ACCOUNT bot (bot3) — i.e. they exist in
     the shared `users` table / are management — may actually use this bot.
  3. All buttons/UI are in English.

Notification keys:
  Three alert types — Transaction, Login, Online/Offline. For each type a user
  can create up to 2 keys. Creating a key asks for a channel chat ID, verifies
  this bot is an admin there, then issues the key. Each existing key can be
  updated (re-bind to a channel + fresh key) or deleted. Keys are resolved by
  the bridge (store.find_user_by_key) so the panel can route alerts to them.

The owner (store.is_owner → HARDCODED_OWNER_ID) is recognised as the same owner
UID as the other bots.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

import bot3
import membership
import store
from sender import btn, send

log = logging.getLogger("zenin.bot4")

_NO_ACCESS_TEXT = (
    "🚫 <b>Access denied</b>\n\n"
    "This notification bot is only available to users who have access to the "
    "account bot. If you think this is a mistake, please contact the owner."
)

_EXPIRED_TEXT = (
    "⌛ <b>Access expired</b>\n\n"
    "Your access key has expired. Open the account bot and redeem a new access "
    "key, then come back and tap /start."
)

_OWNER_TEXT = (
    "👑 <b>You are the owner</b>\n"
    "<i>Welcome to the Zenin Notification bot.</i>\n\n"
    "━━━━━━━━━━━━━━━━━━━\n"
    "📣 <b>What this bot does</b>\n"
    "• Forwards your panel notifications to your bound channels.\n"
    "• Sends Transaction, Login and Online/Offline alerts.\n"
    "━━━━━━━━━━━━━━━━━━━"
)

_GRANTED_TEXT = (
    "✅ <b>Access granted</b>\n\n"
    "Welcome to the <b>Zenin Notification bot</b>. You'll receive the panel's "
    "notifications here and in the channels configured by your keys below."
)

# (code, label) — code is persisted as the key's category and used in callbacks.
_CATEGORIES: list[tuple[str, str]] = [
    ("transaction", "💳 Transaction"),
    ("login", "🔓 Login"),
    ("onlineOffline", "🟢 Online"),
]
_CAT_LABELS = dict(_CATEGORIES)


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


# ─── transient dialog state (in-memory; NOT store["users"]) ─────────────────
# bot1 reuses store["users"] for both persisted keys and its own dialog state,
# so bot4 keeps its transient state separate to avoid clobbering bot1's data.
_dialogs: dict[int, dict[str, Any]] = {}


def _get_dialog(chat_id: int) -> dict[str, Any]:
    return _dialogs.get(chat_id, {"kind": "idle"})


def _set_dialog(chat_id: int, state: dict[str, Any]) -> None:
    if state.get("kind") == "idle":
        _dialogs.pop(chat_id, None)
    else:
        _dialogs[chat_id] = state


# ─── bot identity ───────────────────────────────────────────────────────────
_me_mention: Optional[str] = None


async def _bot_mention(client: Client) -> str:
    global _me_mention
    if _me_mention is None:
        try:
            me = await client.get_me()
            _me_mention = f"@{me.username}" if me and me.username else "this bot"
        except Exception:
            return "this bot"
    return _me_mention


# ─── channel gate ───────────────────────────────────────────────────────────
async def _all_joined(user_id: int) -> dict[str, Any]:
    res = await membership.verify_membership(user_id)
    if not res["configured"]:
        return {"ok": True, "missing": [], "unverifiable": []}
    ok = not res["missing"] and not res["unverifiable"]
    return {"ok": ok, "missing": res["missing"], "unverifiable": res["unverifiable"]}


async def _channel_link(ch: dict[str, Any]) -> str:
    if ch.get("inviteLink"):
        return ch["inviteLink"]
    cid = ch.get("chatId")
    if isinstance(cid, str) and cid.startswith("@"):
        return f"https://t.me/{cid[1:]}"
    info = await membership.get_chat(cid)
    if info is not None:
        if getattr(info, "username", None):
            return f"https://t.me/{info.username}"
        if getattr(info, "invite_link", None):
            return info.invite_link
    return ""


async def _send_join_prompt(
    client: Client, chat_id: int, missing: list, unverifiable: list
) -> None:
    if not store.required_channels():
        await send(
            client,
            chat_id,
            "⚠️ The owner hasn't configured any required channels yet. Please wait.",
        )
        return

    links: list[str] = []
    for ch in missing:
        links.append(await _channel_link(ch))
    for ch in unverifiable:
        cid = ch.get("chatId")
        link = ch.get("inviteLink") or (
            f"https://t.me/{cid[1:]}"
            if isinstance(cid, str) and cid.startswith("@")
            else ""
        )
        if link:
            links.append(link)
    links = [link for link in links if link]

    rows: list[list] = []
    pair_count = len(links) - (len(links) % 2)
    for i in range(0, pair_count, 2):
        rows.append(
            [
                btn(f"{i + 1}. Join Channel", url=links[i]),
                btn(f"{i + 2}. Join Channel", url=links[i + 1]),
            ]
        )
    if len(links) % 2 == 1:
        rows.append([btn(f"{len(links)}. Join Channel", url=links[-1])])
    rows.append([btn("✅ Verify", cb="verify_join")])

    text = (
        "⚠️ <b>You must join our channels to use this bot</b>\n\n"
        "Please join all the channels below, then tap '<b>Verify</b>'.\n\n"
        "Once you've joined them all, you'll get access."
    )
    await send(client, chat_id, text, rows)


# ─── account-bot access gate ────────────────────────────────────────────────
async def _account_access(user_id: int) -> dict[str, Any]:
    """{'role': str | None, 'live': bool} — same gate as Auto Verify.

    A plain ``user`` needs a LIVE access key; admins / owner / management do not.
    See bot3.access_status.
    """
    try:
        return await bot3.access_status(user_id)
    except Exception as err:  # DB hiccup — fail closed, don't grant access.
        log.warning("access_status failed for %s: %s", user_id, err)
        return {"role": None, "live": False}


async def _ensure_access(client: Client, chat_id: int, user_id: int) -> bool:
    """Run both gates. Sends the appropriate prompt and returns False if denied."""
    res = await _all_joined(user_id)
    if not res["ok"]:
        await _send_join_prompt(client, chat_id, res["missing"], res["unverifiable"])
        return False
    access = await _account_access(user_id)
    if access["role"] is None:
        await send(client, chat_id, _NO_ACCESS_TEXT)
        return False
    if not access["live"]:
        await send(client, chat_id, _EXPIRED_TEXT)
        return False
    return True


# ─── home + notification-key UI ─────────────────────────────────────────────
def _home_menu() -> list:
    rows = [[btn(label, cb=f"nk:cat:{code}")] for code, label in _CATEGORIES]
    rows.append([btn("❓ Help", cb="help")])
    return rows


def _support_line() -> str:
    sb = store.support_button()
    handle = (sb.get("url") or "").strip() or "@support"
    return ("❓ <b>Stuck somewhere?</b> Send us a quick message → "
            f"{_esc(handle)}")


async def _send_help(client: Client, chat_id: int) -> None:
    cap = store.MAX_NOTIFY_KEYS_PER_CATEGORY
    text = (
        "ℹ️ <b>How this bot works</b>\n\n"
        "This is the <b>Zenin Notification bot</b> — it routes your Zenin "
        "panel's real-time alerts to the Telegram channels of your choice.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📣 <b>Alert types</b>\n\n"
        "💳 <b>Transaction</b> — fires whenever a transaction event is detected "
        "on a monitored device.\n\n"
        "🔓 <b>Login</b> — fires when a panel login occurs (new session started "
        "on your Zenin panel).\n\n"
        "🟢 <b>Online</b> — fires every time a monitored device comes online.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔑 <b>Notification keys</b>\n\n"
        f"You can create up to <b>{cap}</b> keys per alert type, each pointing "
        "to a different Telegram channel or group. When an alert fires, the "
        "panel sends it to every channel bound to that alert type.\n\n"
        "<b>To add a key:</b>\n"
        "1. Add this bot as an <b>Administrator</b> in your channel or group.\n"
        "2. Open the bot, pick an alert type, then tap <b>➕ Add key</b>.\n"
        "3. Send the channel's <b>chat ID</b> "
        "(e.g. <code>-1001234567890</code>).\n"
        "4. Copy the key you receive and paste it into your Zenin panel under "
        "the matching notification setting.\n\n"
        "<b>Managing keys:</b>\n"
        "• <b>Update</b> — re-bind an existing key to a different channel.\n"
        "• <b>Delete</b> — remove a key; that channel stops receiving alerts.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "<b>Access</b>\n"
        "You must be a registered Zenin account holder to use this bot. "
        "If you see an access error, open the <b>Account bot</b> and redeem "
        "a valid access key first.\n\n"
        f"{_support_line()}"
    )
    await send(client, chat_id, text, [[btn("⬅ Back", cb="nk:home")]])


async def _send_home(client: Client, chat_id: int) -> None:
    text = (
        "🔑 <b>Notification keys</b>\n\n\n"
        f"Create up to {store.MAX_NOTIFY_KEYS_PER_CATEGORY} key for each alert "
        "type. Pick a type below to manage its keys."
    )
    await send(client, chat_id, text, _home_menu())


async def _render_category(client: Client, chat_id: int, category: str) -> None:
    label = _CAT_LABELS.get(category, category)
    keys = store.list_notify_keys(chat_id, category)
    cap = store.MAX_NOTIFY_KEYS_PER_CATEGORY

    lines = [f"{label} <b>keys</b> ({len(keys)}/{cap})"]
    rows: list[list] = []
    if keys:
        for i, k in enumerate(keys, 1):
            lines.append(
                f"\n\n<b>{i}.</b> {_esc(k.get('title') or k.get('chatId'))}\n"
                f"🔑 <code>{_esc(k.get('key'))}</code>"
            )
            rows.append(
                [
                    btn(f"🔄 Update {i}", cb=f"nk:upd:{category}:{k['key']}"),
                    btn(f"🗑 Delete {i}", cb=f"nk:del:{category}:{k['key']}"),
                ]
            )
    else:
        lines.append("\n\nNo keys yet. Tap <b>Add key</b> to create one.")

    if len(keys) < cap:
        rows.append([btn("➕ Add key", cb=f"nk:add:{category}")])
    else:
        rows.append([btn(f"🚫 Limit reached ({cap}/{cap})", cb=f"nk:cat:{category}")])
    rows.append([btn("⬅ Back", cb="nk:home")])
    await send(client, chat_id, "".join(lines), rows)


async def _prompt_for_chat(client: Client, chat_id: int, category: str, mode: str) -> None:
    me = await _bot_mention(client)
    verb = "Add" if mode == "add" else "Update"
    await send(
        client,
        chat_id,
        f"{'➕' if mode == 'add' else '🔄'} <b>{verb} {_CAT_LABELS[category]} key</b>\n\n"
        f"1. Add {me} as an <b>Administrator</b> in your channel or group.\n"
        "2. Send me the <b>chat ID</b> of that channel/group "
        "(e.g. <code>-1001234567890</code>).\n\n"
        "I'll verify I'm admin there, then give you the key.",
        [[btn("✖️ Cancel", cb=f"nk:cat:{category}")]],
    )


async def _handle_nk_chat_input(
    client: Client, chat_id: int, user_id: int, text: str, st: dict[str, Any]
) -> None:
    category = st.get("category", "")
    mode = st.get("mode", "add")
    old_key = st.get("oldKey")
    cancel_kb = [[btn("✖️ Cancel", cb=f"nk:cat:{category}")]]

    if text.startswith("/"):
        _set_dialog(chat_id, {"kind": "idle"})
        await _gate(client, chat_id, user_id)
        return
    if not await _ensure_access(client, chat_id, user_id):
        _set_dialog(chat_id, {"kind": "idle"})
        return

    m = re.search(r"-?\d+", text)
    if not m:
        await send(
            client, chat_id,
            "❌ That doesn't look like a chat ID. Send the numeric ID "
            "(e.g. <code>-1001234567890</code>), or tap Cancel.",
            cancel_kb,
        )
        return
    target_id = int(m.group(0))

    if mode == "add" and store.count_notify_keys(chat_id, category) >= store.MAX_NOTIFY_KEYS_PER_CATEGORY:
        _set_dialog(chat_id, {"kind": "idle"})
        await send(client, chat_id, "⚠️ You already have the maximum number of keys for this type.")
        await _render_category(client, chat_id, category)
        return
    if mode == "update" and not store.get_notify_key(chat_id, old_key or ""):
        _set_dialog(chat_id, {"kind": "idle"})
        await send(client, chat_id, "⚠️ That key no longer exists.")
        await _render_category(client, chat_id, category)
        return

    existing = store.list_notify_keys(chat_id, category)
    if any(k.get("chatId") == target_id and k.get("key") != old_key for k in existing):
        await send(client, chat_id, "⚠️ You already have a key for that channel in this type.", cancel_kb)
        return

    status: Optional[str] = None
    # Auto-normalise chat ID: channel IDs must be negative in Pyrogram.
    # Users often paste the ID without the leading minus sign
    # (e.g. 1003934202689 instead of -1003934202689).
    # Try the raw value first; if it fails and the ID is positive, retry
    # with the sign flipped so both forms are accepted transparently.
    candidates = [target_id] if target_id < 0 else [target_id, -target_id]
    resolved_target: Optional[int] = None
    for cid in candidates:
        try:
            member = await client.get_chat_member(cid, "me")
            status = member.status.name.lower() if member and member.status else None
            resolved_target = cid
            break
        except Exception as err:
            log.warning("bot4 get_chat_member failed target=%s: %s", cid, err)

    if resolved_target is None:
        await send(
            client, chat_id,
            f"❌ I can't see chat <code>{target_id}</code>.\n\n"
            "Make sure the chat ID is correct and I've been added to that chat, "
            "then send it again.\n\n"
            "💡 Channel IDs start with <code>-100</code> "
            "(e.g. <code>-1001234567890</code>). Make sure you include the minus sign.",
            cancel_kb,
        )
        return
    target_id = resolved_target

    if status not in ("administrator", "owner", "creator"):
        me = await _bot_mention(client)
        await send(
            client, chat_id,
            f"❌ I'm not an admin in chat <code>{target_id}</code> yet "
            f"(status: <code>{status or 'unknown'}</code>).\n\n"
            f"Please add {me} as <b>Administrator</b> there, then send the chat ID again.",
            cancel_kb,
        )
        return

    title = str(target_id)
    try:
        info = await client.get_chat(target_id)
        if info and getattr(info, "title", None):
            title = info.title
    except Exception:
        pass

    _set_dialog(chat_id, {"kind": "idle"})
    if mode == "update" and old_key:
        entry = store.update_notify_key(chat_id, old_key, target_id, title)
    else:
        entry = store.add_notify_key(chat_id, category, target_id, title)
    if not entry:
        # add returns None when the per-category cap is hit (authoritative check).
        if mode == "add":
            await send(client, chat_id, "⚠️ You already have the maximum number of keys for this type.")
        else:
            await send(client, chat_id, "❌ That key no longer exists. Please try again.")
        await _render_category(client, chat_id, category)
        return

    await send(
        client, chat_id,
        f"✅ <b>Key ready</b> for {_CAT_LABELS.get(category, category)}\n\n"
        f"🏷 Channel: <b>{_esc(title)}</b>\n"
        f"🔑 Key: <code>{_esc(entry['key'])}</code>\n\n"
        "Use this key in your Zenin panel to route these alerts to that channel.",
    )
    await _render_category(client, chat_id, category)


# ─── home dispatch ──────────────────────────────────────────────────────────
async def _send_home_gated(client: Client, chat_id: int, user_id: int) -> None:
    if await _ensure_access(client, chat_id, user_id):
        await _send_home(client, chat_id)


async def _gate(client: Client, chat_id: int, user_id: int) -> None:
    await _send_home_gated(client, chat_id, user_id)


# ─── handlers ───────────────────────────────────────────────────────────────
async def _on_start(client: Client, msg) -> None:
    if not msg.from_user:
        return
    _set_dialog(msg.chat.id, {"kind": "idle"})
    await _gate(client, msg.chat.id, msg.from_user.id)


async def _on_help_cmd(client: Client, msg) -> None:
    if not msg.from_user:
        return
    if not await _ensure_access(client, msg.chat.id, msg.from_user.id):
        return
    await _send_help(client, msg.chat.id)


async def _on_message(client: Client, msg) -> None:
    if not msg.from_user:
        return
    chat_id = msg.chat.id
    user_id = msg.from_user.id
    st = _get_dialog(chat_id)
    if st.get("kind") == "nk_awaiting_chat":
        await _handle_nk_chat_input(client, chat_id, user_id, (msg.text or "").strip(), st)
        return
    await _gate(client, chat_id, user_id)


async def _on_verify(client: Client, cq) -> None:
    if not cq.from_user:
        return await cq.answer()
    user_id = cq.from_user.id
    chat_id = cq.message.chat.id if cq.message else user_id
    res = await _all_joined(user_id)
    if not res["ok"]:
        await cq.answer(
            "❌ You haven't joined all the channels yet. Please join them and try again.",
            show_alert=True,
        )
        return
    await cq.answer("✅ Verified!")
    access = await _account_access(user_id)
    if access["role"] is None:
        await send(client, chat_id, _NO_ACCESS_TEXT)
        return
    if not access["live"]:
        await send(client, chat_id, _EXPIRED_TEXT)
        return
    await _send_home(client, chat_id)


async def _on_nk_callback(client: Client, cq, data: str) -> None:
    if not cq.from_user:
        return await cq.answer()
    user_id = cq.from_user.id
    chat_id = cq.message.chat.id if cq.message else user_id
    await cq.answer()
    if not await _ensure_access(client, chat_id, user_id):
        return

    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    category = parts[2] if len(parts) > 2 else ""
    key = parts[3] if len(parts) > 3 else ""

    if action == "home":
        _set_dialog(chat_id, {"kind": "idle"})
        await _send_home(client, chat_id)
        return

    if category not in _CAT_LABELS:
        await _send_home(client, chat_id)
        return

    if action == "cat":
        _set_dialog(chat_id, {"kind": "idle"})
        await _render_category(client, chat_id, category)
        return

    if action == "add":
        if store.count_notify_keys(chat_id, category) >= store.MAX_NOTIFY_KEYS_PER_CATEGORY:
            await _render_category(client, chat_id, category)
            return
        _set_dialog(chat_id, {"kind": "nk_awaiting_chat", "category": category, "mode": "add"})
        await _prompt_for_chat(client, chat_id, category, "add")
        return

    if action == "del":
        store.remove_notify_key(chat_id, key)
        _set_dialog(chat_id, {"kind": "idle"})
        await _render_category(client, chat_id, category)
        return

    if action == "upd":
        if not store.get_notify_key(chat_id, key):
            await _render_category(client, chat_id, category)
            return
        _set_dialog(
            chat_id,
            {"kind": "nk_awaiting_chat", "category": category, "mode": "update", "oldKey": key},
        )
        await _prompt_for_chat(client, chat_id, category, "update")
        return


async def _on_help(client: Client, cq) -> None:
    if not cq.from_user:
        return await cq.answer()
    chat_id = cq.message.chat.id if cq.message else cq.from_user.id
    await cq.answer()
    if not await _ensure_access(client, chat_id, cq.from_user.id):
        return
    await _send_help(client, chat_id)


async def _on_del_msg(client: Client, cq) -> None:
    """Delete the channel message that carries the notification inline button."""
    await cq.answer()
    try:
        if cq.message:
            await cq.message.delete()
    except Exception as err:
        log.warning("del_msg: failed to delete message: %s", err)


async def _on_callback(client: Client, cq) -> None:
    data = cq.data or ""
    if data == "del_msg":
        return await _on_del_msg(client, cq)
    if data == "verify_join":
        return await _on_verify(client, cq)
    if data == "help":
        return await _on_help(client, cq)
    if data.startswith("nk:"):
        return await _on_nk_callback(client, cq, data)
    await cq.answer()


def register(app: Client) -> None:
    """No standalone handlers.

    Bot4 (Notifications) is fully integrated into bot3.  All routing
    — /start, message input, and callback queries — flows through bot3's
    _on_message and _on_callback which delegate to this module when the
    user is in a notification dialog or taps a notification menu button.
    """
