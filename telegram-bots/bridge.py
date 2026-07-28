"""Internal HTTP bridge between the Node dashboard/API and this Python service.

Direction 1 (Python -> Node): incoming channel posts are pushed to the Node
auto-verify SSE bus via `publish_sms_event`.

Direction 2 (Node -> Python): the dashboard's routes call these endpoints to
operate on the JSON store (which Python now owns) and to make the bots send
DMs. Every endpoint is guarded by a shared secret header.

All endpoints bind to loopback only.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import aiohttp
from aiohttp import web
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import store
from config import BOT3_TOKEN, BRIDGE_SECRET, BRIDGE_SOCKET, NODE_API_BASE

log = logging.getLogger("zenin.bridge")

_bot1: Optional[Client] = None
_bot3: Optional[Client] = None
_notify_bot: Optional[Client] = None
_panel_bot: Optional[Client] = None
_session: Optional[aiohttp.ClientSession] = None


def set_clients(bot1: Optional[Client], bot3: Optional[Client],
                notify_bot: Optional[Client] = None,
                panel_bot: Optional[Client] = None) -> None:
    global _bot1, _bot3, _notify_bot, _panel_bot
    _bot1 = bot1
    _bot3 = bot3
    _notify_bot = notify_bot
    _panel_bot = panel_bot


def get_account_bot() -> Optional[Client]:
    """Return the account-bot (bot3) Pyrogram client, or None if it isn't up.

    bot2 (member checker) uses this to broadcast owner messages to everyone who
    started the account bot, since those users only have a chat with that bot.
    """
    return _bot3


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─── Python -> Node ─────────────────────────────────────────────────────────
async def publish_sms_event(payload: dict[str, Any]) -> None:
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    url = f"{NODE_API_BASE}/auto-verify/internal/publish"
    try:
        async with _session.post(url, json=payload, headers={"x-internal-secret": BRIDGE_SECRET},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status >= 300:
                log.warning("publish_sms_event non-2xx: %s", r.status)
    except Exception as err:
        log.warning("publish_sms_event failed: %s", err)


# ─── bot DMs (used by Node -> Python endpoints) ─────────────────────────────
async def _notify_token_revoked(chat_id: int) -> None:
    if _bot3 is None:
        return
    try:
        await _bot3.send_message(
            chat_id,
            "⚠️ <b>Your token has been revoked.</b>\n\n"
            "Your previous token is no longer valid and the bot is now <b>INACTIVE</b>.\n\n"
            "🔑 Please connect the bot with the new active token from your Zenin panel to keep using Auto Verify.",
        )
    except Exception as err:
        log.warning("notify_token_revoked failed chat=%s: %s", chat_id, err)


async def _notify_sms_result(chat_id: int, p: dict[str, Any]) -> dict[str, Any]:
    """DM the user the SMS send-result and report whether the DM was delivered.

    Returns the real Telegram send outcome so callers (and the live test) can
    confirm, without a human, that the account bot actually reached the user:
      {"delivered": bool, "messageId": int | None, "error": str | None}
    """
    if _bot3 is None:
        return {"delivered": False, "messageId": None, "error": "account bot unavailable"}
    try:
        ok = bool(p.get("ok"))
        header = "✅ <b>SMS SEND SUCCESSFUL</b>" if ok else "❌ <b>SMS SEND FAILED</b>"
        blocks: list[str] = [header]
        if p.get("deviceNumber") or p.get("deviceId"):
            dev = f"<b>{p.get('deviceNumber') or 1} DEVICE</b>"
            if p.get("deviceId"):
                dev += f"\n<code>{_esc(str(p['deviceId']))}</code>"
            blocks.append(dev)
        if p.get("simSlot"):
            blocks.append(f"🔢 SIM {p['simSlot']}")
        if p.get("from_number"):
            blocks.append(f"📱 From: <code>{_esc(str(p['from_number']))}</code>")
        blocks.append(f"📞 To: <code>{_esc(str(p.get('to', '')))}</code>")
        if p.get("message"):
            blocks.append(f"💬 MESSAGE: <code>{_esc(str(p['message']))}</code>")
        if not ok and p.get("error"):
            blocks.append(f"⚠️ {_esc(str(p['error']))}")
        msg = await _bot3.send_message(chat_id, "\n\n".join(blocks))
        return {"delivered": True, "messageId": getattr(msg, "id", None), "error": None}
    except Exception as err:
        log.warning("notify_sms_result failed chat=%s: %s", chat_id, err)
        return {"delivered": False, "messageId": None, "error": str(err)}


def _build_reply_markup(
    buttons: list[dict[str, Any]] | None,
) -> Optional[InlineKeyboardMarkup]:
    """Convert a flat list of button dicts into an InlineKeyboardMarkup.

    Each button may have:
      • ``cb``  — callback_data string (action button handled by the notify bot)
      • ``url`` — URL string (external link button)
    Buttons with neither key are silently skipped.
    """
    if not buttons:
        return None
    row: list[InlineKeyboardButton] = []
    for b in buttons:
        text = str(b.get("text", "")).strip()
        if not text:
            continue
        cb = b.get("cb")
        url = b.get("url")
        if cb:
            row.append(InlineKeyboardButton(text, callback_data=str(cb)))
        elif url:
            row.append(InlineKeyboardButton(text, url=str(url)))
    if not row:
        return None
    return InlineKeyboardMarkup([row])


async def _post_to_channel(
    channel_chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> dict[str, Any]:
    """Post `text` OUT to a user's bound channel via the notification bot.

    This is the reverse direction of the Auto Verify bridge: instead of pulling
    channel posts INTO the dashboard, the dashboard forwards its own
    notifications OUT to the channel the key is bound to. Uses the dedicated
    notification bot when configured, otherwise falls back to bot1.

    The chosen bot must be a member/admin of the target channel for the post to
    land; otherwise Telegram rejects the send and the error is returned.
    """
    bot = _notify_bot or _bot3
    if bot is None:
        return {"ok": False, "messageId": None, "error": "notify bot unavailable"}
    try:
        msg = await bot.send_message(
            channel_chat_id,
            text,
            reply_markup=reply_markup,
        )
        return {"ok": True, "messageId": getattr(msg, "id", None), "error": None}
    except Exception as err:
        log.warning("post_to_channel failed chat=%s: %s", channel_chat_id, err)
        return {"ok": False, "messageId": None, "error": str(err)}


async def _panel_send(
    targets: list[dict[str, Any]], summary: str,
    details_filename: str, details_text: str,
) -> list[dict[str, Any]]:
    """Forward a panel summary + details file to each section channel.

    `targets` is a list of {chatId, tgUid, role}. For each target the panel bot
    posts the text summary and then uploads the details file. The per-target
    outcome (ok / messageId / error) is returned so the Node side can persist an
    auditable backup row even when one channel post fails.
    """
    results: list[dict[str, Any]] = []
    if _panel_bot is None:
        for t in targets:
            results.append({
                "tgUid": t.get("tgUid"), "role": t.get("role"),
                "chatId": t.get("chatId"), "ok": False,
                "messageId": None, "error": "panel bot unavailable",
            })
        return results

    import io

    for t in targets:
        chat_id = t.get("chatId")
        out = {
            "tgUid": t.get("tgUid"), "role": t.get("role"),
            "chatId": chat_id, "ok": False, "messageId": None, "error": None,
        }
        if chat_id is None:
            out["error"] = "missing chatId"
            results.append(out)
            continue
        try:
            msg = await _panel_bot.send_message(
                int(chat_id), summary, parse_mode=ParseMode.HTML
            )
            out["messageId"] = getattr(msg, "id", None)
            if details_text:
                buf = io.BytesIO(details_text.encode("utf-8"))
                buf.name = details_filename or "panel.txt"
                await _panel_bot.send_document(int(chat_id), buf, parse_mode=ParseMode.DISABLED)
            out["ok"] = True
        except Exception as err:
            log.warning("panel_send failed chat=%s: %s", chat_id, err)
            out["error"] = str(err)
        results.append(out)
    return results


# ─── HTTP handlers (Node -> Python) ─────────────────────────────────────────
def _auth_ok(request: web.Request) -> bool:
    return request.headers.get("x-internal-secret") == BRIDGE_SECRET


@web.middleware
async def _auth_mw(request: web.Request, handler):
    if request.path == "/internal/health":
        return await handler(request)
    if not _auth_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def _h_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _h_mint_token(request: web.Request) -> web.Response:
    return web.json_response({"token": store.mint_token()})


async def _h_token_status(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    return web.json_response(store.get_token_status(token))


async def _h_revoke(request: web.Request) -> web.Response:
    body = await request.json()
    revoked, disconnected = store.revoke_token(str(body.get("token", "")))
    if disconnected is not None:
        await _notify_token_revoked(disconnected)
    return web.json_response({"revoked": revoked, "disconnectedChatId": disconnected})


async def _h_connected_users(request: web.Request) -> web.Response:
    return web.json_response({"users": store.list_connected_users()})


async def _h_user_by_key(request: web.Request) -> web.Response:
    key = request.query.get("key", "")
    found = store.find_user_by_key(key)
    return web.json_response({"user": found})


async def _h_key_admin_status(request: web.Request) -> web.Response:
    """Check whether a key exists in the store AND the bot is still admin
    in the channel linked to that key.

    Returns: {exists, isAdmin, channelTitle, error?}
    - exists=False  → key unknown (user deleted it, or never existed)
    - isAdmin=False → key exists but bot was removed from admin / channel
    - isAdmin=True  → key is live, bot is admin right now  → ACTIVE
    """
    key = request.rel_url.query.get("key", "").strip()
    if not key:
        return web.json_response({"exists": False, "isAdmin": False})

    user = store.find_user_by_key(key)
    if not user:
        return web.json_response({"exists": False, "isAdmin": False})

    channel_chat_id = user.get("channelChatId")
    channel_title = user.get("channelTitle", "")

    if not channel_chat_id:
        return web.json_response({"exists": True, "isAdmin": False,
                                  "channelTitle": channel_title,
                                  "error": "no_channel"})

    # Use direct Telegram Bot API HTTP call — bypasses Pyrogram peer cache
    # so this works correctly right after every container restart.
    try:
        url = (
            f"https://api.telegram.org/bot{BOT3_TOKEN}/getChatMember"
            f"?chat_id={int(channel_chat_id)}&user_id={BOT3_TOKEN.split(':')[0]}"
        )
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=7)) as resp:
                data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "tg_api_error"))
        status = (data.get("result") or {}).get("status", "")
        is_admin = status in ("administrator", "creator")
        return web.json_response({"exists": True, "isAdmin": is_admin,
                                  "channelTitle": channel_title})
    except Exception as err:
        log.warning("key_admin_status check failed key=%s: %s", key, err)
        # Return isAdmin=True when the check itself fails (network/timeout) so a
        # transient error never incorrectly marks an active key as inactive.
        return web.json_response({"exists": True, "isAdmin": True,
                                  "channelTitle": channel_title,
                                  "error": str(err)})


async def _h_notify_sms_result(request: web.Request) -> web.Response:
    body = await request.json()
    chat_id = body.get("userChatId")
    if chat_id is None:
        return web.json_response({"ok": True, "delivered": False, "messageId": None,
                                  "error": "missing userChatId"})
    result = await _notify_sms_result(int(chat_id), body)
    # `ok` stays True for backward compatibility (the call itself succeeded);
    # `delivered` reflects whether bot1 actually reached the user.
    return web.json_response({"ok": True, **result})


async def _h_forward_notification(request: web.Request) -> web.Response:
    body = await request.json()
    key = str(body.get("key", "")).strip()
    text = str(body.get("text", ""))
    if not key or not text:
        return web.json_response(
            {"ok": False, "messageId": None, "error": "missing key or text"},
            status=400,
        )
    found = store.find_user_by_key(key)
    if not found:
        return web.json_response(
            {"ok": False, "messageId": None, "error": "key not connected"}
        )
    raw_buttons = body.get("buttons")
    reply_markup = _build_reply_markup(raw_buttons if isinstance(raw_buttons, list) else None)
    result = await _post_to_channel(int(found["channelChatId"]), text, reply_markup)
    return web.json_response(result)


async def _h_get_chat(request: web.Request) -> web.Response:
    import membership
    body = await request.json()
    ref = body.get("ref")
    chat = await membership.get_chat(ref)
    if chat is None:
        return web.json_response({"chat": None})
    return web.json_response({"chat": {
        "id": getattr(chat, "id", None),
        "type": getattr(getattr(chat, "type", None), "name", None),
        "title": getattr(chat, "title", None),
        "username": getattr(chat, "username", None),
        "inviteLink": getattr(chat, "invite_link", None),
    }})


async def _h_panel_send(request: web.Request) -> web.Response:
    body = await request.json()
    targets = body.get("targets") or []
    summary = str(body.get("summary", ""))
    details_filename = str(body.get("detailsFilename", "panel.txt"))
    details_text = str(body.get("detailsText", ""))
    if not isinstance(targets, list) or not summary:
        return web.json_response(
            {"results": [], "error": "missing targets or summary"}, status=400
        )
    results = await _panel_send(targets, summary, details_filename, details_text)
    return web.json_response({"results": results})


async def _h_account_send(request: web.Request) -> web.Response:
    """Send a plain HTML message to a specific chat ID.

    Tries bot3 (Account bot) first.  If bot3 is unavailable or the peer
    hasn't started it yet (PEER_ID_INVALID / USER_IS_BOT), falls back to
    bot1 (Auto Verify bot) — users who have linked a channel via bot1
    already have a conversation with it and can receive DMs from it.
    """
    body = await request.json()
    chat_id = body.get("chatId")
    text = str(body.get("text", ""))
    if not chat_id or not text:
        return web.json_response({"ok": False, "error": "missing chatId or text"}, status=400)

    cid = int(chat_id)

    # Try bot3 (Account / Panel bot) first
    if _bot3 is not None:
        try:
            await _bot3.send_message(cid, text, parse_mode=ParseMode.HTML)
            return web.json_response({"ok": True})
        except Exception as err:
            log.warning("account_send bot3 failed chat=%s: %s — trying bot1 fallback", cid, err)

    # Fallback: bot1 (Auto Verify bot) — user may have an existing conversation with it
    if _bot1 is not None:
        try:
            await _bot1.send_message(cid, text, parse_mode=ParseMode.HTML)
            return web.json_response({"ok": True})
        except Exception as err:
            log.warning("account_send bot1 fallback failed chat=%s: %s", cid, err)
            return web.json_response({"ok": False, "error": str(err)})

    return web.json_response({"ok": False, "error": "no account bot available"})


async def _panel_send_apk(
    targets: list[dict[str, Any]], apk_base64: str, apk_filename: str,
) -> list[dict[str, Any]]:
    """Forward an APK file (base64-encoded) to each section channel.

    Sends the raw APK as a Telegram document. Used when Firebase extraction
    fails so section owners receive the original file for manual inspection.
    """
    import base64
    import io

    results: list[dict[str, Any]] = []

    if _panel_bot is None:
        for t in targets:
            results.append({
                "tgUid": t.get("tgUid"), "role": t.get("role"),
                "chatId": t.get("chatId"), "ok": False,
                "messageId": None, "error": "panel bot unavailable",
            })
        return results

    try:
        apk_bytes = base64.b64decode(apk_base64)
    except Exception as err:
        log.warning("panel_send_apk: base64 decode failed: %s", err)
        for t in targets:
            results.append({
                "tgUid": t.get("tgUid"), "role": t.get("role"),
                "chatId": t.get("chatId"), "ok": False,
                "messageId": None, "error": "invalid base64",
            })
        return results

    for t in targets:
        chat_id = t.get("chatId")
        out: dict[str, Any] = {
            "tgUid": t.get("tgUid"), "role": t.get("role"),
            "chatId": chat_id, "ok": False, "messageId": None, "error": None,
        }
        if chat_id is None:
            out["error"] = "missing chatId"
            results.append(out)
            continue
        try:
            buf = io.BytesIO(apk_bytes)
            buf.name = apk_filename or "app.apk"
            msg = await _panel_bot.send_document(
                int(chat_id), buf,
                caption="📦 <b>Failed APK</b> — Firebase extraction failed",
                parse_mode=ParseMode.HTML,
            )
            out["messageId"] = getattr(msg, "id", None)
            out["ok"] = True
        except Exception as err:
            log.warning("panel_send_apk failed chat=%s: %s", chat_id, err)
            out["error"] = str(err)
        results.append(out)
    return results


async def _h_panel_send_apk(request: web.Request) -> web.Response:
    body = await request.json()
    targets = body.get("targets") or []
    apk_base64 = str(body.get("apkBase64", ""))
    apk_filename = str(body.get("apkFileName", "app.apk"))
    if not isinstance(targets, list) or not apk_base64:
        return web.json_response(
            {"results": [], "error": "missing targets or apkBase64"}, status=400
        )
    results = await _panel_send_apk(targets, apk_base64, apk_filename)
    return web.json_response({"results": results})


async def _h_panel_bot_username(request: web.Request) -> web.Response:
    if _panel_bot is None:
        return web.json_response({"username": None})
    try:
        me = await _panel_bot.get_me()
        return web.json_response({"username": getattr(me, "username", None)})
    except Exception as err:
        log.warning("panel_bot_username failed: %s", err)
        return web.json_response({"username": None})


def _make_app() -> web.Application:
    app = web.Application(middlewares=[_auth_mw])
    app.router.add_get("/internal/health", _h_health)
    app.router.add_post("/internal/mint-token", _h_mint_token)
    app.router.add_get("/internal/token-status", _h_token_status)
    app.router.add_post("/internal/revoke", _h_revoke)
    app.router.add_get("/internal/connected-users", _h_connected_users)
    app.router.add_get("/internal/user-by-key", _h_user_by_key)
    app.router.add_get("/internal/key-admin-status", _h_key_admin_status)
    app.router.add_post("/internal/notify-sms-result", _h_notify_sms_result)
    app.router.add_post("/internal/forward-notification", _h_forward_notification)
    app.router.add_post("/internal/get-chat", _h_get_chat)
    app.router.add_post("/internal/panel-send", _h_panel_send)
    app.router.add_post("/internal/panel-send-apk", _h_panel_send_apk)
    app.router.add_post("/internal/account-send", _h_account_send)
    app.router.add_get("/internal/panel-bot-username", _h_panel_bot_username)
    return app


async def start_bridge() -> web.AppRunner:
    runner = web.AppRunner(_make_app())
    await runner.setup()
    # Bind a Unix domain socket (local filesystem only — never a TCP port).
    os.makedirs(os.path.dirname(BRIDGE_SOCKET), exist_ok=True)
    try:
        os.unlink(BRIDGE_SOCKET)  # clear any stale socket from a previous run
    except FileNotFoundError:
        pass
    site = web.UnixSite(runner, BRIDGE_SOCKET)
    await site.start()
    try:
        os.chmod(BRIDGE_SOCKET, 0o600)
    except OSError:
        pass
    log.info("bridge listening on unix socket %s", BRIDGE_SOCKET)
    return runner
