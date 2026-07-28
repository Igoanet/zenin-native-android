"""BOT 5 — Panel bot (Pyrogram, bot mode).

Dedicated bot that backs up and forwards every linked Firebase panel (and every
failed APK) from the dashboard's "Panel Linked" section. The actual forwarding
is driven by the Node API (routes/panel.ts), which resolves the destination
section channels and calls the bridge /panel-send endpoint. This bot owns the
*section registration* side: a role-holder binds ONE section channel here, and
the panel bot must be an admin of that channel before the binding is stored.

Forwarding hierarchy (resolved Node-side from the onboarding chain):
  • management section  → receives EVERY panel
  • owner section       → receives panels from the whole subtree they onboarded
  • dev_admin section   → receives panels from the subtree they onboarded

Access rules (mirroring bot4):
  1. Every user must first JOIN all required channels (verified via bot2, the
     only client that is channel admin).
  2. Only users who have access to the ACCOUNT bot (bot3) — i.e. they exist in
     the shared `users` table / are management — may use this bot.
  3. Only management / owner / dev_admin own a section channel. base_admin /
     user have no section: their linked panels still flow UP to the sections of
     whoever onboarded them, but they cannot register one here.
  4. All buttons / UI are in English.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from typing import Any, Optional

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

import bot3
import db
import membership
from config import DEPLOYMENT_ID, HARDCODED_MANAGEMENT_ID, IS_DEPLOYMENT
from sender import btn, send

log = logging.getLogger("zenin.bot5")

_NO_ACCESS_TEXT = (
    "🚫 <b>Access denied</b>\n\n"
    "This panel bot is only available to users who have access to the account "
    "bot. If you think this is a mistake, please contact the owner."
)

# Roles allowed to use the panel bot at all: dev_admin, owner and the ENV
# manager (resolved as the "management" role). base_admin / user are denied.
_ALLOWED_ROLES = ("management", "owner", "dev_admin")
# Roles that own a section channel (same set — every allowed role owns one).
_SECTION_ROLES = _ALLOWED_ROLES

_ROLE_LABELS = {
    "management": "👑 Management",
    "owner": "🏆 Owner",
    "dev_admin": "🛠 Dev-Owner",
    "base_admin": "🧰 Admin",
    "user": "👤 User",
}


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


# ─── transient dialog state (in-memory; NOT store["users"]) ─────────────────
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


# ─── channel gate (reuses bot2 via membership) ──────────────────────────────
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
    import store

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
async def _resolve_role(user_id: int) -> Optional[str]:
    try:
        return await bot3.effective_role(user_id)
    except Exception as err:  # DB hiccup — fail closed, don't grant access.
        log.warning("effective_role failed for %s: %s", user_id, err)
        return None


async def _ensure_access(client: Client, chat_id: int, user_id: int) -> Optional[str]:
    """Run both gates. Returns the caller's role, or None if denied (a prompt
    has already been sent)."""
    res = await _all_joined(user_id)
    if not res["ok"]:
        await _send_join_prompt(client, chat_id, res["missing"], res["unverifiable"])
        return None
    role = await _resolve_role(user_id)
    if role is None or role not in _ALLOWED_ROLES:
        await send(client, chat_id, _NO_ACCESS_TEXT)
        return None
    return role


# ─── home + section UI ──────────────────────────────────────────────────────
async def _send_home(client: Client, chat_id: int, role: str) -> None:
    can_register = role in _SECTION_ROLES
    role_label = _ROLE_LABELS.get(role, role)

    lines = [
        "🗂 <b>Zenin Panel Bot</b>",
        f"<i>Role: {role_label}</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━",
        "📦 <b>What this bot does</b>",
        "• Forwards every linked Firebase panel to your section channel.",
        "• If an APK fails to extract Firebase, the APK file is also forwarded.",
        "• Each forward includes a summary + a details file.",
        "━━━━━━━━━━━━━━━━━━━",
    ]

    rows: list[list] = []
    if can_register:
        section = await db.get_panel_section(chat_id)
        if section:
            lines.append(
                "\n🔗 <b>Section channel</b>\n"
                f"🏷 <b>{_esc(section.get('title') or section.get('chat_id'))}</b>\n"
                f"<code>{_esc(section.get('chat_id'))}</code>"
            )
            lines.append(
                "\nEvery panel forwarded to your section lands in this channel."
            )
            rows.append([btn("🔄 Update Panel", cb="ps:reg")])
            rows.append([btn("🗑 Remove Panel", cb="ps:del")])
        else:
            lines.append(
                "\n🔗 <b>Section channel</b>\nNot connected yet. Connect a channel "
                "to receive your section's panel backups."
            )
            rows.append([btn("➕ Connect Panel", cb="ps:reg")])
    else:
        lines.append(
            "\nℹ️ Your role does not own a section channel. Your linked panels are "
            "automatically backed up and forwarded to the people who onboarded you."
        )

    await send(client, chat_id, "\n".join(lines), rows or None)


async def _prompt_for_chat(client: Client, chat_id: int) -> None:
    me = await _bot_mention(client)
    await send(
        client,
        chat_id,
        "🔗 <b>Connect section channel</b>\n\n"
        f"1. Add {me} as an <b>Administrator</b> in your channel or group.\n"
        "2. Send me the <b>chat ID</b> of that channel/group "
        "(e.g. <code>-1001234567890</code>).\n\n"
        "I'll verify I'm admin there, then bind it as your section channel.",
        [[btn("✖️ Cancel", cb="ps:home")]],
    )


async def _handle_chat_input(
    client: Client, chat_id: int, user_id: int, text: str
) -> None:
    cancel_kb = [[btn("✖️ Cancel", cb="ps:home")]]

    if text.startswith("/"):
        _set_dialog(chat_id, {"kind": "idle"})
        await _gate(client, chat_id, user_id)
        return
    role = await _ensure_access(client, chat_id, user_id)
    if role is None or role not in _SECTION_ROLES:
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

    # Step 1 — verify the bot can see the chat at all (also resolves the peer
    # so the subsequent get_chat_member call won't throw PeerIdInvalid).
    title = str(target_id)
    try:
        info = await client.get_chat(target_id)
        if info and getattr(info, "title", None):
            title = info.title
    except Exception as err:
        log.warning("bot5 get_chat failed target=%s: %s", target_id, err)
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

    # Step 2 — check the bot's own admin status using its numeric ID (using the
    # string "me" can throw PeerIdInvalid in Pyrogram bot mode for unseen peers).
    status: Optional[str] = None
    try:
        from pyrogram.errors import UserNotParticipant as _UNP
        bot_self = await client.get_me()
        member = await client.get_chat_member(target_id, bot_self.id)
        status = member.status.name.lower() if member and member.status else None
    except Exception as err:
        log.warning("bot5 get_chat_member failed target=%s: %s", target_id, err)
        # get_chat succeeded so the bot CAN see the chat — treat this as
        # "not yet admin" rather than "can't see".
        status = None

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

    try:
        await db.upsert_panel_section(chat_id, role, target_id, title)
    except Exception as err:
        log.warning("bot5 upsert_panel_section failed uid=%s: %s", chat_id, err)
        await send(client, chat_id, "❌ Something went wrong saving your channel. Please try again.")
        return

    _set_dialog(chat_id, {"kind": "idle"})
    await send(
        client, chat_id,
        "✅ <b>Section channel connected</b>\n\n"
        f"🏷 Channel: <b>{_esc(title)}</b>\n"
        f"<code>{_esc(target_id)}</code>\n\n"
        "Every panel forwarded to your section will now be backed up here.",
    )
    await _send_home(client, chat_id, role)


# ─── home dispatch ──────────────────────────────────────────────────────────
async def _gate(client: Client, chat_id: int, user_id: int) -> None:
    role = await _ensure_access(client, chat_id, user_id)
    if role is not None:
        await _send_home(client, chat_id, role)


# ─── handlers ───────────────────────────────────────────────────────────────
async def _on_start(client: Client, msg) -> None:
    if not msg.from_user:
        return
    _set_dialog(msg.chat.id, {"kind": "idle"})
    await _gate(client, msg.chat.id, msg.from_user.id)


async def _on_message(client: Client, msg) -> None:
    if not msg.from_user:
        return
    chat_id = msg.chat.id
    user_id = msg.from_user.id
    st = _get_dialog(chat_id)
    if st.get("kind") == "ps_awaiting_chat":
        await _handle_chat_input(client, chat_id, user_id, (msg.text or "").strip())
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
    role = await _resolve_role(user_id)
    if role is None or role not in _ALLOWED_ROLES:
        await send(client, chat_id, _NO_ACCESS_TEXT)
        return
    await _send_home(client, chat_id, role)


async def _on_ps_callback(client: Client, cq, data: str) -> None:
    if not cq.from_user:
        return await cq.answer()
    user_id = cq.from_user.id
    chat_id = cq.message.chat.id if cq.message else user_id
    await cq.answer()
    role = await _ensure_access(client, chat_id, user_id)
    if role is None:
        return

    action = data.split(":", 1)[1] if ":" in data else ""

    if action == "home":
        _set_dialog(chat_id, {"kind": "idle"})
        await _send_home(client, chat_id, role)
        return

    if role not in _SECTION_ROLES:
        await _send_home(client, chat_id, role)
        return

    if action == "reg":
        _set_dialog(chat_id, {"kind": "ps_awaiting_chat"})
        await _prompt_for_chat(client, chat_id)
        return

    if action == "del":
        _set_dialog(chat_id, {"kind": "idle"})
        try:
            await db.remove_panel_section(chat_id)
        except Exception as err:
            log.warning("bot5 remove_panel_section failed uid=%s: %s", chat_id, err)
        await send(client, chat_id, "🗑 Your section channel has been disconnected.")
        await _send_home(client, chat_id, role)
        return


async def _on_callback(client: Client, cq) -> None:
    data = cq.data or ""
    if data == "verify_join":
        return await _on_verify(client, cq)
    if data.startswith("ps:"):
        return await _on_ps_callback(client, cq, data)
    await cq.answer()


def register(app: Client) -> None:
    app.add_handler(
        MessageHandler(_on_start, filters.private & filters.command("start"))
    )
    app.add_handler(
        MessageHandler(
            _on_message,
            filters.private & ~filters.service & ~filters.command("start"),
        )
    )
    app.add_handler(CallbackQueryHandler(_on_callback))


# ─── deploy-once "panel bot is live" announcement ───────────────────────────
# Persisted in app_settings: the deployment id we last announced for. When the
# panel bot starts in a new deployment, every section-owning role-holder
# (management / owner / dev_admin) gets one DM; restarts of the same deployment
# are skipped via this marker. base_admin / user are never notified.
_ANNOUNCE_SETTING_KEY = "panel_bot_live_announced_deploy"

_LIVE_ANNOUNCEMENT = (
    "🟢 <b>Panel Bot is now live.</b>\n\n"
    "Every panel linked in the dashboard — and any APK that fails to connect — "
    "will be automatically backed up and forwarded to your section channel.\n\n"
    "If you haven't already: add me as an admin in your Telegram channel and "
    "register it as your section so your backups start flowing."
)


async def _announce_recipients() -> list[int]:
    """tg_uids whose effective role owns a section channel.

    These are the management / owner / dev_admin holders (the same set allowed
    to register a section in this bot), plus the hardcoded management UID which
    is management-by-identity even without a users row.
    """
    uids: set[int] = {HARDCODED_MANAGEMENT_ID}
    rows = await db.list_users_by_roles(_SECTION_ROLES)
    for u in rows:
        uid = u.get("tg_uid")
        if uid is not None:
            uids.add(int(uid))
    return sorted(uids)


async def _run_live_announcement(app: Client) -> None:
    """Send the one-time 'panel bot is live' DM after a deployment."""
    import time
    import store as _store

    _HEARTBEAT_SUPPRESS_MS = 5 * 60 * 1000  # 5 minutes
    try:
        last_beat = _store.get_bot_heartbeat_ms("bot3")
        if last_beat is not None:
            gap_ms = int(time.time() * 1000) - last_beat
            if gap_ms < _HEARTBEAT_SUPPRESS_MS:
                log.info(
                    "bot5 announce: recent restart (gap=%ds) — suppressing announcement",
                    gap_ms // 1000,
                )
                return
    except Exception as err:
        log.warning("bot5 announce: heartbeat check failed: %s", err)

    try:
        last = await db.get_setting(_ANNOUNCE_SETTING_KEY)
    except Exception as err:
        log.warning("bot5 announce: could not read dedup marker: %s", err)
        return
    if last == DEPLOYMENT_ID:
        log.info(
            "bot5 announce: already announced for deployment %s; skipping",
            DEPLOYMENT_ID,
        )
        return

    try:
        recipients = await _announce_recipients()
    except Exception as err:
        log.warning("bot5 announce: could not resolve recipients, aborting: %s", err)
        return

    sent = 0
    failed = 0
    for uid in recipients:
        # send() already swallows per-recipient failures (e.g. the user never
        # opened the bot, or blocked it) and returns None, so one bad recipient
        # never aborts the pass or crashes startup.
        msg = await send(app, uid, _LIVE_ANNOUNCEMENT)
        if msg is not None:
            sent += 1
        else:
            failed += 1

    try:
        await db.set_setting(_ANNOUNCE_SETTING_KEY, DEPLOYMENT_ID)
    except Exception as err:
        log.warning("bot5 announce: failed to persist dedup marker: %s", err)

    log.info(
        "bot5 announce: live announcement complete sent=%s failed=%s deploy=%s",
        sent,
        failed,
        DEPLOYMENT_ID,
    )


# ─── recurring "connect your section channel" reminder ───────────────────────
# The one-time live announcement only fires once per deployment, so a section-
# owning role-holder (management / owner / dev_admin) who ignores it never gets
# nudged again. This loop closes that gap: on a schedule it DMs every such admin
# who still has NO row in panel_sections. Per-admin dedup (last-reminded epoch)
# is persisted in app_settings so a given admin is attempted at most once per
# period, even across restarts. Admins who already connected a channel — and
# users who never opened the bot — are skipped gracefully.
_REMINDER_SETTING_KEY = "panel_section_reminder_state"
_REMINDER_PERIOD_SECONDS = 24 * 60 * 60  # at most one reminder per admin per day
_REMINDER_CHECK_INTERVAL = 60 * 60  # re-scan hourly to catch newly-eligible admins
_REMINDER_STARTUP_DELAY = 30  # let the bot settle before the first scan

_REMINDER_TEXT = (
    "🔔 <b>Reminder: connect your section channel.</b>\n\n"
    "You still haven't linked a Telegram channel to receive your section's panel "
    "backups. Until you do, your linked panels only reach whoever onboarded you.\n\n"
    "To fix this: add me as an <b>Administrator</b> in your channel, then send "
    "/start and tap “➕ Connect channel” to register its chat ID."
)


async def _load_reminder_state() -> dict[str, float]:
    """Per-admin last-reminded epochs, keyed by str(tg_uid)."""
    try:
        raw = await db.get_setting(_REMINDER_SETTING_KEY)
    except Exception as err:
        log.warning("bot5 reminder: could not read dedup state: %s", err)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        log.warning("bot5 reminder: dedup state is not valid JSON; resetting")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


async def _save_reminder_state(state: dict[str, float]) -> None:
    try:
        await db.set_setting(_REMINDER_SETTING_KEY, json.dumps(state))
    except Exception as err:
        log.warning("bot5 reminder: failed to persist dedup state: %s", err)


async def _send_section_reminders(app: Client) -> None:
    """One reminder pass: DM eligible, un-connected, not-recently-pinged admins."""
    try:
        recipients = await _announce_recipients()
    except Exception as err:
        log.warning("bot5 reminder: could not resolve recipients, skipping pass: %s", err)
        return

    state = await _load_reminder_state()
    now = time.time()
    # Rebuilt from scratch each pass so markers for admins who lost their section
    # role (no longer in `recipients`) are pruned and the map stays bounded.
    next_state: dict[str, float] = {}
    sent = 0
    failed = 0
    connected = 0

    for uid in recipients:
        key = str(uid)
        prev = state.get(key)

        try:
            section = await db.get_panel_section(uid)
        except Exception as err:
            # DB hiccup: don't guess. Carry any prior marker forward so we never
            # double-ping, and try this admin again on the next pass.
            log.warning("bot5 reminder: get_panel_section failed uid=%s: %s", uid, err)
            if prev is not None:
                next_state[key] = prev
            continue

        if section:
            # Already connected → never remind; drop any stale marker.
            connected += 1
            continue

        if prev is not None and (now - prev) < _REMINDER_PERIOD_SECONDS:
            # Still inside the dedup window → keep the marker, don't ping.
            next_state[key] = prev
            continue

        # send() swallows per-recipient failures (user never opened the bot, or
        # blocked it) and returns None, so one bad recipient never aborts the
        # pass. We mark the attempt regardless of delivery so an unreachable
        # admin is retried at most once per period, not every scan.
        msg = await send(app, uid, _REMINDER_TEXT)
        next_state[key] = now
        if msg is not None:
            sent += 1
        else:
            failed += 1

    await _save_reminder_state(next_state)
    log.info(
        "bot5 reminder: pass complete sent=%s failed=%s connected=%s tracked=%s",
        sent,
        failed,
        connected,
        len(next_state),
    )


async def _reminder_loop(app: Client) -> None:
    await asyncio.sleep(_REMINDER_STARTUP_DELAY)
    while True:
        try:
            await _send_section_reminders(app)
        except Exception as err:
            log.warning("bot5 reminder: round failed: %s", err)
        await asyncio.sleep(_REMINDER_CHECK_INTERVAL)


async def on_started(app: Client) -> None:
    """Wire up the panel bot's post-start background work.

    Both the one-time live announcement and the recurring section reminder DM
    real role-holders, so they only run in a deployment — never in dev, where a
    restart would spam admins.
    """
    if not IS_DEPLOYMENT:
        log.info("bot5: not a deployment; skipping live announcement + reminders")
        return
    # Recurring nudge for section-owning admins who never connected a channel.
    asyncio.create_task(_reminder_loop(app))
    # One-time per-deployment 'panel bot is live' announcement.
    await _run_live_announcement(app)
