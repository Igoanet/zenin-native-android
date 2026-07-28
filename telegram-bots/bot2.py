"""BOT 2 — Member Checker (Pyrogram, bot mode).

Two roles:

  * For ordinary users it stays intentionally silent — it never replies, it only
    runs membership checks (logged) and auto-links any channel it is added to,
    backfilling the shared JSON store's `requiredChannels`.
  * For the OWNER it is the SINGLE place to manage the required channel join
    links and the support button. Whatever the owner sets here is written to the
    shared JSON store, which bot1 (auto-verify), bot3 (account) and bot4
    (notification) all consume.

The reusable membership helpers live in `membership.py`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.handlers import (
    CallbackQueryHandler,
    ChatMemberUpdatedHandler,
    MessageHandler,
)

import bridge
import db
import membership
import store
from config import IS_DEPLOYMENT
from sender import btn, send

log = logging.getLogger("zenin.bot2")

CB_MENU = "menu"

# In-memory per-owner dialog state (single-instance service, process lifetime).
_dialogs: dict[int, dict[str, Any]] = {}


def _get_dialog(uid: int) -> dict[str, Any]:
    return _dialogs.get(uid, {"kind": "idle"})


def _set_dialog(uid: int, state: dict[str, Any]) -> None:
    if state.get("kind") == "idle":
        _dialogs.pop(uid, None)
    else:
        _dialogs[uid] = state


def _esc(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─── silent membership check (non-owner users) ───────────────────────────────
async def _run_silent_check(user_id: int, username: str | None) -> None:
    res = await membership.verify_membership(user_id)
    log.info(
        "bot2 membership check user=%s username=%s configured=%s joined=%s missing=%s unverifiable=%s verified=%s",
        user_id, username, res["configured"],
        [c["title"] for c in res["joined"]],
        [c["title"] for c in res["missing"]],
        [c["title"] for c in res["unverifiable"]],
        res["configured"] and len(res["missing"]) == 0,
    )


# ─── owner: channel + support management ─────────────────────────────────────
def _owner_menu() -> list:
    return [
        [btn("➕ Add channel", cb="ch_add")],
        [btn("📋 List channels", cb="ch_list"), btn("➖ Remove channel", cb="ch_rm")],
        [btn("💬 Edit support", cb="sup_edit")],
        [btn("📢 Broadcast all", cb="bc_all")],
    ]


async def _send_owner_menu(client: Client, chat_id: int, header: Optional[str] = None) -> None:
    chans = store.required_channels()
    sb = store.support_button()
    head = header or "👑 <b>Member Checker — Owner</b>"
    body = (
        f"{head}\n\n"
        "This is the single place to manage the required channels and support "
        "link that the account, auto-verify and notification bots all use.\n\n"
        f"<b>Channels configured:</b> {len(chans)}\n"
        f"<b>Support:</b> {_esc(sb.get('text'))} → {_esc(sb.get('url'))}\n\n"
        "What would you like to do?"
    )
    await send(client, chat_id, body, _owner_menu())


def _normalize_channel_ref(link: str) -> Optional[Any]:
    """Turn a user-typed channel reference into something membership.get_chat
    can resolve, or None when it can't be resolved (private invite link)."""
    t = link.strip()
    if not t:
        return None
    low = t.lower()
    if "t.me/+" in low or "t.me/joinchat" in low or "telegram.me/+" in low:
        return None  # private invite — can't resolve to a public username
    if low.startswith("https://t.me/") or low.startswith("http://t.me/") or low.startswith("t.me/"):
        handle = t.split("t.me/", 1)[1].strip("/").split("/")[0]
        if not handle or handle.startswith("+"):
            return None
        return f"@{handle}"
    if t.startswith("@"):
        return t
    if t.startswith("-") or t.isdigit():
        try:
            return int(t)
        except ValueError:
            return None
    # bare username
    return f"@{t}"


async def _owner_add_channel(client: Client, chat_id: int, raw: str) -> None:
    parts = raw.strip().split(None, 1)
    link = parts[0] if parts else ""
    title = parts[1].strip() if len(parts) > 1 else ""
    if not link:
        await send(client, chat_id, "❌ Empty. Send the channel link/@handle, then tap a menu item.")
        return

    ref = _normalize_channel_ref(link)
    chat = None
    if ref is not None:
        try:
            chat = await membership.get_chat(ref)
        except Exception as err:
            log.warning("bot2 owner add: get_chat failed for %s: %s", ref, err)
            chat = None

    if chat is not None and getattr(chat, "id", None):
        username = getattr(chat, "username", None)
        resolved_title = title or getattr(chat, "title", None) or username or str(chat.id)
        entry: dict[str, Any] = {"chatId": chat.id, "title": resolved_title}
        if username:
            entry["inviteLink"] = f"https://t.me/{username}"
        elif link:
            entry["inviteLink"] = link
        store.add_required_channel(entry)
        await send(
            client, chat_id,
            f"✅ Added <b>{_esc(resolved_title)}</b>\n"
            f"<b>Chat ID:</b> <code>{chat.id}</code>",
        )
    else:
        # Couldn't resolve (likely a private invite). Store as pending — bot2
        # fills in the real chat id once it's added to the channel as admin.
        entry = {
            "chatId": f"pending:{int(time.time() * 1000)}",
            "title": f"⏳ {title or link}",
            "inviteLink": link,
        }
        store.add_required_channel(entry)
        await send(
            client, chat_id,
            "✅ Saved as <b>pending</b>.\n\n"
            "I couldn't resolve that link directly. Add me (this bot) as an "
            "<b>admin</b> to that channel and I'll link it automatically. "
            "Membership checks only work once I'm in the channel.",
        )
    await _send_owner_menu(client, chat_id)


async def _owner_edit_support(client: Client, chat_id: int, raw: str) -> None:
    value = raw.strip()
    if not value:
        await send(client, chat_id, "❌ Empty. Send the support text and URL, or tap a menu item.")
        return
    if "|" in value:
        text, url = value.split("|", 1)
        text = text.strip()
        url = url.strip()
    else:
        text = "💬 Zenin Support"
        url = value
    if not url:
        await send(client, chat_id, "❌ Missing URL. Send it as <code>Text | https://t.me/...</code>.")
        return
    store.set_support_button(text, url)
    await send(client, chat_id, f"✅ Support updated:\n{_esc(text)} → {_esc(url)}")
    await _send_owner_menu(client, chat_id)


# ─── owner: broadcast to all account-bot users ───────────────────────────────
# Each entry: (Message attribute, client send-method name, supports a caption?).
# Order matters — pick the most specific media kind first (a Telegram GIF sets
# `.animation`, a round video sets `.video_note`, etc.), document last.
_MEDIA_KINDS: list[tuple[str, str, bool]] = [
    ("photo", "send_photo", True),
    ("animation", "send_animation", True),
    ("video_note", "send_video_note", False),
    ("video", "send_video", True),
    ("voice", "send_voice", True),
    ("audio", "send_audio", True),
    ("sticker", "send_sticker", False),
    ("document", "send_document", True),
]


def _detect_media(message: Any) -> Optional[tuple[str, str, bool]]:
    for kind, method, has_caption in _MEDIA_KINDS:
        if getattr(message, kind, None):
            return kind, method, has_caption
    return None


async def _build_broadcast_content(member_bot: Client, message: Any) -> Optional[dict[str, Any]]:
    """Capture the owner's message as a reusable broadcast payload.

    Text is carried verbatim with its entities (formatting/links preserved).
    Media is downloaded once via the member-checker bot; file ids are bot
    specific, so the bytes are what we hand to the account bot to re-upload.
    """
    if message.text:
        return {"type": "text", "text": message.text, "entities": message.entities or None}

    media = _detect_media(message)
    if media is None:
        return None
    kind, method, has_caption = media
    try:
        buf = await member_bot.download_media(message, in_memory=True)
    except Exception:
        log.exception("bot2 broadcast: failed to download media")
        return None
    if buf is None:
        return None
    return {
        "type": "media",
        "kind": kind,
        "method": method,
        "has_caption": has_caption,
        "buf": buf,
        "caption": message.caption if has_caption else None,
        "caption_entities": message.caption_entities if has_caption else None,
    }


async def _deliver_broadcast(
    account_bot: Client, uid: int, content: dict[str, Any], file_id: Optional[str]
) -> Optional[str]:
    """Deliver one broadcast item to one user via the account bot.

    Returns an account-bot file id to reuse for the next recipients so the media
    is uploaded once, not once per user. Raises on failure so the caller can
    count it (and handle FloodWait).
    """
    if content["type"] == "text":
        await account_bot.send_message(
            uid, content["text"], entities=content["entities"],
            disable_web_page_preview=True,
        )
        return None

    method = getattr(account_bot, content["method"])
    kwargs: dict[str, Any] = {}
    if content["has_caption"] and content.get("caption"):
        kwargs["caption"] = content["caption"]
        if content.get("caption_entities"):
            kwargs["caption_entities"] = content["caption_entities"]

    if file_id:
        await method(uid, file_id, **kwargs)
        return file_id

    content["buf"].seek(0)
    sent = await method(uid, content["buf"], **kwargs)
    media_obj = getattr(sent, content["kind"], None) if sent else None
    return getattr(media_obj, "file_id", None) if media_obj else None


async def _broadcast_recipients() -> list[int]:
    """Everyone who has opened the account bot: tracked starters unioned with
    onboarded users (the latter covers people who started before tracking).

    Lets DB errors propagate so callers fail closed rather than broadcast a
    partial audience while telling the owner it reached everyone.
    """
    uids: set[int] = set(store.list_account_bot_starters())
    uids.update(await db.list_all_user_tg_uids())
    return sorted(uids)


async def _prepare_broadcast(member_bot: Client, owner_chat_id: int, message: Any) -> None:
    """Capture the owner's content and ask for confirmation before sending."""
    if bridge.get_account_bot() is None:
        _set_dialog(owner_chat_id, {"kind": "idle"})
        await send(member_bot, owner_chat_id,
                   "❌ The account bot isn't running right now, so I can't broadcast. Try again shortly.")
        await _send_owner_menu(member_bot, owner_chat_id)
        return

    content = await _build_broadcast_content(member_bot, message)
    if content is None:
        _set_dialog(owner_chat_id, {"kind": "awaiting_broadcast"})
        await send(member_bot, owner_chat_id,
                   "❌ I can only broadcast a text message, photo, file, voice note, audio or video.\n\n"
                   "Send one of those, or tap Back to cancel.",
                   [[btn("⬅️ Back", cb=CB_MENU)]])
        return

    try:
        recipients = await _broadcast_recipients()
    except Exception:
        log.exception("bot2 broadcast: failed to load recipients")
        _set_dialog(owner_chat_id, {"kind": "idle"})
        await send(member_bot, owner_chat_id,
                   "❌ Couldn't load the full user list just now, so nothing was sent. "
                   "Please try again in a moment.")
        await _send_owner_menu(member_bot, owner_chat_id)
        return

    if not recipients:
        _set_dialog(owner_chat_id, {"kind": "idle"})
        await send(member_bot, owner_chat_id, "ℹ️ There are no account-bot users to broadcast to yet.")
        await _send_owner_menu(member_bot, owner_chat_id)
        return

    _set_dialog(owner_chat_id, {"kind": "confirm_broadcast", "content": content, "recipients": recipients})
    type_label = "text message" if content["type"] == "text" else content["kind"]
    await send(member_bot, owner_chat_id,
               "📢 <b>Confirm broadcast</b>\n\n"
               f"Content: <b>{_esc(type_label)}</b>\n"
               f"Recipients: <b>{len(recipients)}</b> (everyone who has opened the account bot)\n\n"
               "It will be sent from the account bot. Send it now?",
               [[btn(f"✅ Send to {len(recipients)}", cb="bc_go")],
                [btn("❌ Cancel", cb="bc_cancel")]])


async def _run_broadcast(member_bot: Client, owner_chat_id: int,
                         content: dict[str, Any], recipients: list[int]) -> None:
    account_bot = bridge.get_account_bot()
    if account_bot is None:
        await send(member_bot, owner_chat_id,
                   "❌ The account bot isn't running right now, so I can't broadcast. Try again shortly.")
        await _send_owner_menu(member_bot, owner_chat_id)
        return
    if not recipients:
        await send(member_bot, owner_chat_id, "ℹ️ No users to broadcast to.")
        await _send_owner_menu(member_bot, owner_chat_id)
        return

    await send(member_bot, owner_chat_id,
               f"📢 Broadcasting to <b>{len(recipients)}</b> users via the account bot… "
               "I'll report back when it's done.")

    sent_count = 0
    failed_count = 0
    file_id: Optional[str] = None
    for uid in recipients:
        try:
            file_id = await _deliver_broadcast(account_bot, uid, content, file_id)
            sent_count += 1
        except FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 1)) + 1)
            try:
                file_id = await _deliver_broadcast(account_bot, uid, content, file_id)
                sent_count += 1
            except Exception:
                log.warning("bot2 broadcast: delivery failed for uid=%s (after flood wait)", uid)
                failed_count += 1
        except Exception:
            log.warning("bot2 broadcast: delivery failed for uid=%s", uid)
            failed_count += 1
        await asyncio.sleep(0.05)

    await send(member_bot, owner_chat_id,
               "✅ <b>Broadcast complete</b>\n\n"
               f"Delivered: <b>{sent_count}</b>\n"
               f"Failed / blocked: <b>{failed_count}</b>\n"
               f"Total users: <b>{len(recipients)}</b>")
    await _send_owner_menu(member_bot, owner_chat_id)


# ─── owner: handlers ─────────────────────────────────────────────────────────
async def _on_owner_callback(client: Client, cq) -> bool:
    """Handle owner menu callbacks. Returns True if handled."""
    chat_id = cq.from_user.id
    data = cq.data or ""

    if data == CB_MENU:
        await cq.answer()
        _set_dialog(chat_id, {"kind": "idle"})
        await _send_owner_menu(client, chat_id)
        return True

    if data == "ch_add":
        await cq.answer()
        _set_dialog(chat_id, {"kind": "awaiting_channel"})
        await send(
            client, chat_id,
            "➕ <b>Add channel</b>\n\n"
            "Send the channel as <code>@handle</code> or a <code>https://t.me/...</code> "
            "link, optionally followed by a display title:\n"
            "<code>@mychannel My Channel</code>",
            [[btn("⬅️ Back", cb=CB_MENU)]],
        )
        return True

    if data == "ch_list":
        await cq.answer()
        chans = store.required_channels()
        if not chans:
            body = "📋 <b>Channels</b>\n\nNo channels configured yet."
        else:
            lines = "\n".join(
                f"{i + 1}. <b>{_esc(c.get('title'))}</b> — <code>{_esc(c.get('chatId'))}</code>"
                + (f"\n    {_esc(c.get('inviteLink'))}" if c.get("inviteLink") else "")
                for i, c in enumerate(chans)
            )
            body = f"📋 <b>Channels ({len(chans)})</b>\n\n{lines}"
        await send(client, chat_id, body, [[btn("⬅️ Back", cb=CB_MENU)]])
        return True

    if data == "ch_rm":
        await cq.answer()
        chans = store.required_channels()
        if not chans:
            await send(client, chat_id, "No channels to remove.", [[btn("⬅️ Back", cb=CB_MENU)]])
            return True
        kb = [[btn(f"➖ {c.get('title')}", cb=f"ch_rmx:{i}")] for i, c in enumerate(chans)]
        kb.append([btn("⬅️ Back", cb=CB_MENU)])
        await send(client, chat_id, "Tap a channel to remove it:", kb)
        return True

    if data.startswith("ch_rmx:"):
        try:
            idx = int(data[len("ch_rmx:"):])
        except ValueError:
            await cq.answer("Bad selection.", show_alert=True)
            return True
        removed = store.remove_required_channel_at(idx)
        if removed:
            await cq.answer("Removed")
            await send(client, chat_id, f"✅ Removed <b>{_esc(removed.get('title'))}</b>.")
        else:
            await cq.answer("Already gone.", show_alert=True)
        await _send_owner_menu(client, chat_id)
        return True

    if data == "sup_edit":
        await cq.answer()
        _set_dialog(chat_id, {"kind": "awaiting_support"})
        sb = store.support_button()
        await send(
            client, chat_id,
            "💬 <b>Edit support</b>\n\n"
            f"Current: {_esc(sb.get('text'))} → {_esc(sb.get('url'))}\n\n"
            "Send it as <code>Text | https://t.me/yoursupport</code>, "
            "or just a URL to keep the default label.",
            [[btn("⬅️ Back", cb=CB_MENU)]],
        )
        return True

    if data == "bc_all":
        await cq.answer()
        _set_dialog(chat_id, {"kind": "awaiting_broadcast"})
        await send(
            client, chat_id,
            "📢 <b>Broadcast to all account-bot users</b>\n\n"
            "Send me what you want to broadcast — it can be:\n"
            "• a text message\n"
            "• a photo / image\n"
            "• a file / document\n"
            "• a voice note or audio\n"
            "• a video\n\n"
            "It will be delivered to <b>everyone who has started the account bot</b>, "
            "sent from the account bot itself.\n\n"
            "Tap Back to cancel.",
            [[btn("⬅️ Back", cb=CB_MENU)]],
        )
        return True

    if data == "bc_go":
        await cq.answer()
        state = _get_dialog(chat_id)
        if state.get("kind") != "confirm_broadcast" or not state.get("content"):
            await send(client, chat_id,
                       "Nothing to broadcast — it may have expired. Tap 📢 Broadcast all to start again.")
            await _send_owner_menu(client, chat_id)
            return True
        content = state["content"]
        recipients = state.get("recipients") or []
        _set_dialog(chat_id, {"kind": "idle"})
        await _run_broadcast(client, chat_id, content, recipients)
        return True

    if data == "bc_cancel":
        await cq.answer("Cancelled")
        _set_dialog(chat_id, {"kind": "idle"})
        await send(client, chat_id, "❌ Broadcast cancelled.")
        await _send_owner_menu(client, chat_id)
        return True

    return False


# ─── dispatch ────────────────────────────────────────────────────────────────
async def _on_start(client: Client, msg) -> None:
    if not msg.from_user:
        return
    if store.is_owner(msg.from_user.id):
        _set_dialog(msg.from_user.id, {"kind": "idle"})
        await _send_owner_menu(client, msg.chat.id)
        return
    await _run_silent_check(msg.from_user.id, msg.from_user.username)


async def _on_message(client: Client, message) -> None:
    if not message.from_user:
        return
    chat_id = message.from_user.id
    if store.is_owner(chat_id):
        state = _get_dialog(chat_id)
        kind = state.get("kind")
        if kind in ("awaiting_broadcast", "confirm_broadcast"):
            await _prepare_broadcast(client, message.chat.id, message)
            return
        text = (message.text or "").strip()
        if kind == "awaiting_channel":
            _set_dialog(chat_id, {"kind": "idle"})
            await _owner_add_channel(client, message.chat.id, text)
            return
        if kind == "awaiting_support":
            _set_dialog(chat_id, {"kind": "idle"})
            await _owner_edit_support(client, message.chat.id, text)
            return
        await _send_owner_menu(client, message.chat.id)
        return
    await _run_silent_check(chat_id, message.from_user.username)


async def _on_callback(client: Client, cq) -> None:
    if cq.from_user and store.is_owner(cq.from_user.id):
        handled = await _on_owner_callback(client, cq)
        if handled:
            return
    try:
        await cq.answer()
    except Exception:
        pass
    if cq.from_user:
        await _run_silent_check(cq.from_user.id, cq.from_user.username)


# ─── auto-link channels bot2 is added to ─────────────────────────────────────
def _match_pending_channel(pendings: list[dict[str, Any]], chat: Any) -> Optional[dict[str, Any]]:
    """Pick which pending entry a freshly-joined chat belongs to.

    Prefer a confident match by username / invite link so multiple pending
    channels never get bound to the wrong chat id. Only fall back to "the one
    pending entry" when there is exactly one — otherwise refuse to guess.
    """
    username = getattr(chat, "username", None)
    chat_invite = getattr(chat, "invite_link", None)
    if username:
        uname_low = username.lower()
        for p in pendings:
            if uname_low in (p.get("inviteLink") or "").lower():
                return p
    if chat_invite:
        for p in pendings:
            if p.get("inviteLink") == chat_invite:
                return p
    if len(pendings) == 1:
        return pendings[0]
    return None


def _link_chat_to_store(chat: Any) -> None:
    if not chat or not getattr(chat, "id", None):
        return
    s = store.load_store()
    chat_id = chat.id
    title = getattr(chat, "title", None)
    username = getattr(chat, "username", None)

    existing = next((c for c in s["requiredChannels"] if str(c.get("chatId")) == str(chat_id)), None)
    if existing:
        if title and existing.get("title") != title:
            existing["title"] = title
            store.save_store()
        return

    pendings = [
        c for c in s["requiredChannels"]
        if isinstance(c.get("chatId"), str) and c["chatId"].startswith("pending:")
    ]
    pending = _match_pending_channel(pendings, chat)
    if pending:
        pending["chatId"] = chat_id
        if title:
            pending["title"] = title
        if username and not pending.get("inviteLink"):
            pending["inviteLink"] = f"https://t.me/{username}"
        store.save_store()
        log.info("bot2 linked pending channel id=%s title=%s", chat_id, title)
        return

    entry = {"chatId": chat_id, "title": title or username or str(chat_id)}
    if username:
        entry["inviteLink"] = f"https://t.me/{username}"
    s["requiredChannels"].append(entry)
    store.save_store()
    log.info("bot2 auto-added channel id=%s title=%s", chat_id, title)


async def _on_my_chat_member(client: Client, upd) -> None:
    new_member = getattr(upd, "new_chat_member", None)
    user = getattr(new_member, "user", None) if new_member else None
    if not (user and getattr(user, "is_self", False)):
        return
    status = getattr(new_member, "status", None)
    name = status.name if status is not None else ""
    if name in ("LEFT", "BANNED"):
        return
    _link_chat_to_store(upd.chat)


async def _on_channel_post(client: Client, message) -> None:
    # Receiving a channel post means bot2 belongs to that channel — use it to
    # discover the chat id.
    _link_chat_to_store(message.chat)


def register(app: Client) -> None:
    app.add_handler(MessageHandler(_on_start, filters.private & filters.command("start")))
    app.add_handler(
        MessageHandler(
            _on_message,
            filters.private & ~filters.service & ~filters.command("start"),
        )
    )
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_handler(ChatMemberUpdatedHandler(_on_my_chat_member))
    app.add_handler(MessageHandler(_on_channel_post, filters.channel))


async def on_started(app: Client) -> None:
    """Send a 'bot is back online' DM to every user who has ever started this bot.

    Only runs in a deployment — never in dev, to avoid spamming during restarts.
    Errors per individual user are silenced so one blocked user doesn't abort the rest.
    """
    if not IS_DEPLOYMENT:
        log.info("bot2: not a deployment; skipping online notification")
        return
    # Only notify if the bot was genuinely offline.
    # A heartbeat is written every 60 s while running; a gap < 3 min means
    # this is a normal deploy/restart, not an outage.
    _threshold_ms = 3 * 60 * 1_000
    last_beat = store.get_bot_heartbeat_ms("bot2")
    if last_beat is not None and (int(time.time() * 1_000) - last_beat) < _threshold_ms:
        log.info("bot2: heartbeat %d ms ago — normal restart, skipping online notification",
                 int(time.time() * 1_000) - last_beat)
        return
    uids: list[int] = [int(k) for k in (store.load_store().get("users") or {}).keys() if k.isdigit()]
    if not uids:
        return
    log.info("bot2: sending 'bot online' notification to %d user(s)", len(uids))
    sent = 0
    for uid in uids:
        try:
            await send(app, uid, "🟢 <b>Bot is back online</b>\n\nI just restarted and I'm live again.")
            sent += 1
        except Exception:
            pass
    log.info("bot2: online notification delivered to %d/%d user(s)", sent, len(uids))
