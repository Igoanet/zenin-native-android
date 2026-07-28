"""Thin, rate-limited send/edit helpers over Pyrogram.

Centralises HTML parse mode, link-preview suppression, inline-keyboard
building, gentle global pacing, and FloodWait handling so the individual bot
modules stay declarative and never hammer Telegram.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Optional, Sequence, Union

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger("zenin.sender")

# A button is (text, kind, value) where kind is "cb" (callback_data) or "url".
Button = tuple[str, str, str]
Rows = Sequence[Sequence[Button]]

# Gentle global pacing: never exceed ~25 messages/second across all bots.
_MIN_INTERVAL = 0.04
_last_send = 0.0
_lock = asyncio.Lock()


def btn(text: str, *, cb: Optional[str] = None, url: Optional[str] = None) -> Button:
    if url is not None:
        return (text, "url", url)
    return (text, "cb", cb or "")


def kb(rows: Optional[Rows]) -> Optional[InlineKeyboardMarkup]:
    if not rows:
        return None
    out: list[list[InlineKeyboardButton]] = []
    for row in rows:
        line: list[InlineKeyboardButton] = []
        for text, kind, value in row:
            if kind == "url":
                line.append(InlineKeyboardButton(text, url=value))
            else:
                line.append(InlineKeyboardButton(text, callback_data=value))
        if line:
            out.append(line)
    return InlineKeyboardMarkup(out) if out else None


async def _pace() -> None:
    global _last_send
    async with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_send)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_send = time.monotonic()


async def send(
    client: Client,
    chat_id: Union[int, str],
    text: str,
    rows: Optional[Rows] = None,
) -> Optional[Message]:
    await _pace()
    try:
        return await client.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=kb(rows),
        )
    except FloodWait as e:
        await asyncio.sleep(int(getattr(e, "value", 1)) + 1)
        try:
            return await client.send_message(
                chat_id, text, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True, reply_markup=kb(rows),
            )
        except Exception as err:
            log.warning("send retry failed chat=%s: %s", chat_id, err)
            return None
    except Exception as err:
        log.warning("send failed chat=%s: %s", chat_id, err)
        return None


async def edit(
    client: Client,
    chat_id: Union[int, str],
    message_id: int,
    text: str,
    rows: Optional[Rows] = None,
) -> Optional[Message]:
    await _pace()
    try:
        return await client.edit_message_text(
            chat_id,
            message_id,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=kb(rows),
        )
    except MessageNotModified:
        return None
    except FloodWait as e:
        await asyncio.sleep(int(getattr(e, "value", 1)) + 1)
        return None
    except Exception as err:
        log.warning("edit failed chat=%s msg=%s: %s", chat_id, message_id, err)
        return None


async def send_dashboard_shortcut(
    client: Client,
    chat_id: Union[int, str],
    dashboard_url: str,
    caption: str = "🌐 <b>Open your Zenin Dashboard</b>\n\nDouble-click the file to open the panel in your browser.",
) -> None:
    """Send a Windows .url shortcut file that opens the dashboard when double-clicked."""
    content = f"[InternetShortcut]\r\nURL={dashboard_url}\r\n"
    file_bytes = io.BytesIO(content.encode("utf-8"))
    file_bytes.name = "Zenin Dashboard.url"
    await _pace()
    try:
        await client.send_document(
            chat_id,
            file_bytes,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    except FloodWait as e:
        await asyncio.sleep(int(getattr(e, "value", 1)) + 1)
        try:
            file_bytes.seek(0)
            await client.send_document(
                chat_id,
                file_bytes,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        except Exception as err:
            log.warning("send_dashboard_shortcut retry failed chat=%s: %s", chat_id, err)
    except Exception as err:
        log.warning("send_dashboard_shortcut failed chat=%s: %s", chat_id, err)
