"""BOT 1 — Auto Verify (Pyrogram, bot mode).

Faithful port of bot.ts. Fully inline-keyboard driven; /start is the only entry.
Owner gets a read-only panel; channels + support are managed in the Member
Checker bot (bot2). Users join the required channels (verified via bot2) and are
auto-connected when they're registered Account-bot users, then add this bot as
admin in their channel and generate KEYs.

Incoming channel posts are forwarded to the Node dashboard's auto-verify bus
(via the bridge) so they can be sent as SMS. A periodic verifier removes keys
for channels where this bot is no longer admin.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import aiohttp
from pyrogram import Client, filters
from pyrogram.handlers import (
    CallbackQueryHandler,
    ChatMemberUpdatedHandler,
    MessageHandler,
)

import bot3
import bridge
import membership
import store
from config import BOT3_TOKEN, BOT_NAME
from sender import btn, edit, send

log = logging.getLogger("zenin.bot1")

MAX_KEYS_PER_USER = 5

# Sentinel stored as the "token" for users auto-connected via their Account-bot
# access (the bot no longer uses pasted access tokens).
ACCOUNT_TOKEN = "account"

# Cached identity of this bot (filled at startup).
_me: dict[str, Any] = {"id": None, "username": None}


# ── Direct Telegram Bot API helper ───────────────────────────────────────────
async def _tg_api(method: str, params: dict) -> Optional[dict]:
    """Call a Telegram Bot API method via plain HTTP (no Pyrogram peer cache).

    Returns the "result" dict on success, None on any error. Never raises.
    Using this instead of client.get_chat / get_chat_member avoids the
    PeerIdInvalid error that Pyrogram throws for channels whose peer entity
    hasn't been seen in the current session (e.g. after a container restart).
    """
    url = f"https://api.telegram.org/bot{BOT3_TOKEN}/{method}"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url, params={k: str(v) for k, v in params.items()}
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]
                log.warning("tg_api %s → %s", method, data.get("description"))
    except Exception as err:
        log.warning("tg_api %s failed: %s", method, err)
    return None


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def _support_handle_display() -> str:
    url = store.support_button()["url"]
    m = re.search(r"t\.me/([A-Za-z0-9_+]+)", url, re.IGNORECASE)
    if not m:
        return url
    handle = m.group(1)
    return url if handle.startswith("+") else f"@{handle}"


# ─── membership ────────────────────────────────────────────────────────────
# ─── account-bot access gate ────────────────────────────────────────────────
async def _account_access(user_id: int) -> dict[str, Any]:
    """Resolve account-bot access: {'role': str | None, 'live': bool}.

    Everyone with a Zenin account may use Auto Verify, but a plain ``user`` must
    have a LIVE (non-expired) access key. Admins (base_admin / dev_admin), owner
    and management never need an access key. See bot3.access_status.
    """
    try:
        return await bot3.access_status(user_id)
    except Exception as err:  # DB hiccup — fail closed, don't grant access.
        log.warning("bot1 access_status failed for %s: %s", user_id, err)
        return {"role": None, "live": False}


async def _deny_no_account(client: Client, chat_id: int) -> None:
    handle = _support_handle_display()
    await send(
        client, chat_id,
        "🔒 <b>No access yet</b>\n\n"
        "This bot is only for registered Zenin account users.\n\n"
        "Open the <b>Account bot</b> and redeem an access key to get started, "
        f"then come back and tap /start.\n\n❓ Need help? → {handle}",
    )


async def _deny_expired(client: Client, chat_id: int) -> None:
    handle = _support_handle_display()
    await send(
        client, chat_id,
        "⌛ <b>Access expired</b>\n\n"
        "Your access key has expired. Open the <b>Account bot</b> and redeem a "
        "new access key, then come back and tap /start.\n\n"
        f"❓ Need help? → {handle}",
    )


async def _connected_access_ok(client: Client, chat_id: int) -> bool:
    """Re-validate a previously-connected user's account access on every action.

    Local 'connected' state is persisted, so a plain ``user`` whose access key
    has since expired (or whose account was removed) would otherwise keep using
    the bot indefinitely. On loss we drop the connected state and send the
    matching denial. Returns True only if the account may still use the bot.
    """
    access = await _account_access(chat_id)
    if access["role"] is None:
        store.set_user_state(chat_id, {"kind": "pending_join"})
        await _deny_no_account(client, chat_id)
        return False
    if not access["live"]:
        store.set_user_state(chat_id, {"kind": "pending_join"})
        await _deny_expired(client, chat_id)
        return False
    return True


async def _verify_all_joined(user_id: int) -> dict[str, Any]:
    res = await membership.verify_membership(user_id)
    if not res["configured"]:
        return {"ok": True, "missing": [], "unverifiable": []}
    ok = len(res["missing"]) == 0 and len(res["unverifiable"]) == 0
    return {"ok": ok, "missing": res["missing"], "unverifiable": res["unverifiable"]}


async def _ensure_channel_link(ch: dict[str, Any]) -> str:
    if ch.get("inviteLink"):
        return ch["inviteLink"]
    chat_id = ch.get("chatId")
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}"
    info = await membership.get_chat(chat_id)
    if info is not None:
        if getattr(info, "username", None):
            return f"https://t.me/{info.username}"
        if getattr(info, "invite_link", None):
            return info.invite_link
    return ""


# ─── user-facing screens ────────────────────────────────────────────────────
async def _send_join_prompt(client: Client, chat_id: int, missing: list, unverifiable: list) -> None:
    if not store.required_channels():
        await send(client, chat_id, "⚠️ The owner hasn't configured any required channels yet. Please wait.")
        return
    links: list[str] = []
    for ch in missing:
        links.append(await _ensure_channel_link(ch))
    for ch in unverifiable:
        cid = ch.get("chatId")
        link = ch.get("inviteLink") or (f"https://t.me/{cid[1:]}" if isinstance(cid, str) and cid.startswith("@") else "")
        if link:
            links.append(link)

    rows: list[list] = []
    pair_count = len(links) - (len(links) % 2)
    for i in range(0, pair_count, 2):
        rows.append([btn(f"{i + 1}. Link", url=links[i]), btn(f"{i + 2}. Link", url=links[i + 1])])
    if len(links) % 2 == 1:
        rows.append([btn(f"{len(links)}. Link", url=links[-1])])
    rows.append([btn("✅ Verify", cb="verify_join")])

    handle = _support_handle_display()
    text = (
        "⚠️ <b>You need to join our chat to use this bot</b>\n\n"
        "Please join all chat below, then click '<b>Verify</b>'.\n\n"
        "Once you've joined all chat, you'll get access to the bot.\n\n"
        f"❓ Need help? Just drop a message → {handle}"
    )
    await send(client, chat_id, text, rows)


async def _on_join_confirmed(client: Client, chat_id: int) -> None:
    # In private chats chat_id == user_id. Auto-connect registered Account-bot
    # users (no token to paste); deny everyone else.
    access = await _account_access(chat_id)
    if access["role"] is None:
        return await _deny_no_account(client, chat_id)
    if not access["live"]:
        return await _deny_expired(client, chat_id)
    existing = store.get_user_state(chat_id)
    keys = existing.get("keys", []) if existing.get("kind") == "connected" else []
    store.set_user_state(chat_id, {"kind": "connected", "token": ACCOUNT_TOKEN, "keys": keys})
    await send(
        client, chat_id,
        "✅ <b>Connected!</b>\n\n"
        "Your Zenin account access is active. You can now generate KEYs for the "
        "channels/groups where this bot is admin.",
    )
    await _send_user_home(client, chat_id)


# ─── USER HOME ──────────────────────────────────────────────────────────────
def _user_home_keyboard() -> list:
    return [[btn("🔑 Keys", cb="user:keys")], [btn("❓ Help", cb="user:help")]]


def _user_home_text(state: dict) -> str:
    is_connected = state.get("kind") == "connected" and bool(state.get("token"))
    status_line = "🟢 Status: <b>ACTIVE</b>" if is_connected else "🔴 Status: <b>INACTIVE</b>"
    if state.get("kind") != "connected":
        return f"🏠 <b>Your panel</b>\n\n{status_line}"
    return f"🏠 <b>Your panel</b>\n\n{status_line}\n🔑 Keys: <b>{len(state.get('keys', []))}</b>"


async def _send_user_home(client: Client, chat_id: int) -> None:
    state = store.get_user_state(chat_id)
    await send(client, chat_id, _user_home_text(state), _user_home_keyboard())


async def _edit_to_user_home(client: Client, chat_id: int, message_id: int) -> None:
    state = store.get_user_state(chat_id)
    await edit(client, chat_id, message_id, _user_home_text(state), _user_home_keyboard())


# ─── KEYS submenu ───────────────────────────────────────────────────────────
def _user_keys_text(state: dict) -> str:
    if state.get("kind") != "connected":
        return "🔑 <b>Keys</b>\n\nYou don't have access yet. Tap /start to verify."
    keys = state.get("keys", [])
    if not keys:
        return "🔑 <b>Your Keys</b>\n\nYou have <b>0</b> keys.\n\nTap <b>➕ Generate Key</b> to create one."
    lines = "\n".join(f"{i + 1}. <b>{k['title']}</b>\n   🔑 <code>{k['key']}</code>" for i, k in enumerate(keys))
    return f"🔑 <b>Your Keys ({len(keys)})</b>\n\n{lines}"


def _user_keys_keyboard(state: dict) -> list:
    rows: list = []
    keys = state.get("keys", []) if state.get("kind") == "connected" else []
    at_cap = state.get("kind") == "connected" and len(keys) >= MAX_KEYS_PER_USER
    if at_cap:
        rows.append([btn(f"🚫 Limit reached ({MAX_KEYS_PER_USER}/{MAX_KEYS_PER_USER})", cb="user:gen:full")])
    else:
        rows.append([btn("➕ Generate Key", cb="user:gen")])
    if state.get("kind") == "connected" and keys:
        rows.append([btn("🗑 Delete Key", cb="user:del")])
    rows.append([btn("🔙 Back", cb="user:home")])
    return rows


async def _edit_to_user_keys(client: Client, chat_id: int, message_id: int) -> None:
    state = store.get_user_state(chat_id)
    await edit(client, chat_id, message_id, _user_keys_text(state), _user_keys_keyboard(state))


async def _user_prompt_generate_key(client: Client, chat_id: int) -> None:
    state = store.get_user_state(chat_id)
    if state.get("kind") != "connected":
        await send(client, chat_id, "❌ You don't have access yet. Tap /start to verify.")
        return
    if len(state.get("keys", [])) >= MAX_KEYS_PER_USER:
        await send(client, chat_id,
                   f"⚠️ You've reached the <b>{MAX_KEYS_PER_USER}-key limit</b>.\n\n"
                   "Delete a key first if you want to add a new one.")
        return
    store.set_user_state(chat_id, {"kind": "awaiting_new_key_chat_id", "token": state["token"], "keys": state["keys"]})
    uname = _me.get("username") or "this bot"
    await send(
        client, chat_id,
        "➕ <b>Generate Key</b>\n\n"
        f"1. Add <b>@{uname}</b> as an <b>Administrator</b> in your channel or group.\n"
        "2. Send me the <b>chat ID</b> of that channel/group (e.g. <code>-1001234567890</code>).\n\n"
        "I'll verify I'm admin there and then send the key.",
        [[btn("✖️ Cancel", cb="user:keys")]],
    )


async def _handle_new_key_chat_id(client: Client, chat_id: int, text: str) -> None:
    state = store.get_user_state(chat_id)
    if state.get("kind") != "awaiting_new_key_chat_id":
        return
    m = re.search(r"-?\d+", text.strip())
    if not m:
        await send(client, chat_id,
                   "❌ That doesn't look like a chat ID. Send the numeric ID (e.g. <code>-1001234567890</code>).")
        return
    target_id = int(m.group(0))
    keys = state.get("keys", [])
    if len(keys) >= MAX_KEYS_PER_USER:
        await send(client, chat_id, f"⚠️ You've reached the <b>{MAX_KEYS_PER_USER}-key limit</b>. Delete one first.")
        store.set_user_state(chat_id, {"kind": "connected", "token": state["token"], "keys": keys})
        await _send_user_home(client, chat_id)
        return
    if any(k["chatId"] == target_id for k in keys):
        await send(client, chat_id, "⚠️ That key already exists.")
        store.set_user_state(chat_id, {"kind": "connected", "token": state["token"], "keys": keys})
        await _send_user_home(client, chat_id)
        return

    # Verify bot access and admin status via direct Telegram Bot API HTTP calls.
    # This bypasses Pyrogram's in-memory peer cache entirely, so it works even
    # right after a container restart when no peer entity has been seen yet.
    #
    # Auto-normalise: users sometimes paste channel IDs without the minus sign.
    # Try the raw value first; if the Bot API rejects it and the ID is positive,
    # retry with the sign flipped.
    candidates = [target_id] if target_id < 0 else [target_id, -target_id]
    resolved_target: Optional[int] = None
    title = str(target_id)
    for cid in candidates:
        chat_info = await _tg_api("getChat", {"chat_id": cid})
        if chat_info is not None:
            title = chat_info.get("title") or str(cid)
            resolved_target = cid
            break

    if resolved_target is None:
        await send(client, chat_id,
                   f"❌ I can't see chat <code>{target_id}</code>.\n\n"
                   "Make sure you have added me as an <b>Administrator</b> in that channel "
                   "<b>before</b> sending the chat ID, then try again.\n\n"
                   "💡 Channel IDs start with <code>-100</code> "
                   "(e.g. <code>-1001234567890</code>). Make sure you include the minus sign.")
        return
    target_id = resolved_target

    # Check the bot's own admin status via the same direct HTTP path.
    bot_id = _me.get("id") or (await client.get_me()).id
    member_info = await _tg_api("getChatMember", {"chat_id": target_id, "user_id": bot_id})
    status = (member_info.get("status") if member_info else None)

    is_admin = status in ("administrator", "creator")
    if not is_admin:
        await send(client, chat_id,
                   f"❌ I'm not an admin in chat <code>{target_id}</code> yet "
                   f"(status: <code>{status or 'not a member'}</code>).\n\n"
                   f"Please promote <b>@{_me.get('username')}</b> to <b>Administrator</b> "
                   "in that channel, then send the chat ID again.")
        return

    store.set_user_state(chat_id, {
        "kind": "awaiting_new_key_title", "token": state["token"], "keys": keys,
        "pendingChatId": target_id, "defaultTitle": title,
    })
    await send(
        client, chat_id,
        f"✅ Admin check passed for <b>{title}</b>.\n\n"
        "📝 Now send a <b>name</b> for this token (just for your reference).\n\n"
        "Or tap <b>Skip</b> to use the channel name.",
        [[btn("⏭ Skip", cb="user:gen:skip")], [btn("✖️ Cancel", cb="user:keys")]],
    )


async def _finalize_new_key(client: Client, chat_id: int, custom_title: Optional[str]) -> None:
    state = store.get_user_state(chat_id)
    if state.get("kind") != "awaiting_new_key_title":
        return
    title = (custom_title or "").strip() or state["defaultTitle"]
    new_key = store.generate_key_string()
    new_keys = list(state.get("keys", [])) + [
        {"key": new_key, "chatId": state["pendingChatId"], "title": title, "createdAt": _now_ms()}
    ]
    store.set_user_state(chat_id, {"kind": "connected", "token": state["token"], "keys": new_keys})
    await send(
        client, chat_id,
        "✅ <b>Key generated!</b>\n\n"
        f"🏷 Name: <b>{title}</b>\n"
        f"🔑 <b>KEY:</b> <code>{new_key}</code>\n\n"
        "Paste this KEY into the <b>Key</b> field in your Zenin panel.",
    )
    await _send_user_home(client, chat_id)


def _user_delete_keys_text(state: dict) -> str:
    if state.get("kind") != "connected" or not state.get("keys"):
        return "🗑 <b>Delete Key</b>\n\nYou have no keys to delete."
    return "🗑 <b>Delete Key</b>\n\nTap a key below to delete it."


def _user_delete_keys_keyboard(state: dict) -> list:
    rows: list = []
    if state.get("kind") == "connected":
        for k in state.get("keys", []):
            rows.append([btn(f"🗑 {k['title']}", cb=f"user:del:{k['key']}")])
    rows.append([btn("🔙 Back", cb="user:keys")])
    return rows


async def _edit_to_user_delete_keys(client: Client, chat_id: int, message_id: int) -> None:
    state = store.get_user_state(chat_id)
    await edit(client, chat_id, message_id, _user_delete_keys_text(state), _user_delete_keys_keyboard(state))


async def _user_delete_key(client: Client, chat_id: int, message_id: int, key_str: str) -> None:
    state = store.get_user_state(chat_id)
    if state.get("kind") != "connected":
        return
    new_keys = [k for k in state.get("keys", []) if k["key"] != key_str]
    store.set_user_state(chat_id, {"kind": "connected", "token": state["token"], "keys": new_keys})
    await _edit_to_user_delete_keys(client, chat_id, message_id)


# ─── HELP submenu ───────────────────────────────────────────────────────────
def _user_help_text(state: dict) -> str:
    connected = state.get("kind") == "connected" and bool(state.get("token"))
    status_line = "Status: ✅ <b>Active</b>" if connected else "Status: ❌ <b>No access</b>"
    return (
        "❓ <b>Help — Auto Verify bot</b>\n\n"
        "This bot connects your Telegram channels to the Zenin panel so that "
        "incoming channel messages are automatically forwarded as SMS.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "<b>Commands</b>\n"
        "• /start — open your panel\n\n"
        "<b>What this bot does</b>\n\n"
        "🔑 <b>Keys</b> — view all the channel keys you've generated. Each key "
        "links one of your Telegram channels to the Zenin panel.\n\n"
        "➕ <b>Generate Key</b> — create a new key for a channel or group where "
        "this bot is an admin. Steps:\n"
        "  1. Add this bot as an <b>Administrator</b> in your channel.\n"
        "  2. Send the channel's <b>chat ID</b> when prompted.\n"
        "  3. Paste the key you receive into your Zenin panel.\n"
        f"  You can have up to {MAX_KEYS_PER_USER} active keys at once.\n\n"
        "🗑 <b>Delete Key</b> — remove a key you no longer need. The channel "
        "will stop forwarding messages to the panel immediately.\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "<b>Access</b>\n"
        "Access is automatic for registered Zenin account holders — no token "
        "needed. If you see an access error, open the <b>Account bot</b> first "
        "and redeem a valid access key.\n\n"
        f"{status_line}\n\n"
        f"❓ Stuck? Message us → {_support_handle_display()}"
    )


def _user_help_keyboard(state: dict) -> list:
    return [[btn("🔙 Back", cb="user:home")]]


async def _edit_to_user_help(client: Client, chat_id: int, message_id: int) -> None:
    state = store.get_user_state(chat_id)
    await edit(client, chat_id, message_id, _user_help_text(state), _user_help_keyboard(state))


# ─── OWNER PANEL ────────────────────────────────────────────────────────────
def _owner_home_keyboard() -> list:
    return [[btn("🔄 Refresh", cb="owner:home")]]


def _owner_home_text(first_name: Optional[str]) -> str:
    chans = store.required_channels()
    greeting = f", <b>{first_name}</b>" if first_name else ""
    if not chans:
        lst = "<i>No channels set yet.</i>"
    else:
        lst = "\n".join(f"{i + 1}. <b>{c['title']}</b> — <code>{c['chatId']}</code>" for i, c in enumerate(chans))
    s = store.support_button()
    return (
        f"👑 <b>YOU ARE THE OWNER</b>{greeting}\n"
        f"<i>Welcome to {BOT_NAME} control panel.</i>\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📖 <b>Quick Guide</b>\n"
        "• Channels & support are managed in the <b>Member Checker bot</b>.\n"
        "• Whatever you set there applies here automatically.\n"
        "• Users get their <b>KEY</b> after joining + adding this bot as admin in their own channel.\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        f"📡 <b>Required Channels ({len(chans)}):</b>\n{lst}\n\n"
        f"💬 <b>Support:</b> {s['text']} — <code>{s['url']}</code>"
    )


async def _send_owner_home(client: Client, chat_id: int, first_name: Optional[str] = None) -> None:
    store.set_user_state(chat_id, {"kind": "idle"})
    await send(client, chat_id, _owner_home_text(first_name), _owner_home_keyboard())


async def _edit_to_owner_home(client: Client, chat_id: int, message_id: int, first_name: Optional[str] = None) -> None:
    store.set_user_state(chat_id, {"kind": "idle"})
    await edit(client, chat_id, message_id, _owner_home_text(first_name), _owner_home_keyboard())


# ─── verify-join callback ───────────────────────────────────────────────────
async def _handle_verify_join_callback(client: Client, cq) -> None:
    chat_id = cq.message.chat.id if cq.message else None
    user_id = cq.from_user.id
    if chat_id is None:
        return
    res = await _verify_all_joined(user_id)
    if not res["ok"]:
        await cq.answer("❌ You haven't joined all the chats yet. Please join them and try again.", show_alert=True)
        return
    await cq.answer("✅ Verified by membership bot!")
    await _on_join_confirmed(client, chat_id)


# ─── callback router ────────────────────────────────────────────────────────
async def _on_callback(client: Client, cq) -> None:
    data = cq.data or ""
    chat_id = cq.message.chat.id if cq.message else None
    message_id = cq.message.id if cq.message else None

    if data == "verify_join":
        return await _handle_verify_join_callback(client, cq)

    if data.startswith("user:") and chat_id and message_id:
        if not await _connected_access_ok(client, chat_id):
            return await cq.answer("Access ended — tap /start.", show_alert=True)
        if data == "user:home":
            await cq.answer()
            return await _edit_to_user_home(client, chat_id, message_id)
        if data == "user:keys":
            await cq.answer()
            return await _edit_to_user_keys(client, chat_id, message_id)
        if data == "user:gen":
            await cq.answer()
            return await _user_prompt_generate_key(client, chat_id)
        if data == "user:gen:full":
            return await cq.answer(f"Limit reached: {MAX_KEYS_PER_USER} keys max. Delete one first.", show_alert=True)
        if data == "user:gen:skip":
            await cq.answer()
            return await _finalize_new_key(client, chat_id, None)
        if data == "user:del":
            await cq.answer()
            return await _edit_to_user_delete_keys(client, chat_id, message_id)
        if data.startswith("user:del:"):
            target = data[len("user:del:"):]
            await cq.answer("Deleted.")
            if target:
                await _user_delete_key(client, chat_id, message_id, target)
            return
        if data == "user:help":
            await cq.answer()
            return await _edit_to_user_help(client, chat_id, message_id)

    # Owner-only below.
    if not store.is_owner(cq.from_user.id):
        return await cq.answer("Not authorized.", show_alert=True)
    if not chat_id or not message_id:
        return await cq.answer()

    if data == "owner:home":
        await cq.answer()
        return await _edit_to_owner_home(client, chat_id, message_id)
    await cq.answer()


# ─── message router ─────────────────────────────────────────────────────────
async def _handle_start(client: Client, msg) -> None:
    chat_id = msg.chat.id
    s = store.load_store()
    if s.get("ownerChatId") is None:
        s["ownerChatId"] = chat_id
        store.save_store()
    if store.is_owner(chat_id):
        first = msg.from_user.first_name if msg.from_user else None
        return await _send_owner_home(client, chat_id, first)
    existing = store.get_user_state(chat_id)
    if existing.get("kind") == "connected" and existing.get("token"):
        if not await _connected_access_ok(client, chat_id):
            return
        return await _send_user_home(client, chat_id)
    first = msg.from_user.first_name if msg.from_user else None
    welcome = (
        f"👋 <b>Welcome{', ' + first if first else ''}!</b>\n\n"
        f"I am <b>{BOT_NAME}</b> — I help you receive verification tokens from your Telegram channel and "
        "instantly route them as SMS through the Zenin panel."
    )
    await send(client, chat_id, welcome)
    if not s["requiredChannels"]:
        return await _on_join_confirmed(client, chat_id)
    store.set_user_state(chat_id, {"kind": "pending_join"})
    res = await _verify_all_joined(chat_id)
    if res["ok"]:
        await _on_join_confirmed(client, chat_id)
    else:
        await _send_join_prompt(client, chat_id, res["missing"], res["unverifiable"])


async def _on_message(client: Client, msg) -> None:
    if msg.chat.type.name != "PRIVATE":
        return
    chat_id = msg.chat.id
    text = (msg.text or "").strip()
    if text.startswith("/start"):
        return await _handle_start(client, msg)

    state = store.get_user_state(chat_id)
    owner = store.is_owner(chat_id)

    if (
        state.get("kind") in ("connected", "awaiting_new_key_chat_id", "awaiting_new_key_title")
        and not owner
        and not await _connected_access_ok(client, chat_id)
    ):
        return

    if state.get("kind") == "awaiting_new_key_chat_id" and text:
        return await _handle_new_key_chat_id(client, chat_id, text)
    if state.get("kind") == "awaiting_new_key_title" and text:
        return await _finalize_new_key(client, chat_id, text)
    if state.get("kind") == "connected":
        return await _send_user_home(client, chat_id)
    if state.get("kind") == "pending_join":
        res = await _verify_all_joined(chat_id)
        return await _send_join_prompt(client, chat_id, res["missing"], res["unverifiable"])

    if owner:
        return await _send_owner_home(client, chat_id)
    await send(client, chat_id, "Send /start to begin.")


# ─── channel post → SMS bridge ──────────────────────────────────────────────
_SENDER_RE = re.compile(
    r"^(?:FROM|SENDER|FROM\s+NUMBER|SENDER\s+NUMBER)\s*[:\-]\s*(\+?[\d][\d\s\-\(\)]{5,})",
    re.IGNORECASE | re.MULTILINE,
)

def _extract_sender(text: str) -> Optional[str]:
    """Extract sender phone number from a channel post, if present.

    Handles common Android SMS-relay formats:
      FROM: +91XXXXXXXXXX
      SENDER: +91XXXXXXXXXX
      FROM NUMBER: +91XXXXXXXXXX
    Returns the digits-only (with leading +) normalized number, or None.
    """
    m = _SENDER_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if len(raw.replace("+", "")) < 6:
        return None
    return raw


async def _on_channel_post(client: Client, post) -> None:
    text = post.text
    if not text:
        return
    channel_chat_id = post.chat.id
    targets = store.find_users_by_channel_chat_id(channel_chat_id)
    if not targets:
        return
    channel_title = post.chat.title or str(channel_chat_id)
    sender = _extract_sender(text)
    for t in targets:
        payload: dict[str, Any] = {
            "userChatId": t["userChatId"],
            "channelChatId": channel_chat_id,
            "channelTitle": t["channelTitle"] or channel_title,
            "text": text,
        }
        if sender:
            payload["sender"] = sender
        await bridge.publish_sms_event(payload)
    log.info(
        "bot1 channel_post forwarded targets=%d len=%d sender=%s",
        len(targets), len(text), sender or "none",
    )


# ─── bot loses admin in a user channel ──────────────────────────────────────
async def _on_my_chat_member(client: Client, upd) -> None:
    new_member = getattr(upd, "new_chat_member", None)
    old_member = getattr(upd, "old_chat_member", None)
    user = getattr(new_member, "user", None) if new_member else None
    if not (user and getattr(user, "is_self", False)):
        return
    new_status = new_member.status.name if new_member and new_member.status else ""
    old_status = old_member.status.name if old_member and old_member.status else ""
    was_admin = old_status in ("ADMINISTRATOR", "OWNER")
    is_admin_now = new_status in ("ADMINISTRATOR", "OWNER")
    if not was_admin or is_admin_now:
        return
    channel_chat_id = upd.chat.id
    channel_title = upd.chat.title or str(channel_chat_id)
    affected = store.remove_keys_for_channel(channel_chat_id)
    for user_chat_id in affected:
        await send(
            client, user_chat_id,
            "⚠️ <b>Key removed.</b>\n\n"
            f"I'm no longer an admin in <b>{channel_title}</b>, so the key for that channel has been deleted.\n\n"
            "🔑 Re-add me as an admin and generate a new key from your panel to resume Auto Verify.",
        )


# ─── periodic admin verifier ────────────────────────────────────────────────
async def _verifier_loop(client: Client) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            all_keys = store.list_all_channel_keys()
            unique: dict[int, str] = {}
            for k in all_keys:
                unique[k["channelChatId"]] = k["channelTitle"]
            for channel_chat_id, channel_title in unique.items():
                try:
                    member = await client.get_chat_member(channel_chat_id, _me["id"])
                    status = member.status.name if member and member.status else ""
                except Exception:
                    continue
                if status in ("ADMINISTRATOR", "OWNER"):
                    continue
                affected = store.remove_keys_for_channel(channel_chat_id)
                for user_chat_id in affected:
                    await send(
                        client, user_chat_id,
                        "⚠️ <b>Key removed.</b>\n\n"
                        f"I'm no longer an admin in <b>{channel_title}</b>, so the key for that channel has been deleted.\n\n"
                        "🔑 Re-add me as an admin and generate a new key from your panel to resume Auto Verify.",
                    )
        except Exception as err:
            log.warning("bot1 verifier round failed: %s", err)
        await asyncio.sleep(60)


def register(app: Client) -> None:
    """Register only the channel-side handlers on the given client.

    Private-message and callback routing for Auto Verify is integrated
    into bot3 so that all user interaction flows through a single token.
    Only the channel-post forwarder and the admin-status watcher need to
    be attached here — they have no equivalent in bot3.
    """
    app.add_handler(MessageHandler(_on_channel_post, filters.channel))
    app.add_handler(ChatMemberUpdatedHandler(_on_my_chat_member))


async def on_started(app: Client) -> None:
    me = await app.get_me()
    _me["id"] = me.id
    _me["username"] = me.username
    log.info("bot1 connected @%s id=%s", me.username, me.id)
    asyncio.create_task(_verifier_loop(app))
