"""Membership verification via bot2's credentials.

bot2 (Member Checker) is the bot added as admin in the required channels, so it
is the only client that can read membership reliably. bot1 and bot3 both call
through here. Faithful port of `verifyMembershipViaBot2` / `checkMembership`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Union

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant

import store

log = logging.getLogger("zenin.membership")

# Set by main.py at startup to bot2's running Client.
_bot2: Optional[Client] = None


def set_bot2_client(client: Client) -> None:
    global _bot2
    _bot2 = client


_LEFT = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}


async def get_chat_member_status(chat_id: Union[int, str], user_id: int) -> Optional[str]:
    """Return a Bot-API-style status string, or None if the call failed."""
    if _bot2 is None:
        return None
    try:
        m = await _bot2.get_chat_member(chat_id, user_id)
        return _status_str(m.status)
    except UserNotParticipant:
        return "left"
    except Exception as err:
        log.warning("get_chat_member failed chat=%s user=%s: %s", chat_id, user_id, err)
        return None


def _status_str(status: ChatMemberStatus) -> str:
    return {
        ChatMemberStatus.OWNER: "creator",
        ChatMemberStatus.ADMINISTRATOR: "administrator",
        ChatMemberStatus.MEMBER: "member",
        ChatMemberStatus.RESTRICTED: "restricted",
        ChatMemberStatus.LEFT: "left",
        ChatMemberStatus.BANNED: "kicked",
    }.get(status, "member")


async def get_chat(ref: Union[int, str]) -> Optional[Any]:
    if _bot2 is None:
        return None
    try:
        return await _bot2.get_chat(ref)
    except Exception as err:
        log.warning("get_chat failed ref=%s: %s", ref, err)
        return None


async def verify_membership(user_id: int) -> dict[str, Any]:
    """Mirror of checkMembership in the old bot2.ts.

    Returns {configured, missing, joined, unverifiable} where each list holds
    RequiredChannel dicts from the JSON store.
    """
    channels = store.required_channels()
    if not channels:
        return {"configured": False, "missing": [], "joined": [], "unverifiable": []}

    missing: list[dict] = []
    joined: list[dict] = []
    unverifiable: list[dict] = []

    for ch in channels:
        chat_id = ch.get("chatId")
        if isinstance(chat_id, str) and chat_id.startswith("pending:"):
            unverifiable.append(ch)
            continue
        status = await get_chat_member_status(chat_id, user_id)
        if status is None:
            unverifiable.append(ch)
            continue
        if status in ("left", "kicked"):
            missing.append(ch)
        else:
            joined.append(ch)

    return {"configured": True, "missing": missing, "joined": joined, "unverifiable": unverifiable}
