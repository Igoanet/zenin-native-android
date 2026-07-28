"""BOT 3 — Account / Panel Access (Pyrogram, bot mode).

Faithful port of bot3.ts. Three concerns:

  * Onboarding new users via channel gate + single-use access keys.
  * Issuing fresh panel credentials (`/get_credential`).
  * Administering channels, keys, and roles (chat-id-driven menus).

Shared DB tables live in Postgres (users, access_keys, required_channels,
app_settings, role_events). Password hashing matches the Node dashboard so
credentials minted here log in on the panel.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp
import psycopg
from pyrogram import Client, filters
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.handlers import CallbackQueryHandler, MessageHandler

import db
import store
from auth import hash_password
from config import (
    APK_URL,
    DASHBOARD_URL,
    HARDCODED_MANAGEMENT_ID,
    IS_DEPLOYMENT,
    MAX_OWNERS,
    OWNER_CAP_LOCK_KEY,
)
import membership
from membership import get_chat_member_status
from sender import btn, edit, send, send_dashboard_shortcut

log = logging.getLogger("zenin.bot3")

BOT3_NAME = "Zenin Panel Access"
CB_CANCEL = "menu"

# In-memory per-user dialog state. Same lifetime as the process (which is fine
# for a single-instance Python service).
_dialogs: dict[int, dict[str, Any]] = {}


def _get_dialog(uid: int) -> dict[str, Any]:
    return _dialogs.get(uid, {"kind": "idle"})


def _set_dialog(uid: int, state: dict[str, Any]) -> None:
    if state.get("kind") == "idle":
        _dialogs.pop(uid, None)
    else:
        _dialogs[uid] = state


# ─── Generators ───────────────────────────────────────────────────────────
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PWD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


# Panel User IDs start at 7 digits (≈9M ids) for backward compatibility with
# existing accounts. As collisions appear the generator widens the id space one
# digit at a time (each digit is a 10x larger address space), so a growing user
# base can't realistically exhaust it before a free id is found.
_USER_ID_DIGITS = 7
_USER_ID_MAX_DIGITS = 12
_USER_ID_ATTEMPTS = 50
_USER_ID_WIDEN_EVERY = 5
# Number of collisions to tolerate quietly before logging that a free id only
# turned up after burning several retries. Keeps normal (zero/one collision)
# generation silent while surfacing rising saturation as early warning.
_USER_ID_RETRY_LOG_THRESHOLD = 3


def gen_user_id(digits: int = _USER_ID_DIGITS) -> str:
    first = secrets.randbelow(9) + 1
    rest = "".join(str(secrets.randbelow(10)) for _ in range(digits - 1))
    return f"{first}{rest}"


def id_address_space(digits: int = _USER_ID_DIGITS) -> int:
    """Number of distinct ids at a given digit width.

    Matches ``gen_user_id``: the first digit is 1-9 (9 options) and each
    remaining digit is 0-9, so the space is ``9 * 10**(digits - 1)``.
    """
    return 9 * (10 ** (digits - 1))


def gen_password() -> str:
    return "".join(secrets.choice(_PWD_ALPHABET) for _ in range(12))


def gen_key_code() -> str:
    s = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(8))
    return f"ZN-{s[:4]}-{s[4:]}"


async def _generate_unique_user_id() -> str:
    digits = _USER_ID_DIGITS
    for attempt in range(_USER_ID_ATTEMPTS):
        candidate = gen_user_id(digits)
        existing = await db.get_user_by_user_id(candidate)
        if not existing:
            # Normal generation lands a free id on the first try or two; only
            # log once we've burned more than a small number of retries, so the
            # rate of these lines tracks rising collision pressure and gives
            # admins early warning before the friendly-failure path is hit.
            if attempt >= _USER_ID_RETRY_LOG_THRESHOLD:
                log.warning(
                    "bot3: user id pressure: found free %d-digit id after %d collision(s)",
                    digits,
                    attempt,
                )
            return candidate
        # Every cluster of collisions widens the id space by one digit (10x the
        # addressable range), so exhaustion stays vanishingly unlikely even as
        # the user base grows. If it still can't find a free id, the caller
        # surfaces the existing friendly "please try again" failure.
        if digits < _USER_ID_MAX_DIGITS and (attempt + 1) % _USER_ID_WIDEN_EVERY == 0:
            widened_to = digits + 1
            log.warning(
                "bot3: user id space saturating at %d digits after %d collision(s); "
                "widening to %d digits",
                digits,
                attempt + 1,
                widened_to,
            )
            digits = widened_to
    raise RuntimeError("could not generate unique user id")


# ─── Saturation broadcast ─────────────────────────────────────────────────
# Percentage thresholds at which a saturation alert is broadcast to admins.
# Each value is a fill percentage (0–100) of the current-digit-width address
# space. The list must be sorted ascending.
_SATURATION_ALERT_BANDS: list[int] = [50, 75, 90, 95, 99]


def _effective_thresholds() -> list[int]:
    """Return the active saturation alert band list (percent, ascending)."""
    return _SATURATION_ALERT_BANDS


async def _saturation_alert_recipients() -> list[int]:
    """Return sorted Telegram UIDs that should receive saturation alerts.

    Includes every user with a management or owner role plus the hardcoded
    management ID, deduped.
    """
    rows = await db.list_users_by_roles(("management", "owner"))
    uids: set[int] = {r["tg_uid"] for r in rows if r.get("tg_uid")}
    uids.add(HARDCODED_MANAGEMENT_ID)
    return sorted(uids)


async def _broadcast_saturation_alert(app: Any, ratio: float) -> None:
    """Send a saturation alert DM to all admin recipients.

    *ratio* is the fractional fill of the current id-digit address space
    (e.g. 0.91 means 91 % full). The message always shows the complete band
    list so recipients can see at a glance how close the next threshold is.
    """
    bands = _effective_thresholds()
    pct = ratio * 100.0
    next_band = next((b for b in bands if b > pct), None)
    bands_str = ", ".join(f"{b}%" for b in bands)
    next_str = f"{next_band}%" if next_band is not None else "none — all bands crossed"
    text = (
        "\u26a0\ufe0f <b>User ID space saturation alert</b>\n\n"
        f"Current fill: {pct:.1f}%\n"
        f"Alert bands: {bands_str}\n"
        f"Next alert at: {next_str}"
    )
    recipients = await _saturation_alert_recipients()
    for uid in recipients:
        try:
            await send(app, uid, text)
        except Exception:
            log.exception("bot3: saturation alert delivery failed for uid=%s", uid)


# ─── Roles / helpers ──────────────────────────────────────────────────────
ROLE_LABELS = {
    "management": "Management",
    "owner": "Owner",
    "dev_admin": "Dev Admin",
    "base_admin": "Base Admin",
    "user": "User",
}
ROLE_EMOJI = {
    "management": "🛠️",
    "owner": "👑",
    "dev_admin": "⚙️",
    "base_admin": "🔧",
    "user": "👤",
}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def role_emoji(role: str) -> str:
    return ROLE_EMOJI.get(role, "")


def _esc(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_role(s: str) -> Optional[str]:
    t = s.strip().lower()
    if t == "user":
        return "user"
    if t in ("base_admin", "base-admin", "baseadmin"):
        return "base_admin"
    if t in ("dev_admin", "dev-admin", "devadmin"):
        return "dev_admin"
    if t == "owner":
        return "owner"
    return None


def parse_duration(s: str) -> Optional[dict[str, Any]]:
    """Parse a duration string into {expires_at, label}.
    Supports: unlimited/infinite/none/0, Nm, Nh, Nd, Nmo.
    1 month = 28 days. Returns None for bad input.
    """
    t = s.strip().lower()
    if t in ("unlimited", "infinite", "none", "0"):
        return {"expires_at": None, "label": "Unlimited"}
    # months: e.g. 1mo, 2mo
    m = re.match(r"^(\d+)mo$", t)
    if m:
        n = int(m.group(1))
        if n <= 0:
            return None
        secs = n * 28 * 86400
        expires = datetime.now(timezone.utc).timestamp() + secs
        label = f"{n} month{'' if n == 1 else 's'} ({n * 28} days)"
        return {"expires_at": datetime.fromtimestamp(expires, tz=timezone.utc), "label": label}
    # minutes / hours / days
    m = re.match(r"^(\d+)([mhd])$", t)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0:
        return None
    unit = m.group(2)
    secs = {"m": 60, "h": 3600, "d": 86400}[unit]
    expires = datetime.now(timezone.utc).timestamp() + n * secs
    unit_label = {"m": "minute", "h": "hour", "d": "day"}[unit]
    label = f"{n} {unit_label}{'' if n == 1 else 's'}"
    return {"expires_at": datetime.fromtimestamp(expires, tz=timezone.utc), "label": label}


def role_privilege(r: str) -> int:
    if r == "management":
        return 4
    if r == "owner":
        return 3
    if r == "user":
        return 1
    return 2


def can_create_key(creator_role: str, target_role: str) -> bool:
    if target_role == "management":
        return False
    if creator_role == "management":
        return False
    if creator_role == "owner":
        return target_role != "owner"
    if creator_role in ("dev_admin", "base_admin"):
        return target_role == "user"
    return False


def can_promote(actor_role: str, target_role: str) -> bool:
    if target_role == "management":
        return False
    if actor_role == "management":
        return target_role == "owner"
    if actor_role == "owner":
        return target_role != "owner"
    if actor_role in ("dev_admin", "base_admin"):
        return target_role == "user"
    return False


def can_demote(actor_role: str, target_current_role: str) -> bool:
    if target_current_role in ("management", "user"):
        return False
    if actor_role == "management":
        return True
    if actor_role == "owner":
        return target_current_role != "owner"
    return False


def placeholder_name(tg_uid: int) -> str:
    return f"User {tg_uid}"


def is_placeholder_name(name: str) -> bool:
    return bool(re.match(r"^User \d{5,15}$", name or ""))


def _ts_utc(dt: Any) -> str:
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    return str(dt)


def _expired(dt: Any) -> bool:
    if not dt:
        return False
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc) <= datetime.now(timezone.utc)
    return False


def _time_remaining(dt: Any) -> str:
    if not dt:
        return "♾ Never expires"
    if isinstance(dt, datetime):
        now = datetime.now(timezone.utc)
        exp = dt.astimezone(timezone.utc)
        if exp <= now:
            return "⛔ Expired"
        delta = exp - now
        total_secs = int(delta.total_seconds())
        days = total_secs // 86400
        hours = (total_secs % 86400) // 3600
        minutes = (total_secs % 3600) // 60
        if days > 0:
            return f"⏳ {days}d {hours}h {minutes}m remaining"
        if hours > 0:
            return f"⏳ {hours}h {minutes}m remaining"
        return f"⏳ {minutes}m remaining"
    return str(dt)


async def effective_role(chat_id: int) -> Optional[str]:
    if chat_id == HARDCODED_MANAGEMENT_ID:
        return "management"
    u = await db.get_user_by_tg_uid(chat_id)
    return u["role"] if u else None


async def access_status(chat_id: int) -> dict[str, Any]:
    """Resolve account-bot access for the gated bots (Auto Verify, Notification).

    Returns {'role': str | None, 'live': bool}:
      • role is None when the user has no Zenin account at all.
      • live is True when the account may use the gated bots right now.
        management / owner / dev_admin / base_admin are always live — they never
        need an access key. A plain ``user`` is live only while their redeemed
        access key has not expired.
    """
    if chat_id == HARDCODED_MANAGEMENT_ID:
        return {"role": "management", "live": True}
    u = await db.get_user_by_tg_uid(chat_id)
    if not u:
        return {"role": None, "live": False}
    role = u.get("role")
    live = True if role != "user" else not _expired(u.get("access_expires_at"))
    return {"role": role, "live": live}


async def count_owners() -> int:
    return await db.count_owners()


async def count_demotable_for(actor_role: str) -> int:
    rows = await db.fetchall(
        "SELECT tg_uid, role FROM users WHERE role IN ('owner','dev_admin','base_admin')"
    )
    return sum(
        1 for u in rows
        if u["tg_uid"] != HARDCODED_MANAGEMENT_ID and can_demote(actor_role, u["role"])
    )


async def get_support_contact() -> Optional[str]:
    sb = store.support_button()
    return sb.get("url") or None


def _support_line() -> str:
    sb = store.support_button()
    url = (sb.get("url") or "").strip()
    # Extract @username from t.me URLs; fall back to raw value if not a t.me link
    import re as _re
    m = _re.search(r"t\.me/([A-Za-z0-9_]+)", url)
    handle = f"@{m.group(1)}" if m else (url or "@support")
    return f"❓ Need help? Just send a message → {_esc(handle)}"


async def send_help(client: Client, chat_id: int) -> None:
    """Page 1 — Feature overview."""
    role = await effective_role(chat_id)
    is_admin = role in ("management", "owner", "dev_admin", "base_admin")
    is_owner_or_mgmt = role in ("management", "owner")
    has_panel = role in ("management", "owner", "dev_admin")

    account_section = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Your Account</b>\n\n"
        "🔐 <b>Get Credentials</b>\n"
        "Shows your panel User ID and current password. Always returns the "
        "same password so your session stays active. A password is generated "
        "on first tap if you haven't set one yet.\n\n"
        "🔁 <b>Change Password</b>\n"
        "Set a new panel login password. All existing sessions are signed out "
        "immediately.\n"
    )

    auto_verify_section = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "✅ <b>Auto Verify</b>\n\n"
        "Links your Telegram channel to the panel so SMS results are forwarded "
        "to you automatically. Connect your channel once and every send result "
        "— success or failure — appears here as a DM.\n"
        "• <b>Connect channel</b> — paste your channel's chat ID after adding "
        "this bot as admin there.\n"
        "• <b>Disconnect</b> — unlink the channel at any time.\n"
        "• <b>Dashboard shortcut</b> — sends a one-tap link to open the Zenin "
        "panel without typing credentials.\n"
    )

    notifications_section = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "📢 <b>Notifications</b>\n\n"
        "Creates per-category keys that route panel alerts to your Telegram "
        "channels. Each key is bound to one channel and one alert type.\n"
        "• 💳 <b>Transaction</b> — payment / top-up events\n"
        "• 🔓 <b>Login</b> — panel sign-in alerts\n"
        "• 🟢 <b>Online / Offline</b> — device status changes\n"
        "Limit: 1 key per alert type.\n"
    )

    admin_section = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "🛡 <b>Admin Tools</b>\n\n"
        "🔑 <b>Access Keys</b>\n"
        "Create single-use invite keys for new users. Each key carries a "
        "label, role, and duration (1 h – 56 days). The countdown starts when "
        "the key is claimed. You get a DM the moment someone redeems your key.\n"
        "Submenu: 🆕 New key · 📋 List keys · 🚫 Revoke key\n\n"
        "👥 <b>Access Users</b>\n"
        "Browse every user who claimed a key you created — name, @username, "
        "Telegram ID, User ID, role, and expiry date.\n\n"
        "🔍 <b>Find User</b>\n"
        "Look up any panel user by User ID. Returns name, Telegram ID, "
        "username, and role.\n\n"
        "⛔ <b>End Access</b>\n"
        "Immediately revoke a user's panel access. Accepts a Telegram ID, "
        "User ID, or key code. The user is notified and their session ends.\n"
    ) if is_admin else ""

    owner_section = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "👑 <b>Owner / Management</b>\n\n"
        "👑 <b>Manage Admins</b>\n"
        "View and demote existing admins. Each slot shows a live count.\n\n"
        "🛠 <b>Promote User</b>\n"
        "Change any user's role. Management → Owner; Owner → Base Admin or "
        "Dev Admin.\n"
    ) if is_owner_or_mgmt else ""

    panel_section = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "🗂 <b>Panel Bot</b>\n\n"
        "Connects a Telegram channel as your <b>section channel</b>. Every "
        "Firebase panel linked in the dashboard — plus any APK that fails "
        "Firebase extraction — is automatically forwarded there as a summary "
        "message and a details file.\n"
        "• Add this bot as admin in your channel first.\n"
        "• Then send the channel's chat ID here to register it.\n"
        "• You can update or remove the section channel at any time.\n"
    ) if has_panel else ""

    text = (
        "ℹ️ <b>Zenin Bot — Help</b>  <i>(1 / 2)</i>\n\n"
        "One bot handles everything: account access, auto-verify, "
        "notifications, and panel forwarding.\n\n"
        + account_section
        + auto_verify_section
        + notifications_section
        + admin_section
        + owner_section
        + panel_section +
        "\n━━━━━━━━━━━━━━━━━━━\n\n"
        f"{_support_line()}"
    )
    await send(client, chat_id, text, [
        [btn("📟 Commands →", cb="hlp:2")],
        [btn("⬅️ Back", cb=CB_CANCEL)],
    ])


async def send_help_commands(client: Client, chat_id: int) -> None:
    """Page 2 — Commands reference."""
    role = await effective_role(chat_id)
    is_admin = role in ("management", "owner", "dev_admin", "base_admin")
    is_owner_or_mgmt = role in ("management", "owner")

    base_cmds = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "🔰 <b>General</b>\n\n"
        "/start — open the main menu\n"
        "/help — show this help\n"
        "/me — your User ID, role, and access expiry\n"
        "/get_credential — fetch credentials without opening the menu\n"
        "/change_password — start a password change directly\n"
    )

    admin_cmds = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "🛡 <b>Admin</b>\n\n"
        "/newkey &lt;duration&gt; — create an access key\n"
        "  e.g. <code>/newkey 7d</code> · <code>/newkey 12h</code> · <code>/newkey 1mo</code>\n\n"
        "/keys — list your active keys\n\n"
        "/revokekey &lt;ZN-XXXX-XXXX&gt; — revoke an unclaimed key\n\n"
        "/end &lt;id&gt; — end a user's access immediately\n"
        "  accepts: Telegram ID · panel User ID · key code\n"
    ) if is_admin else ""

    promote_cmd = (
        "\n━━━━━━━━━━━━━━━━━━━\n"
        "👑 <b>Owner / Management</b>\n\n"
        "/promote &lt;userId&gt; &lt;role&gt; — change a user's role\n"
        "  e.g. <code>/promote 1234567 dev_admin</code>\n"
        "  roles: <code>owner</code> · <code>dev_admin</code> · <code>base_admin</code> · <code>user</code>\n"
    ) if is_owner_or_mgmt else ""

    text = (
        "📟 <b>Zenin Bot — Commands</b>  <i>(2 / 2)</i>\n\n"
        + base_cmds
        + admin_cmds
        + promote_cmd +
        "\n━━━━━━━━━━━━━━━━━━━\n\n"
        f"{_support_line()}"
    )
    await send(client, chat_id, text, [
        [btn("⬅️ Overview", cb="hlp:1")],
        [btn("⬅️ Back", cb=CB_CANCEL)],
    ])


# ─── Panel Bot (section channel registration) ─────────────────────────────
_PB_ROLES = ("management", "owner", "dev_admin")

_PB_ROLE_LABELS = {
    "management": "👑 Management",
    "owner": "🏆 Owner",
    "dev_admin": "🛠 Dev-Admin",
}


async def _pb_send_home(client: Client, chat_id: int) -> None:
    role = await effective_role(chat_id)
    if not role or role not in _PB_ROLES:
        await send(client, chat_id, "🚫 Not available for your role.")
        return

    role_label_str = _PB_ROLE_LABELS.get(role, role)
    section = await db.get_panel_section(chat_id)

    lines = [
        "🗂 <b>Panel Bot</b>",
        f"<i>Role: {role_label_str}</i>",
        "",
        "━━━━━━━━━━━━━━━━━━━",
        "📦 <b>What this does</b>",
        "• Forwards every linked Firebase panel to your section channel.",
        "• If an APK fails to extract Firebase, the APK file is also forwarded.",
        "• Each forward includes a summary + a details file.",
        "━━━━━━━━━━━━━━━━━━━",
    ]

    rows: list = []
    if section:
        lines.append(
            "\n🔗 <b>Section channel</b>\n"
            f"🏷 <b>{_esc(section.get('title') or section.get('chat_id'))}</b>\n"
            f"<code>{_esc(section.get('chat_id'))}</code>\n\n"
            "Every panel forwarded to your section lands in this channel."
        )
        rows.append([btn("🔄 Update Channel", cb="pb:reg")])
        rows.append([btn("🗑 Remove Channel", cb="pb:del")])
    else:
        lines.append(
            "\n🔗 <b>Section channel</b>\nNot connected. Connect a channel "
            "to receive your panel forwards."
        )
        rows.append([btn("➕ Connect Channel", cb="pb:reg")])

    if role in _PB_PROMO_ROLES:
        lines.append(
            "\n📣 <b>Promotion tools</b>\n"
            "Manage the required (mandatory-join) channels and broadcast to "
            "all users of this bot."
        )
        rows.append([btn("➕ Add Channel", cb="pb:ch_add")])
        rows.append([btn("📋 List Channels", cb="pb:ch_list"),
                     btn("➖ Remove Channel", cb="pb:ch_rm")])
        rows.append([btn("📢 Broadcast All", cb="pb:bc")])

    rows.append([btn("⬅️ Back", cb=CB_CANCEL)])
    await send(client, chat_id, "\n".join(lines), rows)


async def _pb_prompt_for_chat(client: Client, chat_id: int) -> None:
    await send(
        client,
        chat_id,
        "🔗 <b>Connect section channel</b>\n\n"
        "1. Add this bot as an <b>Administrator</b> in your channel or group.\n"
        "2. Send me the <b>chat ID</b> of that channel/group "
        "(e.g. <code>-1001234567890</code>).\n\n"
        "I'll verify I'm admin there, then bind it as your section channel.",
        [[btn("✖️ Cancel", cb="pb:home")]],
    )


async def _pb_handle_chat_input(client: Client, chat_id: int, text: str) -> None:
    cancel_kb = [[btn("✖️ Cancel", cb="pb:home")]]

    role = await effective_role(chat_id)
    if not role or role not in _PB_ROLES:
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
            log.warning("pb get_chat_member failed target=%s: %s", cid, err)

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
        await send(
            client, chat_id,
            f"❌ I'm not an admin in chat <code>{target_id}</code> yet "
            f"(status: <code>{status or 'unknown'}</code>).\n\n"
            "Please add this bot as <b>Administrator</b> there, then send the chat ID again.",
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

    try:
        await db.upsert_panel_section(chat_id, role, target_id, title)
    except Exception as err:
        log.warning("pb upsert_panel_section failed uid=%s: %s", chat_id, err)
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
    await _pb_send_home(client, chat_id)


# ─── Panel Bot promotion tools (channel setup + broadcast all) ──────────────
# Ported from bot2's owner menu: on the shared client bot3's handlers shadow
# bot2's, so this is the reachable home for the required-channel setup and the
# "Broadcast all" that were part of the Zenin promotion flow.
_PB_PROMO_ROLES = ("management", "owner")


async def _pb_add_channel(client: Client, chat_id: int, raw: str) -> None:
    import bot2 as _b2

    parts = raw.strip().split(None, 1)
    link = parts[0] if parts else ""
    title = parts[1].strip() if len(parts) > 1 else ""
    if not link:
        await send(client, chat_id,
                   "❌ Empty. Send the channel link/@handle, or tap Cancel.",
                   [[btn("✖️ Cancel", cb="pb:home")]])
        return

    ref = _b2._normalize_channel_ref(link)
    chat = None
    if ref is not None:
        try:
            chat = await membership.get_chat(ref)
        except Exception as err:
            log.warning("pb add channel: get_chat failed for %s: %s", ref, err)
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
        await send(client, chat_id,
                   f"✅ Added <b>{_esc(resolved_title)}</b>\n"
                   f"<b>Chat ID:</b> <code>{chat.id}</code>")
    else:
        # Couldn't resolve (likely a private invite). Store as pending — the
        # member-checker fills in the real chat id once it's channel admin.
        entry = {
            "chatId": f"pending:{int(time.time() * 1000)}",
            "title": f"⏳ {title or link}",
            "inviteLink": link,
        }
        store.add_required_channel(entry)
        await send(client, chat_id,
                   "✅ Saved as <b>pending</b>.\n\n"
                   "I couldn't resolve that link directly. Add the member-checker "
                   "bot as an <b>admin</b> to that channel and it will be linked "
                   "automatically. Membership checks only work once it's in the "
                   "channel.")
    await _pb_send_home(client, chat_id)


async def _pb_prepare_broadcast(client: Client, chat_id: int, message: Any) -> None:
    """Capture the admin's content, then ask for one-tap confirmation."""
    import bot2 as _b2

    content = await _b2._build_broadcast_content(client, message)
    if content is None:
        _set_dialog(chat_id, {"kind": "pb_awaiting_broadcast"})
        await send(client, chat_id,
                   "❌ I can only broadcast a text message, photo, file, voice "
                   "note, audio or video.\n\nSend one of those, or tap Cancel.",
                   [[btn("✖️ Cancel", cb="pb:home")]])
        return

    try:
        recipients = await _b2._broadcast_recipients()
    except Exception:
        log.exception("pb broadcast: failed to load recipients")
        _set_dialog(chat_id, {"kind": "idle"})
        await send(client, chat_id,
                   "❌ Couldn't load the full user list just now, so nothing was "
                   "sent. Please try again in a moment.")
        await _pb_send_home(client, chat_id)
        return

    if not recipients:
        _set_dialog(chat_id, {"kind": "idle"})
        await send(client, chat_id, "ℹ️ There are no users to broadcast to yet.")
        await _pb_send_home(client, chat_id)
        return

    _set_dialog(chat_id, {"kind": "pb_confirm_broadcast",
                          "content": content, "recipients": recipients})
    type_label = "text message" if content["type"] == "text" else content["kind"]
    await send(client, chat_id,
               "📢 <b>Confirm broadcast</b>\n\n"
               f"Content: <b>{_esc(type_label)}</b>\n"
               f"Recipients: <b>{len(recipients)}</b> (everyone who has opened this bot)\n\n"
               "Send it now?",
               [[btn(f"✅ Send to {len(recipients)}", cb="pb:bc_go")],
                [btn("❌ Cancel", cb="pb:bc_cancel")]])


async def _pb_run_broadcast(client: Client, chat_id: int,
                            content: dict[str, Any], recipients: list[int]) -> None:
    import bot2 as _b2
    from pyrogram.errors import FloodWait as _FloodWait

    await send(client, chat_id,
               f"📢 Broadcasting to <b>{len(recipients)}</b> users… "
               "I'll report back when it's done.")

    sent_count = 0
    failed_count = 0
    file_id: Optional[str] = None
    for uid in recipients:
        try:
            file_id = await _b2._deliver_broadcast(client, uid, content, file_id)
            sent_count += 1
        except _FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 1)) + 1)
            try:
                file_id = await _b2._deliver_broadcast(client, uid, content, file_id)
                sent_count += 1
            except Exception:
                log.warning("pb broadcast: delivery failed for uid=%s (after flood wait)", uid)
                failed_count += 1
        except Exception:
            log.warning("pb broadcast: delivery failed for uid=%s", uid)
            failed_count += 1
        await asyncio.sleep(0.05)

    await send(client, chat_id,
               "✅ <b>Broadcast complete</b>\n\n"
               f"Delivered: <b>{sent_count}</b>\n"
               f"Failed / blocked: <b>{failed_count}</b>\n"
               f"Total users: <b>{len(recipients)}</b>")
    await _pb_send_home(client, chat_id)


# ─── Keyboards ────────────────────────────────────────────────────────────
def main_menu_for(role: Optional[str]) -> list:
    if role == "management":
        return [
            [btn("🛠 Promote User", cb="pms")],
            [btn("🔍 Find User", cb="fdu")],
            [btn("🗂 Panel Bot", cb="pb:home")],
            [btn("❓ Help", cb="hlp")],
        ]
    if role == "owner":
        return [
            [btn("🔐 Get Credentials", cb="gc"), btn("🔁 Change Password", cb="cpw")],
            [btn("🔑 Access Keys", cb="km"), btn("👑 Manage Admins", cb="padm")],
            [btn("🔍 Find User", cb="fdu"), btn("👥 Access Users", cb="acu")],
            [btn("🔔 Auto Verify", cb="av:home"), btn("📢 Notifications", cb="nk:home")],
            [btn("🗂 Panel Bot", cb="pb:home")],
            [btn("❓ Help", cb="hlp")],
        ]
    if role in ("dev_admin", "base_admin"):
        return [
            [btn("🔐 Get Credentials", cb="gc"), btn("🔁 Change Password", cb="cpw")],
            [btn("🔑 Access Keys", cb="km")],
            [btn("🔍 Find User", cb="fdu"), btn("👥 Access Users", cb="acu")],
            [btn("🔔 Auto Verify", cb="av:home"), btn("📢 Notifications", cb="nk:home")],
            [btn("🗂 Panel Bot", cb="pb:home")] if role == "dev_admin" else [],
            [btn("❓ Help", cb="hlp")],
        ]
    return [
        [btn("🔐 Get Credentials", cb="gc"), btn("🔁 Change Password", cb="cpw")],
        [btn("🔔 Auto Verify", cb="av:home"), btn("📢 Notifications", cb="nk:home")],
        [btn("❓ Help", cb="hlp")],
    ]


def keys_submenu() -> list:
    return [
        [btn("🆕 New key", cb="knm")],
        [btn("📋 List keys", cb="kls")],
        [btn("🚫 Revoke key", cb="krm")],
        [btn("⬅️ Back", cb=CB_CANCEL)],
    ]


def access_users_list_keyboard(users: list[dict], back_cb: str = CB_CANCEL) -> list:
    rows = []
    for u in users:
        exp = u.get("access_expires_at")
        if exp and _expired(exp):
            status = "⛔"
        elif exp:
            status = "✅"
        else:
            status = "♾"
        rows.append([btn(f"{status} {_esc(u['name'])} — {u['user_id']}", cb=f"acu_u:{u['tg_uid']}")])
    rows.append([btn("⬅️ Back", cb=back_cb)])
    return rows


def promote_admin_keyboard(base_count: int, dev_count: int, demotable: int = 0) -> list:
    rows = [
        [btn(f"🔧 Promote Base Admin ({base_count})", cb="padm_b")],
        [btn(f"⚙️ Promote Dev Admin ({dev_count})", cb="padm_d")],
    ]
    if demotable > 0:
        rows.append([btn(f"➖ Remove Admin ({demotable})", cb="pml")])
    rows.append([btn("⬅️ Back", cb=CB_CANCEL)])
    return rows


def role_picker_keyboard(
    actor_role: str,
    cb_prefix: str,
    mode: str,
    target_current_role: Optional[str] = None,
) -> list:
    ROLES = ["user", "base_admin", "dev_admin", "owner"]
    gate = can_create_key if mode == "key" else can_promote
    rows: list = []
    for r in ROLES:
        if not gate(actor_role, r):
            continue
        if (mode == "promote" and target_current_role
                and role_privilege(r) < role_privilege(target_current_role)):
            if not can_demote(actor_role, target_current_role):
                continue
        rows.append([btn(f"{role_emoji(r)} {role_label(r)}", cb=f"{cb_prefix}:{r}")])
    rows.append([btn("⬅️ Cancel", cb=CB_CANCEL)])
    return rows


def duration_picker_keyboard(role: str) -> list:
    # User keys: 1 h min, 2 months (56 days) max, no unlimited.
    # Admin/dev-admin keys remain unlimited (no expiry picker shown).
    if role == "user":
        opts = [
            ("1h", "1h"),
            ("6h", "6h"),
            ("12h", "12h"),
            ("7d", "7d"),
            ("30d", "30d"),
            ("1mo", "1mo"),
            ("2mo", "2mo"),
        ]
        row1 = [btn(t, cb=f"knd:{role}:{d}") for t, d in opts[:3]]
        row2 = [btn(t, cb=f"knd:{role}:{d}") for t, d in opts[3:6]]
        row3 = [btn(t, cb=f"knd:{role}:{d}") for t, d in opts[6:]]
        return [row1, row2, row3, [btn("⬅️ Cancel", cb=CB_CANCEL)]]
    # Non-user roles: always unlimited
    return [[btn("♾ Unlimited", cb=f"knd:{role}:unlimited")], [btn("⬅️ Cancel", cb=CB_CANCEL)]]


def revoke_list_keyboard(rows: list[dict]) -> list:
    kb: list = [
        [btn(f"🚫 {k['code']}  ({role_label(k['role'])})", cb=f"krx:{k['code']}")]
        for k in rows[:20]
    ]
    kb.append([btn("✏️ Type a code instead", cb="krt")])
    kb.append([btn("⬅️ Back", cb="km")])
    return kb


# ─── Send menu ────────────────────────────────────────────────────────────
async def send_main_menu(client: Client, chat_id: int, header_override: Optional[str] = None) -> None:
    role = await effective_role(chat_id)
    if not role:
        await send(client, chat_id, "Send /start to begin.")
        return
    u = None if role == "management" else await db.get_user_by_tg_uid(chat_id)
    exp_line = ""
    if u and u.get("access_expires_at"):
        exp_line = f"\n<b>Access expires:</b> {_ts_utc(u['access_expires_at'])}"
    if header_override is not None:
        header = header_override
    elif role == "management":
        header = ("🛠️ <b>Management — Zenin Panel Access</b>\n"
                  "Promote owners and view the support contact.")
    else:
        header = f"{role_emoji(role)} <b>{role_label(role)}</b>"
        if u:
            header += f"\n<b>User ID:</b> <code>{u['user_id']}</code>"
        header += exp_line
    await send(client, chat_id, f"{header}\n\nWhat would you like to do?",
               main_menu_for(role))


# ─── Required channels / membership ───────────────────────────────────────
async def list_required_channels() -> list[dict]:
    out: list[dict] = []
    for c in store.required_channels():
        cid = c.get("chatId")
        if isinstance(cid, str) and cid.startswith("pending:"):
            continue
        out.append({
            "chat_id": cid,
            "title": c.get("title"),
            "invite_link": c.get("inviteLink"),
        })
    return out


async def check_channel_membership(tg_uid: int) -> dict[str, list]:
    channels = await list_required_channels()
    missing: list = []
    for ch in channels:
        status = await get_chat_member_status(ch["chat_id"], tg_uid)
        if status is None:
            missing.append(ch)
            continue
        if status in ("left", "kicked"):
            missing.append(ch)
    return {"all": channels, "missing": missing}


def channels_keyboard(channels: list[dict]) -> list:
    rows: list = []
    for ch in channels:
        link = ch.get("invite_link")
        if link:
            rows.append([btn(f"📢 {ch['title']}", url=link)])
    rows.append([btn("✅ I've joined — Verify", cb="verify_channels")])
    return rows


# ─── /start ──────────────────────────────────────────────────────────────
async def show_start(client: Client, chat_id: int, from_user) -> None:
    if chat_id == HARDCODED_MANAGEMENT_ID:
        await send_main_menu(client, chat_id)
        return
    existing = await db.get_user_by_tg_uid(chat_id)
    if existing:
        exp = existing.get("access_expires_at")
        if exp and _expired(exp):
            await send(client, chat_id,
                       f"⌛ Your access expired on {_ts_utc(exp)}.\n"
                       "Ask an admin for a new access key, then send /start.")
            return
        if from_user:
            new_username = getattr(from_user, "username", None)
            real_name = (getattr(from_user, "first_name", "") or "").strip()
            patches: list[tuple[str, Any]] = []
            if is_placeholder_name(existing.get("name") or "") and real_name:
                patches.append(("name", real_name))
            if existing.get("tg_username") != new_username:
                patches.append(("tg_username", new_username))
            if patches:
                set_clause = ", ".join(f"{c} = %s" for c, _ in patches) + ", updated_at = now()"
                params = tuple(v for _, v in patches) + (chat_id,)
                await db.execute(
                    f"UPDATE users SET {set_clause} WHERE tg_uid = %s", params,
                )
        await send_main_menu(client, chat_id)
        return
    real_name = ""
    tg_username = None
    if from_user:
        real_name = (getattr(from_user, "first_name", "") or "").strip()[:80]
        tg_username = getattr(from_user, "username", None)
    name = real_name if len(real_name) >= 2 else placeholder_name(chat_id)
    await present_channel_gate(client, chat_id, name, tg_username)


async def present_channel_gate(client: Client, chat_id: int, name: str, tg_username: Optional[str]) -> None:
    res = await check_channel_membership(chat_id)
    if not res["all"] or not res["missing"]:
        _set_dialog(chat_id, {"kind": "awaiting_key", "name": name, "tg_username": tg_username})
        await send(client, chat_id,
                   "🔑 <b>Send your access key.</b>\n\n"
                   "Paste the access key an admin shared with you. "
                   "Format looks like <code>ZN-XXXX-XXXX</code>.")
        return
    _set_dialog(chat_id, {"kind": "awaiting_key", "name": name, "tg_username": tg_username})
    missing = res["missing"]
    lines = "\n".join(f"• {_esc(c['title'])}" for c in missing)
    await send(client, chat_id,
               f"📢 <b>Almost there.</b>\n\nPlease join the channel(s) below, then tap <b>Verify</b>:\n\n{lines}",
               channels_keyboard(missing))


async def handle_verify(client: Client, cq) -> None:
    chat_id = cq.from_user.id
    state = _get_dialog(chat_id)
    if chat_id == HARDCODED_MANAGEMENT_ID:
        await cq.answer("Management — skip")
        return
    if state.get("kind") != "awaiting_key":
        await cq.answer("Send /start first", show_alert=True)
        return
    res = await check_channel_membership(chat_id)
    if res["missing"]:
        await cq.answer(f"Still missing {len(res['missing'])} channel(s)", show_alert=True)
        if cq.message:
            lines = "\n".join(f"• {_esc(c['title'])}" for c in res["missing"])
            await edit(client, cq.message.chat.id, cq.message.id,
                       f"📢 <b>Still not in every channel.</b>\n\n{lines}",
                       channels_keyboard(res["missing"]))
        return
    await cq.answer("Verified ✓")
    if cq.message:
        await edit(client, cq.message.chat.id, cq.message.id,
                   f"✅ Joined {len(res['all'])} channel(s).")
    _set_dialog(chat_id, {"kind": "awaiting_key", "name": state["name"],
                          "tg_username": state["tg_username"]})
    await send(client, chat_id,
               "🔑 <b>Send your access key.</b>\n\n"
               "Format looks like <code>ZN-XXXX-XXXX</code>.")


# ─── Key submission ──────────────────────────────────────────────────────
async def handle_key_submission(client: Client, msg, raw: str, state: dict) -> None:
    chat_id = msg.chat.id
    code = raw.strip().upper()
    if not re.match(r"^ZN-[A-Z2-9]{4}-[A-Z2-9]{4}$", code):
        await send(client, chat_id,
                   "❌ That doesn't look like a valid key. Expected <code>ZN-XXXX-XXXX</code>.")
        return
    res = await check_channel_membership(chat_id)
    if res["missing"]:
        await present_channel_gate(client, chat_id, state["name"], state["tg_username"])
        return
    key = await db.get_access_key_by_code(code)
    if not key:
        await send(client, chat_id, "❌ Unknown access key.")
        return
    if key.get("revoked"):
        await send(client, chat_id, "❌ This access key was revoked.")
        return
    if key.get("redeemed_by_tg_uid") is not None:
        await send(client, chat_id, "❌ This access key has already been used.")
        return
    if _expired(key.get("expires_at")):
        await send(client, chat_id, "❌ This access key has expired.")
        return
    existing = await db.get_user_by_tg_uid(chat_id)
    if existing:
        _set_dialog(chat_id, {"kind": "idle"})
        await send(client, chat_id,
                   f"You already have an account.\n<b>User ID:</b> <code>{existing['user_id']}</code>\n"
                   "Use /get_credential for a fresh password.")
        return
    user_id = await _generate_unique_user_id()
    placeholder = gen_password()
    pwd_hash, pwd_salt = hash_password(placeholder)

    error: Optional[str] = None
    try:
        async with db.transaction() as cur:
            await cur.execute(
                "UPDATE access_keys SET redeemed_by_tg_uid = %s, redeemed_at = now() "
                "WHERE id = %s AND revoked = false AND redeemed_by_tg_uid IS NULL "
                "RETURNING id",
                (chat_id, key["id"]),
            )
            if not await cur.fetchone():
                raise RuntimeError("__lost_race__")
            if key["role"] == "owner":
                # Owner-cap call site 1/3 (key-redeem path). Advisory lock is
                # taken here — inside this transaction, after the single-use key
                # claim above — before the COUNT(*) + INSERT, serializing this
                # call against the other two owner-creation paths. See
                # db.acquire_owner_cap_lock for the exhaustive call-site list
                # and the proof that no un-locked owner-creation path exists.
                await db.acquire_owner_cap_lock(cur)
                await cur.execute("SELECT COUNT(*)::int AS n FROM users WHERE role = 'owner'")
                row = await cur.fetchone()
                if row and int(row["n"]) >= MAX_OWNERS:
                    raise RuntimeError("__owner_cap__")
            # Timer starts at claim time: access_expires_at = now + duration_seconds.
            # Fallback for old keys (duration_seconds == 0): use the stored expires_at.
            dur_secs = key.get("duration_seconds") or 0
            if dur_secs > 0:
                access_exp = datetime.now(timezone.utc) + timedelta(seconds=dur_secs)
            else:
                access_exp = key.get("expires_at")  # legacy keys
            await cur.execute(
                "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, name, tg_username, "
                "role, access_granted, access_expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s)",
                (chat_id, user_id, pwd_hash, pwd_salt, state["name"],
                 state.get("tg_username") or None,
                 key["role"], access_exp),
            )
            await cur.execute(
                "INSERT INTO role_events (target_tg_uid, actor_tg_uid, old_role, new_role, reason) "
                "VALUES (%s, %s, NULL, %s, 'key_redeem')",
                (chat_id, key["created_by_tg_uid"], key["role"]),
            )
    except RuntimeError as e:
        error = str(e)
    except Exception:
        log.exception("bot3: redemption transaction failed")
        await send(client, chat_id, "❌ Something went wrong creating your account. Please try again.")
        return

    if error == "__lost_race__":
        _post_key = await db.get_access_key_by_code(code)
        if _post_key and _post_key.get("revoked"):
            await send(client, chat_id, "❌ This access key was revoked.")
        else:
            await send(client, chat_id, "❌ Another user just redeemed that key first.")
        return
    if error == "__owner_cap__":
        await send(client, chat_id,
                   f"❌ The owner slot is full (cap {MAX_OWNERS}). "
                   "Ask management to free a slot, then retry.")
        return

    _set_dialog(chat_id, {"kind": "idle"})
    # Compute the access expiry for display (mirrors the value stored in the transaction above)
    dur_secs = key.get("duration_seconds") or 0
    if dur_secs > 0:
        access_exp = datetime.now(timezone.utc) + timedelta(seconds=dur_secs)
    else:
        access_exp = key.get("expires_at")
    exp_line = (f"<b>Access expires:</b> {_ts_utc(access_exp)}\n" if access_exp
                else "<b>Access:</b> Unlimited\n")
    await send(client, chat_id,
               "🎉 <b>Access granted.</b>\n\n"
               f"<b>Role:</b> {role_label(key['role'])}\n{exp_line}\n"
               "Use <b>/get_credential</b> any time to see your User ID and panel password.")

    # Notify the key creator that their key was claimed
    creator_uid = key.get("created_by_tg_uid")
    if creator_uid and creator_uid != chat_id:
        key_label = key.get("label") or ""
        label_part = f" ({key_label})" if key_label else ""
        try:
            await send(
                client, creator_uid,
                f"🔔 <b>Your access key was claimed!</b>\n\n"
                f"<b>Key:</b> <code>{code}</code>{label_part}\n"
                f"<b>Claimed by:</b> {state['name']}\n"
                + (f"<b>Username:</b> @{state['tg_username']}\n" if state.get("tg_username") else "")
                + f"<b>Telegram ID:</b> <code>{chat_id}</code>\n"
                f"<b>User ID:</b> <code>{user_id}</code>\n"
                f"<b>Role granted:</b> {role_label(key['role'])}\n"
                f"{exp_line}",
            )
        except Exception:
            log.warning("bot3: failed to notify key creator %s", creator_uid)

# ─── /get_credential ─────────────────────────────────────────────────────
async def handle_get_credential(client: Client, chat_id: int) -> None:
    if chat_id == HARDCODED_MANAGEMENT_ID:
        await send(client, chat_id,
                   "🛠️ <b>Management cannot use the panel.</b>\n\n"
                   "Use /channels, /newkey, /keys, /revokekey, /promote from here to administer the system.")
        return
    u = await db.get_user_by_tg_uid(chat_id)
    if not u:
        await send(client, chat_id,
                   "❌ You don't have an account yet.\n\n"
                   "Send /start to begin: you'll need to join the required channels and redeem an access key.")
        return
    if _expired(u.get("access_expires_at")):
        await send(client, chat_id,
                   f"⌛ Your access expired on {_ts_utc(u['access_expires_at'])}.\n"
                   "Ask an admin for a new access key.")
        return
    # Always return the SAME current password. We only generate (and persist)
    # one the first time, for accounts that have no stored plaintext yet (e.g.
    # created before this column existed, or via key redeem / promote). After
    # that, every tap re-shows the existing password unchanged — no rotation,
    # no token_version bump, so existing panel sessions stay valid.
    password = u.get("panel_password")
    newly_issued = not password
    if not password:
        candidate = gen_password()
        pwd_hash, pwd_salt = hash_password(candidate)
        # Conditional update: only seed a password when none is stored yet.
        # The `panel_password IS NULL` guard makes concurrent first-taps (and a
        # racing panel change-password) safe — at most one writer wins, and a
        # loser's UPDATE matches 0 rows, so we re-read the winner's value below
        # instead of overwriting it. Guarantees every tap returns one password.
        applied = await db.execute(
            "UPDATE users SET password_hash = %s, password_salt = %s, "
            "panel_password = %s, token_version = token_version + 1, "
            "updated_at = now() WHERE tg_uid = %s AND panel_password IS NULL",
            (pwd_hash, pwd_salt, candidate, chat_id),
        )
        if applied:
            password = candidate
        else:
            fresh = await db.get_user_by_tg_uid(chat_id)
            password = (fresh or {}).get("panel_password") or candidate
    exp = u.get("access_expires_at")
    exp_line = f"<b>Access expires:</b> {_ts_utc(exp)}\n" if exp else ""
    # On first issuance we just generated this password, so any password the
    # user may have been using before (legacy accounts predate the stored copy)
    # no longer works. Say so explicitly instead of calling it their "current"
    # one, so the change is never a surprise.
    footer = ("🆕 This is a <b>newly issued</b> panel password. If you were "
              "using a different one before, it no longer works — use this one "
              "from now on."
              if newly_issued else
              "This is your current panel password. Use <b>Change Password</b> "
              "if you want a new one.")
    # The APK button delivers the file straight into this chat (see
    # handle_send_apk) instead of bouncing the user out to a browser.
    apk_row = [[btn("📱 Get ZENIN App (APK)", cb="apk")]] if APK_URL else []
    await send(client, chat_id,
               "🔐 <b>Panel credentials</b>\n\n"
               f"<b>User ID:</b> <code>{u['user_id']}</code>\n"
               f"<b>Password:</b> <code>{_esc(password)}</code>\n"
               f"<b>Role:</b> {role_label(u['role'])}\n"
               f"{exp_line}\n"
               f"{footer}",
               apk_row if apk_row else None)


# ─── Direct APK delivery ────────────────────────────────────────────────────
# Telegram bots can send documents up to ~50 MB; the release APK is ~12 MB, so
# we attach it straight into the chat instead of making the user open a
# browser. After the first successful upload we cache the Telegram file_id so
# repeat taps are instant. The release URL is stable while the asset behind it
# changes on every CI build, so the cache key includes the asset's ETag (from
# a cheap HEAD request) — a rebuilt APK gets a new ETag and is re-uploaded
# instead of serving a stale file.
_APK_FILE_IDS: dict[str, str] = {}


async def _apk_cache_key() -> str:
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.head(APK_URL) as resp:
                etag = resp.headers.get("ETag") or resp.headers.get("Last-Modified") or ""
                return f"{APK_URL}|{etag}"
    except Exception:
        return APK_URL


async def handle_send_apk(client: Client, chat_id: int) -> None:
    if not APK_URL:
        await send(client, chat_id, "❌ The app download isn't configured yet. Ask an admin.")
        return
    caption = ("📱 <b>ZENIN App (Android)</b>\n\n"
               "Install this APK, then log in with the <b>User ID</b> and "
               "<b>password</b> from Get Credentials.")
    fallback_kb = [[btn("📱 Download ZENIN App (browser)", url=APK_URL)]]
    key = await _apk_cache_key()
    cached = _APK_FILE_IDS.get(key)
    if cached:
        try:
            await client.send_document(chat_id, cached, caption=caption,
                                       parse_mode=ParseMode.HTML)
            return
        except Exception:
            _APK_FILE_IDS.pop(key, None)  # expired id — fall through to re-upload
    await client.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(APK_URL) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.read()
    except Exception as e:
        log.warning("APK download failed: %s", e)
        await send(client, chat_id,
                   "⚠️ Couldn't fetch the APK right now — use the direct link instead:",
                   fallback_kb)
        return
    try:
        msg = await client.send_document(chat_id, io.BytesIO(data),
                                         file_name="ZENIN.apk", caption=caption,
                                         parse_mode=ParseMode.HTML)
        if msg and msg.document:
            _APK_FILE_IDS[key] = msg.document.file_id
    except Exception as e:
        log.warning("APK upload failed: %s", e)
        await send(client, chat_id,
                   "⚠️ Couldn't send the APK file — use the direct link instead:",
                   fallback_kb)


async def handle_change_password_start(client: Client, chat_id: int) -> None:
    if chat_id == HARDCODED_MANAGEMENT_ID:
        await send(client, chat_id, "🛠️ Management is bot-only and has no panel password.")
        return
    u = await db.get_user_by_tg_uid(chat_id)
    if not u:
        await send(client, chat_id,
                   "❌ You don't have an account yet.\n\n"
                   "Send /start to begin: you'll need to join the required channels and redeem an access key.")
        return
    if _expired(u.get("access_expires_at")):
        await send(client, chat_id,
                   f"⌛ Your access expired on {_ts_utc(u['access_expires_at'])}.\n"
                   "Ask an admin for a new access key.")
        return
    _set_dialog(chat_id, {"kind": "awaiting_new_password"})
    await send(client, chat_id,
               "🔁 <b>Change Password</b>\n\n"
               "Send me the <b>new password</b> you'd like to use for the panel.\n\n"
               "• 6–64 characters\n"
               "• No spaces\n\n"
               "Or tap Cancel to keep your current password.",
               [[btn("✖️ Cancel", cb=CB_CANCEL)]])


async def handle_new_password_submission(client: Client, chat_id: int, text: str) -> None:
    pw = (text or "").strip()
    if len(pw) < 6 or len(pw) > 64 or " " in pw:
        await send(client, chat_id,
                   "❌ Password must be 6–64 characters with no spaces. Try again, or tap Cancel.",
                   [[btn("✖️ Cancel", cb=CB_CANCEL)]])
        return
    u = await db.get_user_by_tg_uid(chat_id)
    if not u:
        _set_dialog(chat_id, {"kind": "idle"})
        await send(client, chat_id, "❌ You don't have an account yet. Send /start.")
        return
    pwd_hash, pwd_salt = hash_password(pw)
    await db.execute(
        "UPDATE users SET password_hash = %s, password_salt = %s, "
        "panel_password = %s, token_version = token_version + 1, "
        "updated_at = now() WHERE tg_uid = %s",
        (pwd_hash, pwd_salt, pw, chat_id),
    )
    _set_dialog(chat_id, {"kind": "idle"})
    await send(client, chat_id,
               "✅ <b>Password changed</b>\n\n"
               f"<b>User ID:</b> <code>{u['user_id']}</code>\n"
               f"<b>Password:</b> <code>{_esc(pw)}</code>\n\n"
               "Your old password no longer works.")
    await send_main_menu(client, chat_id)


# ─── Key commands ────────────────────────────────────────────────────────
async def cmd_new_key(client: Client, creator: dict, args: str) -> None:
    parts = args.strip().split()
    if len(parts) < 2:
        # Mirror can_create_key exactly. Owner is intentionally absent for
        # every role: owners are minted only via /promote (management -> owner),
        # never via access keys. Management itself creates no keys at all.
        if creator["role"] == "management":
            await send(client, creator["tg_uid"],
                       "🛠️ <b>Management does not create access keys.</b>\n\n"
                       "Owners are appointed with <code>/promote</code>. "
                       "Owners then mint user / base_admin / dev_admin keys.")
            return
        if creator["role"] == "owner":
            role_opts = "user | base_admin | dev_admin"
        else:
            role_opts = "user"
        await send(client, creator["tg_uid"],
                   "Usage: <code>/newkey &lt;role&gt; &lt;duration&gt;</code>\n\n"
                   f"<b>role:</b> {role_opts}\n"
                   "<b>duration:</b> unlimited | 30m | 6h | 7d\n\n"
                   "Examples:\n<code>/newkey user 7d</code>\n<code>/newkey base_admin unlimited</code>")
        return
    role = parse_role(parts[0])
    if not role:
        await send(client, creator["tg_uid"], "❌ Unknown role.")
        return
    if not can_create_key(creator["role"], role):
        await send(client, creator["tg_uid"],
                   f"❌ Your role ({role_label(creator['role'])}) cannot create <b>{role_label(role)}</b> keys.")
        return
    # Unreachable defensive guard: can_create_key() forbids an "owner" target
    # for every creator role (see the parametrized test
    # test_owner_key_never_creatable_by_any_role), so the can_create_key gate
    # two lines above always rejects role == "owner" before this point is ever
    # reached. Owners are minted only through the three locked paths enumerated
    # in db.acquire_owner_cap_lock's docstring; cmd_new_key is not one of them.
    #
    # Concurrency note — the "promotion + /newkey race" cannot exist:
    # Because this block is unreachable, a concurrent apply_promotion(…, "owner")
    # call and a /newkey call are never contending on the owner-cap resource at
    # the same time. The /newkey path never reaches the count-then-write window,
    # so there is no gap to close and no advisory lock is needed here.
    #
    # This block is kept purely as a backstop in case the can_create_key rule is
    # ever loosened. If that ever happens, the non-transactional count_owners()
    # call here is still racy (count-then-write without a lock) and MUST be
    # replaced with a proper db.acquire_owner_cap_lock-guarded transaction before
    # it becomes reachable. The redeem-time and promote-time guards are the
    # authoritative, correctly locked cap checks.
    if role == "owner":
        owners = await count_owners()
        if owners >= MAX_OWNERS:
            await send(client, creator["tg_uid"],
                       f"❌ Owner cap reached ({owners}/{MAX_OWNERS}). "
                       "Demote or remove an existing owner first.")
            return
    dur = parse_duration(" ".join(parts[1:]))
    if not dur:
        await send(client, creator["tg_uid"],
                   "❌ Bad duration. Try <code>1h</code>, <code>6h</code>, <code>7d</code>, <code>30d</code>, <code>1mo</code>, <code>2mo</code>.\n"
                   "User keys require an expiry (min 1 h, max 2 months = 56 days).")
        return
    # Enforce expiry rules for user keys: required, min 1 h, max 56 days.
    duration_secs = 0
    if role == "user":
        if dur["expires_at"] is None:
            await send(client, creator["tg_uid"],
                       "❌ User keys must have an expiry. Try <code>7d</code>, <code>1mo</code>, <code>2mo</code>.")
            return
        duration_secs = int((dur["expires_at"] - datetime.now(timezone.utc)).total_seconds())
        if duration_secs < 3600:
            await send(client, creator["tg_uid"], "❌ Minimum duration for user keys is 1 hour.")
            return
        if duration_secs > 56 * 86400:
            await send(client, creator["tg_uid"], "❌ Maximum duration for user keys is 2 months (56 days).")
            return
    code = gen_key_code()
    # expires_at is left NULL — the access timer starts at claim time (now + duration_secs).
    await db.execute(
        "INSERT INTO access_keys (id, code, role, label, duration_seconds, expires_at, created_by_tg_uid, created_by_role) "
        "VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)",
        (str(uuid.uuid4()), code, role, "", duration_secs, creator["tg_uid"], creator["role"]),
    )
    await send(client, creator["tg_uid"],
               "🔑 <b>Access key created</b>\n\n"
               f"<b>Code:</b> <code>{code}</code>\n"
               f"<b>Role:</b> {role_label(role)}\n"
               f"<b>Duration:</b> {dur['label']} — timer starts when claimed\n\n"
               "Single-use — share with one person only.")


async def cmd_list_keys(client: Client, viewer: dict) -> None:
    sees_all = viewer["role"] in ("management", "owner")
    if sees_all:
        rows = await db.fetchall(
            "SELECT * FROM access_keys ORDER BY created_at DESC LIMIT 40"
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM access_keys WHERE created_by_tg_uid = %s ORDER BY created_at DESC LIMIT 40",
            (viewer["tg_uid"],),
        )
    if not rows:
        await send(client, viewer["tg_uid"],
                   "No access keys yet. Create one with <code>/newkey user 7d</code>.")
        return
    lines = []
    for k in rows:
        if k.get("revoked"):
            status = "🚫 revoked"
        elif k.get("redeemed_by_tg_uid") is not None:
            status = f"✅ used by {k['redeemed_by_tg_uid']}"
        elif _expired(k.get("expires_at")):
            status = "⌛ expired"
        else:
            status = "🟢 active"
        if k.get("expires_at"):
            exp = "exp " + k["expires_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        else:
            exp = "unlimited"
        lines.append(f"<code>{k['code']}</code>  {role_label(k['role'])}  {exp}  {status}")
    await send(client, viewer["tg_uid"],
               f"🔑 <b>Access keys</b> ({len(rows)})\n\n" + "\n".join(lines))


async def cmd_revoke_key(client: Client, viewer: dict, args: str) -> None:
    code = args.strip().upper()
    if not code:
        await send(client, viewer["tg_uid"], "Usage: <code>/revokekey ZN-XXXX-XXXX</code>")
        return
    sees_all = viewer["role"] in ("management", "owner")
    if sees_all:
        row = await db.fetchone(
            "UPDATE access_keys SET revoked = true WHERE code = %s RETURNING code", (code,),
        )
    else:
        row = await db.fetchone(
            "UPDATE access_keys SET revoked = true WHERE code = %s AND created_by_tg_uid = %s RETURNING code",
            (code, viewer["tg_uid"]),
        )
    if not row:
        await send(client, viewer["tg_uid"], "Not found (or you don't own it).")
        return
    await send(client, viewer["tg_uid"], f"✅ Revoked <code>{row['code']}</code>")


# ─── /indexes — end a user's access immediately ───────────────────────────
async def cmd_indexes(client: Client, actor: dict, args: str) -> None:
    """End a user's access immediately.

    Accepts a Telegram user ID, panel User ID, or access key code.
    Management/owner can revoke anyone; admins can only revoke users
    created via their own keys.
    """
    raw = args.strip()
    if not raw:
        await send(
            client, actor["tg_uid"],
            "Usage: <code>/end &lt;telegram_id | user_id | key_code&gt;</code>\n\n"
            "Examples:\n"
            "• <code>/end 123456789</code> — by Telegram ID\n"
            "• <code>/end USR-XXXXXX</code> — by panel User ID\n"
            "• <code>/end ZN-XXXX-XXXX</code> — by access key code",
        )
        return

    # ── Resolve the target user ──────────────────────────────────────────
    u = None
    resolved_via = ""
    if re.match(r"^\d{5,15}$", raw):
        u = await db.get_user_by_tg_uid(int(raw))
        resolved_via = "Telegram ID"
    elif re.match(r"^ZN-[A-Z2-9]{4}-[A-Z2-9]{4}$", raw.upper()):
        key = await db.get_access_key_by_code(raw.upper())
        if not key:
            await send(client, actor["tg_uid"], "❌ Access key not found.")
            return
        if not key.get("redeemed_by_tg_uid"):
            await send(client, actor["tg_uid"],
                       "❌ This key hasn't been redeemed yet — no user to revoke.")
            return
        u = await db.get_user_by_tg_uid(key["redeemed_by_tg_uid"])
        resolved_via = "access key"
    else:
        u = await db.get_user_by_user_id(raw)
        resolved_via = "User ID"

    if not u:
        await send(client, actor["tg_uid"], "❌ No user found with that identifier.")
        return

    target_tg_uid = u["tg_uid"]
    target_role = u.get("role", "user")

    # ── Permission checks ────────────────────────────────────────────────
    if target_role == "management":
        await send(client, actor["tg_uid"], "❌ Cannot revoke the management account.")
        return
    if target_role == "owner" and actor["role"] != "management":
        await send(client, actor["tg_uid"], "❌ Only management can revoke an owner's access.")
        return

    sees_all = actor["role"] in ("management", "owner")
    if not sees_all:
        # Admins may only revoke users who were created by their own keys
        created_via_actor = await db.fetchone(
            "SELECT id FROM access_keys WHERE redeemed_by_tg_uid = %s AND created_by_tg_uid = %s",
            (target_tg_uid, actor["tg_uid"]),
        )
        if not created_via_actor:
            await send(client, actor["tg_uid"],
                       "❌ You can only revoke access for users who redeemed one of your keys.")
            return

    # ── End access immediately ────────────────────────────────────────────
    await db.execute(
        "UPDATE users SET access_expires_at = now(), access_granted = false, "
        "updated_at = now() WHERE tg_uid = %s",
        (target_tg_uid,),
    )
    # Revoke the access key so it can never be used again
    await db.execute(
        "UPDATE access_keys SET revoked = true WHERE redeemed_by_tg_uid = %s",
        (target_tg_uid,),
    )

    # Notify the affected user
    try:
        await send(client, target_tg_uid,
                   "⛔ <b>Your access has been revoked.</b>\n\n"
                   "Contact an administrator if you think this is a mistake.")
    except Exception:
        log.warning("bot3: failed to notify revoked user %s", target_tg_uid)

    await send(
        client, actor["tg_uid"],
        f"✅ <b>Access ended</b>\n\n"
        f"<b>Name:</b> {_esc(u['name'])}\n"
        f"<b>User ID:</b> <code>{u['user_id']}</code>\n"
        f"<b>Telegram ID:</b> <code>{target_tg_uid}</code>\n"
        + (f"<b>Username:</b> @{_esc(u['tg_username'])}\n" if u.get("tg_username") else "")
        + f"<b>Resolved via:</b> {resolved_via}",
    )


# ─── Promote (atomic with optional cap check) ────────────────────────────
async def _apply_role_change(
    target_tg_uid: int,
    target_user_id: str,
    target_name: str,
    target_username: Optional[str],
    target_role: str,
    actor: dict,
) -> str:
    """Apply role change inside a transaction with locking + audit. Returns
    sentinel error message or "ok"."""
    try:
        async with db.transaction() as cur:
            await cur.execute(
                "SELECT role FROM users WHERE tg_uid = %s FOR UPDATE", (target_tg_uid,),
            )
            row = await cur.fetchone()
            if not row:
                return "__missing__"
            current_role = row["role"]
            if current_role == target_role:
                return "__noop__"
            if not can_promote(actor["role"], target_role):
                return "__forbidden__"
            if role_privilege(target_role) < role_privilege(current_role) and not can_demote(actor["role"], current_role):
                return "__forbidden__"
            if target_role == "owner" and current_role != "owner":
                # Owner-cap call site 2/3 (existing-user promote path). Advisory
                # lock is taken here — inside this transaction, before COUNT(*) +
                # UPDATE — serializing this call against the other two owner-
                # creation paths. See db.acquire_owner_cap_lock for the
                # exhaustive call-site list and the consistency proof.
                await db.acquire_owner_cap_lock(cur)
                await cur.execute("SELECT COUNT(*)::int AS n FROM users WHERE role = 'owner'")
                cnt = await cur.fetchone()
                if cnt and int(cnt["n"]) >= MAX_OWNERS:
                    return "__owner_cap__"
            # When promoting above 'user', grant permanent access so Get Credential works.
            if target_role != "user":
                await cur.execute(
                    "UPDATE users SET role = %s, token_version = token_version + 1, "
                    "access_granted = true, access_expires_at = NULL, "
                    "updated_at = now() WHERE tg_uid = %s",
                    (target_role, target_tg_uid),
                )
            else:
                await cur.execute(
                    "UPDATE users SET role = %s, token_version = token_version + 1, "
                    "updated_at = now() WHERE tg_uid = %s",
                    (target_role, target_tg_uid),
                )
            await cur.execute(
                "INSERT INTO role_events (target_tg_uid, actor_tg_uid, old_role, new_role, reason) "
                "VALUES (%s, %s, %s, %s, 'bot_promote')",
                (target_tg_uid, actor["tg_uid"], current_role, target_role),
            )
            return "ok"
    except Exception:
        log.exception("bot3: role change transaction failed")
        return "__error__"


async def apply_promotion(client: Client, actor: dict, target_tg_uid: int, target_role: str) -> None:
    if target_tg_uid == HARDCODED_MANAGEMENT_ID:
        await send(client, actor["tg_uid"], "❌ The management account is hardcoded and cannot be re-assigned.")
        return
    if not can_promote(actor["role"], target_role):
        await send(client, actor["tg_uid"],
                   f"❌ Your role ({role_label(actor['role'])}) cannot assign <b>{role_label(target_role)}</b>.")
        return
    existing = await db.get_user_by_tg_uid(target_tg_uid)
    if existing:
        if existing["role"] == target_role:
            await send(client, actor["tg_uid"],
                       f"ℹ️ <code>{existing['user_id']}</code> ({_esc(existing['name'])}) "
                       f"is already {role_label(target_role)}.")
            return
        if role_privilege(target_role) < role_privilege(existing["role"]) and not can_demote(actor["role"], existing["role"]):
            await send(client, actor["tg_uid"],
                       f"❌ Your role ({role_label(actor['role'])}) cannot demote <b>{role_label(existing['role'])}</b>.")
            return
        result = await _apply_role_change(
            target_tg_uid, existing["user_id"], existing["name"], existing.get("tg_username"),
            target_role, actor,
        )
        if result == "__owner_cap__":
            await send(client, actor["tg_uid"], f"❌ Owner cap reached ({MAX_OWNERS}). Demote an existing owner first.")
            return
        if result == "__forbidden__":
            await send(client, actor["tg_uid"], "❌ Their role changed while you were deciding — the transition is no longer allowed.")
            return
        if result == "__noop__":
            await send(client, actor["tg_uid"], f"ℹ️ They're already {role_label(target_role)} now.")
            return
        if result == "__missing__":
            await send(client, actor["tg_uid"], "❌ That user no longer exists.")
            return
        if result != "ok":
            await send(client, actor["tg_uid"], "❌ Failed to update role. Please try again.")
            return
        await send(client, actor["tg_uid"],
                   f"✅ <b>Promoted</b>\n\n"
                   f"<b>Name:</b> {_esc(existing['name'])}\n"
                   f"<b>Panel ID:</b> <code>{existing['user_id']}</code>\n"
                   f"<b>Telegram ID:</b> <code>{target_tg_uid}</code>\n"
                   f"<b>New Role:</b> {role_label(target_role)}\n\n"
                   "Any existing panel session for them has been signed out.")
        try:
            await send(client, target_tg_uid,
                       f"🔔 Your role is now <b>{role_label(target_role)}</b>.\n\n"
                       "Send /get_credential to fetch a fresh password.")
        except Exception:
            pass
        if actor["tg_uid"] != HARDCODED_MANAGEMENT_ID:
            try:
                await send(client, HARDCODED_MANAGEMENT_ID,
                           f"📢 <b>Promotion notice</b>\n\n"
                           f"<b>{_esc(existing['name'])}</b> (<code>{existing['user_id']}</code>) "
                           f"[TG: <code>{target_tg_uid}</code>] has been promoted to "
                           f"<b>{role_label(target_role)}</b> by "
                           f"<b>{role_label(actor['role'])}</b> <code>{actor['tg_uid']}</code>.")
            except Exception:
                pass
        return

    # No user row yet — pre-create.
    try:
        new_user_id = await _generate_unique_user_id()
    except RuntimeError:
        log.exception("bot3: applyPromotion could not generate unique user id")
        await send(client, actor["tg_uid"], "❌ Failed to create the user. Please try again.")
        return
    pwd_hash, pwd_salt = hash_password(gen_password())
    err: Optional[str] = None
    try:
        async with db.transaction() as cur:
            if target_role == "owner":
                # Owner-cap call site 3/3 (new-user promote / pre-create path).
                # Advisory lock is taken here — inside this transaction, before
                # COUNT(*) + INSERT — serializing this call against the other two
                # owner-creation paths. See db.acquire_owner_cap_lock for the
                # exhaustive call-site list and the consistency proof.
                await db.acquire_owner_cap_lock(cur)
                await cur.execute("SELECT COUNT(*)::int AS n FROM users WHERE role = 'owner'")
                cnt = await cur.fetchone()
                if cnt and int(cnt["n"]) >= MAX_OWNERS:
                    raise RuntimeError("__owner_cap__")
            await cur.execute(
                "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, name, "
                "role, access_granted, access_expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, true, NULL)",
                (target_tg_uid, new_user_id, pwd_hash, pwd_salt,
                 placeholder_name(target_tg_uid), target_role),
            )
            await cur.execute(
                "INSERT INTO role_events (target_tg_uid, actor_tg_uid, old_role, new_role, reason) "
                "VALUES (%s, %s, NULL, %s, 'bot_promote')",
                (target_tg_uid, actor["tg_uid"], target_role),
            )
    except RuntimeError as e:
        err = str(e)
    except psycopg.errors.UniqueViolation:
        # Lost a concurrent pre-create race: another admin promoting the SAME
        # brand-new target committed their INSERT between our "no existing row"
        # check and this one. The tg_uid primary key (and user_id unique index)
        # rejected the duplicate, and the whole transaction — including the
        # role_events audit insert — rolled back, so there is no half-written
        # row. Tell the actor it already exists and to retry; the second run
        # will find the now-existing row and apply the role change normally.
        log.info("bot3: applyPromotion lost pre-create race for tg_uid=%s", target_tg_uid)
        await send(client, actor["tg_uid"],
                   "⚠️ That account was just created (likely by another admin). "
                   "Run /promote again to apply the role.")
        return
    except Exception:
        log.exception("bot3: applyPromotion insert failed")
        await send(client, actor["tg_uid"], "❌ Failed to create the user. Please try again.")
        return
    if err == "__owner_cap__":
        await send(client, actor["tg_uid"], f"❌ Owner cap reached ({MAX_OWNERS}). Demote an existing owner first.")
        return
    await send(client, actor["tg_uid"],
               "✅ <b>Promoted</b>\n\n"
               f"<b>Telegram ID:</b> <code>{target_tg_uid}</code>\n"
               f"<b>Panel ID:</b> <code>{new_user_id}</code>\n"
               f"<b>Role:</b> {role_label(target_role)}\n\n"
               "They haven't opened this bot yet — the role is live and they can tap /start "
               "anytime to fetch their credentials.")
    try:
        await send(client, target_tg_uid,
                   f"🎉 You've been promoted to <b>{role_label(target_role)}</b> on Zenin Panel.\n\n"
                   "Send /start to open the menu, then tap <b>Get Credentials</b> for your login password.")
    except Exception:
        pass
    if actor["tg_uid"] != HARDCODED_MANAGEMENT_ID:
        try:
            await send(client, HARDCODED_MANAGEMENT_ID,
                       f"📢 <b>Promotion notice</b>\n\n"
                       f"Telegram ID <code>{target_tg_uid}</code> "
                       f"(Panel ID: <code>{new_user_id}</code>) has been promoted to "
                       f"<b>{role_label(target_role)}</b> by "
                       f"<b>{role_label(actor['role'])}</b> <code>{actor['tg_uid']}</code>.")
        except Exception:
            pass

async def cmd_promote(client: Client, actor: dict, args: str) -> None:
    parts = args.strip().split()
    if len(parts) < 2:
        opts = ("user | base_admin | dev_admin | owner" if actor["role"] == "management"
                else "user | base_admin | dev_admin")
        await send(client, actor["tg_uid"],
                   "Usage: <code>/promote &lt;userId&gt; &lt;role&gt;</code>\n\n"
                   "<b>userId:</b> the numeric panel User ID\n"
                   f"<b>role:</b> {opts}\n\n"
                   "Example: <code>/promote 1234567 dev_admin</code>")
        return
    user_id_arg = parts[0].strip()
    target_role = parse_role(parts[1])
    if not target_role:
        await send(client, actor["tg_uid"], "❌ Unknown role.")
        return
    if not can_promote(actor["role"], target_role):
        await send(client, actor["tg_uid"],
                   f"❌ Your role ({role_label(actor['role'])}) cannot assign <b>{role_label(target_role)}</b>.")
        return
    target = await db.get_user_by_user_id(user_id_arg)
    if not target:
        await send(client, actor["tg_uid"], f"❌ No user with ID <code>{_esc(user_id_arg)}</code>.")
        return
    if target["tg_uid"] == HARDCODED_MANAGEMENT_ID:
        await send(client, actor["tg_uid"], "❌ The management account is hardcoded and cannot be re-assigned.")
        return
    if target["role"] == target_role:
        await send(client, actor["tg_uid"],
                   f"ℹ️ <code>{target['user_id']}</code> is already {role_label(target_role)}.")
        return
    if role_privilege(target_role) < role_privilege(target["role"]) and not can_demote(actor["role"], target["role"]):
        await send(client, actor["tg_uid"],
                   f"❌ Your role ({role_label(actor['role'])}) cannot demote <b>{role_label(target['role'])}</b>.")
        return
    result = await _apply_role_change(
        target["tg_uid"], target["user_id"], target["name"], target.get("tg_username"),
        target_role, actor,
    )
    if result == "__owner_cap__":
        await send(client, actor["tg_uid"], f"❌ Owner cap reached ({MAX_OWNERS}). Demote an existing owner first.")
        return
    if result == "__forbidden__":
        await send(client, actor["tg_uid"], "❌ Their role changed while you were deciding — the transition is no longer allowed.")
        return
    if result in ("__noop__", "__missing__"):
        await send(client, actor["tg_uid"], "ℹ️ Nothing to do — their role already matches or the user is gone.")
        return
    if result != "ok":
        await send(client, actor["tg_uid"], "❌ Failed to update role. Please try again.")
        return
    await send(client, actor["tg_uid"],
               f"✅ <code>{target['user_id']}</code> ({_esc(target['name'])}) is now "
               f"<b>{role_label(target_role)}</b>.\nAny existing panel session for them has been signed out.")
    try:
        await send(client, target["tg_uid"],
                   f"🔔 Your role is now <b>{role_label(target_role)}</b>.\n\n"
                   "Send /get_credential to fetch a fresh password.")
    except Exception:
        pass


# ─── Find User ────────────────────────────────────────────────────────────
async def cmd_find_user(client: Client, chat_id: int, user_id: str) -> None:
    """Look up a panel user by their User ID and return their Telegram details."""
    uid = user_id.strip()
    if not uid:
        await send(client, chat_id, "❌ Please provide a panel User ID.")
        return
    u = await db.get_user_by_user_id(uid)
    if not u:
        await send(client, chat_id, f"❌ No user found with User ID <code>{_esc(uid)}</code>.")
        return
    tg_uid = u.get("tg_uid")
    tg_username = u.get("tg_username")
    username_line = (f"<b>Telegram username:</b> @{_esc(tg_username)}\n"
                     if tg_username else "<b>Telegram username:</b> —\n")
    await send(client, chat_id,
               f"🔍 <b>User found</b>\n\n"
               f"<b>Panel User ID:</b> <code>{_esc(u['user_id'])}</code>\n"
               f"<b>Name:</b> {_esc(u['name'])}\n"
               f"<b>Role:</b> {role_label(u['role'])}\n"
               f"<b>Telegram UID:</b> <code>{tg_uid}</code>\n"
               f"{username_line}")


# ─── Message router ───────────────────────────────────────────────────────
_DIALOG_TEXT_KINDS = {
    "awaiting_promote_tguid", "awaiting_revoke_code", "awaiting_new_password",
    "awaiting_find_user_id", "pb_awaiting_chat", "pb_awaiting_channel",
    "pb_awaiting_broadcast", "pb_confirm_broadcast",
}


async def _on_message(client: Client, msg) -> None:
    if msg.chat.type.name != "PRIVATE":
        return
    from_user = msg.from_user
    if not from_user:
        return
    chat_id = msg.chat.id
    # Remember everyone who opens the account bot — this is the audience for the
    # owner's "Broadcast all" in bot2 (no-op after the first sighting).
    store.record_account_bot_starter(chat_id)
    text = (msg.text or "").strip()
    is_management = chat_id == HARDCODED_MANAGEMENT_ID

    m = re.match(r"^(/[a-zA-Z_]+)(?:\s+([\s\S]+))?$", text)
    cmd = m.group(1).lower() if m else None
    args = (m.group(2) or "") if m else ""

    if cmd:
        cur = _get_dialog(chat_id)
        if cur.get("kind") in _DIALOG_TEXT_KINDS:
            _set_dialog(chat_id, {"kind": "idle"})

    if cmd in ("/start", "/menu"):
        _set_dialog(chat_id, {"kind": "idle"})
        await show_start(client, chat_id, from_user)
        return
    if cmd == "/help":
        _set_dialog(chat_id, {"kind": "idle"})
        await send_help(client, chat_id)
        return
    if cmd in ("/get_credential", "/getcredential"):
        await handle_get_credential(client, chat_id)
        await send_main_menu(client, chat_id)
        return
    if cmd in ("/change_password", "/changepassword"):
        await handle_change_password_start(client, chat_id)
        return
    if cmd == "/me":
        if is_management:
            await send(client, chat_id,
                       f"<b>Role:</b> {role_label('management')}\n\nManagement is bot-only — no panel account.")
        else:
            u = await db.get_user_by_tg_uid(chat_id)
            if not u:
                await send(client, chat_id, "You don't have an account yet. Send /start.")
                return
            exp_line = f"<b>Access expires:</b> {_ts_utc(u['access_expires_at'])}\n" if u.get("access_expires_at") else ""
            await send(client, chat_id,
                       f"<b>User ID:</b> <code>{u['user_id']}</code>\n"
                       f"<b>Name:</b> {_esc(u['name'])}\n"
                       f"<b>Role:</b> {role_label(u['role'])}\n{exp_line}")
        await send_main_menu(client, chat_id)
        return
    if cmd in ("/newkey", "/keys", "/revokekey"):
        role = await effective_role(chat_id)
        if not role or role == "user":
            await send(client, chat_id, "Admins only.")
            return
        viewer = {"tg_uid": chat_id, "role": role}
        if cmd == "/newkey":
            await cmd_new_key(client, viewer, args)
        elif cmd == "/keys":
            await cmd_list_keys(client, viewer)
        else:
            await cmd_revoke_key(client, viewer, args)
        return
    if cmd == "/end":
        role = await effective_role(chat_id)
        if not role or role == "user":
            await send(client, chat_id, "Admins only.")
            return
        await cmd_indexes(client, {"tg_uid": chat_id, "role": role}, args)
        return
    if cmd == "/promote":
        role = await effective_role(chat_id)
        if role not in ("management", "owner"):
            await send(client, chat_id, "Management or owner only.")
            return
        await cmd_promote(client, {"tg_uid": chat_id, "role": role}, args)
        return
    state = _get_dialog(chat_id)
    kind = state.get("kind")
    if kind in ("pb_awaiting_broadcast", "pb_confirm_broadcast"):
        pb_role = await effective_role(chat_id)
        if pb_role in _PB_PROMO_ROLES:
            await _pb_prepare_broadcast(client, chat_id, msg)
            return
        _set_dialog(chat_id, {"kind": "idle"})
    if kind == "pb_awaiting_channel":
        pb_role = await effective_role(chat_id)
        _set_dialog(chat_id, {"kind": "idle"})
        if pb_role in _PB_PROMO_ROLES:
            await _pb_add_channel(client, chat_id, text)
            return
    if kind == "awaiting_key":
        await handle_key_submission(client, msg, text, state)
        return
    if kind == "awaiting_new_password":
        await handle_new_password_submission(client, chat_id, text)
        return
    if kind == "awaiting_promote_tguid":
        actor_role = await effective_role(chat_id)
        if actor_role not in ("management", "owner"):
            _set_dialog(chat_id, {"kind": "idle"})
            await send(client, chat_id, "Not allowed.")
            await send_main_menu(client, chat_id)
            return
        picked_role = state["role"]
        if not can_promote(actor_role, picked_role):
            _set_dialog(chat_id, {"kind": "idle"})
            await send(client, chat_id, f"❌ You cannot assign {role_label(picked_role)}.")
            await send_main_menu(client, chat_id)
            return
        raw = text.strip()
        if not re.match(r"^\d{5,15}$", raw):
            await send(client, chat_id,
                       "❌ That doesn't look like a Telegram user ID (digits only). Try again, or tap Cancel.")
            return
        target_tg_uid = int(raw)
        if target_tg_uid == HARDCODED_MANAGEMENT_ID:
            _set_dialog(chat_id, {"kind": "idle"})
            await send(client, chat_id, "❌ The management account is hardcoded and cannot be re-assigned.")
            await send_main_menu(client, chat_id)
            return
        _set_dialog(chat_id, {"kind": "idle"})
        await apply_promotion(client, {"tg_uid": chat_id, "role": actor_role},
                              target_tg_uid, picked_role)
        await send_main_menu(client, chat_id)
        return
    if kind == "awaiting_find_user_id":
        role = await effective_role(chat_id)
        if role not in ("management", "owner", "dev_admin", "base_admin"):
            _set_dialog(chat_id, {"kind": "idle"})
            await send(client, chat_id, "Not allowed.")
            await send_main_menu(client, chat_id)
            return
        _set_dialog(chat_id, {"kind": "idle"})
        await cmd_find_user(client, chat_id, text)
        await send_main_menu(client, chat_id)
        return
    if kind == "awaiting_revoke_code":
        role = await effective_role(chat_id)
        if not role or role == "user":
            _set_dialog(chat_id, {"kind": "idle"})
            await send(client, chat_id, "Not allowed.")
            await send_main_menu(client, chat_id)
            return
        _set_dialog(chat_id, {"kind": "idle"})
        await cmd_revoke_key(client, {"tg_uid": chat_id, "role": role}, text)
        await send(client, chat_id, "🔑 <b>Access Keys</b>", keys_submenu())
        return

    # ─── Auto Verify (bot1) text-input delegation ──────────────────────────
    # bot1 stores its own dialog state in the shared key-value store; check it
    # before falling through to show_start so channel-setup input is routed
    # correctly when the user is mid-flow in the Auto Verify section.
    av_state = store.get_user_state(chat_id)
    if av_state.get("kind") in ("awaiting_new_key_chat_id", "awaiting_new_key_title"):
        import bot1 as _av
        await _av._on_message(client, msg)
        return

    # ─── Notifications (bot4) text-input delegation ────────────────────────
    # bot4 keeps its own in-memory _dialogs dict; delegate when the user is
    # awaiting a channel chat ID for a notification key.
    import bot4 as _nf
    nf_state = _nf._get_dialog(chat_id)
    if nf_state.get("kind") == "nk_awaiting_chat":
        await _nf._on_message(client, msg)
        return

    # ─── Panel Bot text-input (section channel registration) ──────────────
    if kind == "pb_awaiting_chat":
        await _pb_handle_chat_input(client, chat_id, text)
        return

    await show_start(client, chat_id, from_user)


# ─── Callback dispatcher ──────────────────────────────────────────────────
async def _cb_gate(cq, allowed: list[str]) -> Optional[str]:
    role = await effective_role(cq.from_user.id)
    if not role or role not in allowed:
        await cq.answer("Not allowed for your role.", show_alert=True)
        return None
    return role


async def _on_callback(client: Client, cq) -> None:
    data = cq.data or ""
    chat_id = cq.from_user.id

    if data == "verify_channels":
        await handle_verify(client, cq)
        return

    cur = _get_dialog(chat_id)
    # pb: callbacks manage their own multi-step dialog state (channel setup,
    # broadcast confirm), so never auto-clear it out from under them.
    if cur.get("kind") in _DIALOG_TEXT_KINDS and not data.startswith("pb:"):
        _set_dialog(chat_id, {"kind": "idle"})

    if data == CB_CANCEL:
        await cq.answer()
        was_password = cur.get("kind") == "awaiting_new_password"
        _set_dialog(chat_id, {"kind": "idle"})
        if was_password:
            await send(client, chat_id,
                       "✖️ Cancelled — your current password is unchanged.")
        await send_main_menu(client, chat_id)
        return

    if data == "gc":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin", "user"])
        await cq.answer()
        if not role:
            return
        await handle_get_credential(client, chat_id)
        await send_main_menu(client, chat_id)
        return

    if data == "apk":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin", "user"])
        await cq.answer("Preparing your APK…")
        if not role:
            return
        await handle_send_apk(client, chat_id)
        return

    if data == "cpw":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin", "user"])
        await cq.answer()
        if not role:
            return
        await handle_change_password_start(client, chat_id)
        return

    if data == "me":
        await cq.answer()
        if chat_id == HARDCODED_MANAGEMENT_ID:
            await send(client, chat_id,
                       f"<b>Role:</b> {role_label('management')}\n\nManagement is bot-only — no panel account.")
        else:
            u = await db.get_user_by_tg_uid(chat_id)
            if not u:
                await send(client, chat_id, "You don't have an account yet. Send /start.")
                return
            exp_line = f"<b>Access expires:</b> {_ts_utc(u['access_expires_at'])}\n" if u.get("access_expires_at") else ""
            await send(client, chat_id,
                       f"<b>User ID:</b> <code>{u['user_id']}</code>\n"
                       f"<b>Name:</b> {_esc(u['name'])}\n"
                       f"<b>Role:</b> {role_label(u['role'])}\n{exp_line}")
        await send_main_menu(client, chat_id)
        return

    if data == "sup":
        role = await _cb_gate(cq, ["management", "owner", "dev_admin", "base_admin", "user"])
        await cq.answer()
        if not role:
            return
        sb = store.support_button()
        body = (f"💬 <b>Support</b>\n\n{_esc(sb.get('text'))} → {_esc(sb.get('url'))}"
                if sb.get("url")
                else "💬 <b>Support</b>\n\nNo support contact has been set yet.")
        await send(client, chat_id, body, [[btn("⬅️ Back", cb=CB_CANCEL)]])
        return

    if data == "fdu":
        role = await _cb_gate(cq, ["management", "owner", "dev_admin", "base_admin"])
        await cq.answer()
        if not role:
            return
        _set_dialog(chat_id, {"kind": "awaiting_find_user_id"})
        await send(client, chat_id,
                   "🔍 <b>Find User</b>\n\nSend the panel <b>User ID</b> to look up.",
                   [[btn("⬅️ Cancel", cb=CB_CANCEL)]])
        return

    if data in ("hlp", "hlp:1"):
        await cq.answer()
        await send_help(client, chat_id)
        return

    if data == "hlp:2":
        await cq.answer()
        await send_help_commands(client, chat_id)
        return

    if data == "km":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        await cq.answer()
        if not role:
            return
        await send(client, chat_id, "🔑 <b>Access Keys</b>", keys_submenu())
        return
    if data == "kls":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        await cq.answer()
        if not role:
            return
        await cmd_list_keys(client, {"tg_uid": chat_id, "role": role})
        await send(client, chat_id, "🔑 <b>Access Keys</b>", keys_submenu())
        return
    if data == "knm":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        await cq.answer()
        if not role:
            return
        await send(client, chat_id, "🆕 <b>New key — pick a role</b>",
                   role_picker_keyboard(role, "knr", "key"))
        return
    if data.startswith("knr:"):
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        if not role:
            return
        picked = parse_role(data[4:])
        if not picked or not can_create_key(role, picked):
            await cq.answer("Not allowed.", show_alert=True)
            return
        await cq.answer()
        await send(client, chat_id,
                   f"🆕 <b>New {role_label(picked)} key — pick a duration</b>",
                   duration_picker_keyboard(picked))
        return
    if data.startswith("knd:"):
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        if not role:
            return
        rest = data[4:]
        colon = rest.find(":")
        if colon < 0:
            await cq.answer("Bad selection.", show_alert=True)
            return
        picked_role = parse_role(rest[:colon])
        dur = rest[colon + 1:]
        if not picked_role or not can_create_key(role, picked_role):
            await cq.answer("Not allowed.", show_alert=True)
            return
        await cq.answer("Creating…")
        await cmd_new_key(client, {"tg_uid": chat_id, "role": role}, f"{picked_role} {dur}")
        await send(client, chat_id, "🔑 <b>Access Keys</b>", keys_submenu())
        return
    if data == "krm":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        await cq.answer()
        if not role:
            return
        if role == "owner":
            rows = await db.fetchall(
                "SELECT code, role FROM access_keys WHERE revoked = false AND redeemed_by_tg_uid IS NULL "
                "ORDER BY created_at DESC LIMIT 20"
            )
        else:
            rows = await db.fetchall(
                "SELECT code, role FROM access_keys WHERE revoked = false AND redeemed_by_tg_uid IS NULL "
                "AND created_by_tg_uid = %s ORDER BY created_at DESC LIMIT 20",
                (chat_id,),
            )
        if not rows:
            await send(client, chat_id, "No active keys to revoke.")
            await send(client, chat_id, "🔑 <b>Access Keys</b>", keys_submenu())
            return
        await send(client, chat_id, "Tap a key to revoke it:", revoke_list_keyboard(rows))
        return
    if data == "krt":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        await cq.answer()
        if not role:
            return
        _set_dialog(chat_id, {"kind": "awaiting_revoke_code"})
        await send(client, chat_id,
                   "✏️ Send the key code to revoke (e.g. <code>ZN-XXXX-XXXX</code>).",
                   [[btn("⬅️ Cancel", cb=CB_CANCEL)]])
        return
    if data.startswith("krx:"):
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        if not role:
            return
        await cmd_revoke_key(client, {"tg_uid": chat_id, "role": role}, data[4:])
        await cq.answer("Revoked")
        await send(client, chat_id, "🔑 <b>Access Keys</b>", keys_submenu())
        return

    if data == "pms":
        role = await _cb_gate(cq, ["management", "owner"])
        await cq.answer()
        if not role:
            return
        demotable = await count_demotable_for(role)
        rows: list = [[btn("➕ Promote user", cb="pma")]]
        if demotable > 0:
            rows.append([btn(f"➖ Remove promotion ({demotable})", cb="pml")])
        rows.append([btn("⬅️ Back", cb=CB_CANCEL)])
        body = "🛠 <b>Promote / Remove</b>\n\nPick what you'd like to do."
        if demotable == 0:
            body += "\n\n<i>No one is currently promoted, so there's nothing to remove yet.</i>"
        await send(client, chat_id, body, rows)
        return
    if data == "pma":
        role = await _cb_gate(cq, ["management", "owner"])
        await cq.answer()
        if not role:
            return
        await send(client, chat_id,
                   "🛠 <b>Promote — pick a role</b>\n\nWhat role should the target user get?",
                   role_picker_keyboard(role, "pmrr", "promote"))
        return
    if data.startswith("pmrr:"):
        role = await _cb_gate(cq, ["management", "owner"])
        if not role:
            return
        picked = parse_role(data[5:])
        if not picked or not can_promote(role, picked):
            await cq.answer("Not allowed.", show_alert=True)
            return
        await cq.answer()
        _set_dialog(chat_id, {"kind": "awaiting_promote_tguid", "role": picked})
        await send(client, chat_id,
                   f"🛠 <b>Promote → {role_label(picked)}</b>\n\n"
                   "Send the target's <b>Telegram user ID</b> (digits only).\n\n"
                   "If the user has never opened this bot yet, that's fine — the role will apply the moment "
                   "they tap /start, and they can fetch their login credentials from there.",
                   [[btn("⬅️ Cancel", cb=CB_CANCEL)]])
        return
    if data == "pml":
        role = await _cb_gate(cq, ["management", "owner"])
        await cq.answer()
        if not role:
            return
        back_cb = "padm" if role == "owner" else "pms"
        candidates = await db.fetchall(
            "SELECT tg_uid, user_id, name, role FROM users "
            "WHERE role IN ('owner','dev_admin','base_admin') LIMIT 40"
        )
        visible = [u for u in candidates
                   if u["tg_uid"] != HARDCODED_MANAGEMENT_ID and can_demote(role, u["role"])]
        if not visible:
            await send(client, chat_id,
                       "Nobody to remove — there are no promoted users you can demote.",
                       [[btn("⬅️ Back", cb=back_cb)]])
            return
        kb = [[btn(f"{role_emoji(u['role'])} {u['name']} — {role_label(u['role'])}",
                   cb=f"pmx:{u['tg_uid']}")] for u in visible]
        kb.append([btn("⬅️ Back", cb=back_cb)])
        await send(client, chat_id,
                   "➖ <b>Remove Admin</b>\n\nTap a user to demote them back to <b>User</b>.", kb)
        return
    if data.startswith("pmx:") and not data.startswith("pmxc:"):
        role = await _cb_gate(cq, ["management", "owner"])
        if not role:
            return
        try:
            target_tg_uid = int(data[4:])
        except ValueError:
            await cq.answer("Bad selection.", show_alert=True)
            return
        target = await db.get_user_by_tg_uid(target_tg_uid)
        if not target or target["tg_uid"] == HARDCODED_MANAGEMENT_ID or not can_demote(role, target["role"]):
            await cq.answer("Not allowed.", show_alert=True)
            return
        await cq.answer()
        await send(client, chat_id,
                   "➖ <b>Remove promotion?</b>\n\n"
                   f"<b>Name:</b> {_esc(target['name'])}\n"
                   f"<b>Telegram ID:</b> <code>{target['tg_uid']}</code>\n"
                   f"<b>Panel ID:</b> <code>{target['user_id']}</code>\n"
                   f"<b>Current role:</b> {role_label(target['role'])}\n\n"
                   "They will be demoted to <b>User</b> and any active panel session will be signed out.",
                   [[btn("✅ Confirm — demote to User", cb=f"pmxc:{target['tg_uid']}")],
                    [btn("⬅️ Cancel", cb="pml")]])
        return
    if data.startswith("pmxc:"):
        role = await _cb_gate(cq, ["management", "owner"])
        if not role:
            return
        try:
            target_tg_uid = int(data[5:])
        except ValueError:
            await cq.answer("Bad selection.", show_alert=True)
            return
        target = await db.get_user_by_tg_uid(target_tg_uid)
        if not target or target["tg_uid"] == HARDCODED_MANAGEMENT_ID or not can_demote(role, target["role"]):
            await cq.answer("Not allowed.", show_alert=True)
            return
        outcome: Optional[str] = None
        try:
            async with db.transaction() as cur:
                await cur.execute("SELECT role FROM users WHERE tg_uid = %s FOR UPDATE",
                                  (target["tg_uid"],))
                row = await cur.fetchone()
                if not row or row["role"] == "user":
                    outcome = "__noop__"
                elif not can_demote(role, row["role"]):
                    outcome = "__forbidden__"
                else:
                    prev_role = row["role"]
                    await cur.execute(
                        "UPDATE users SET role = 'user', token_version = token_version + 1, "
                        "updated_at = now() WHERE tg_uid = %s", (target["tg_uid"],),
                    )
                    await cur.execute(
                        "INSERT INTO role_events (target_tg_uid, actor_tg_uid, old_role, new_role, reason) "
                        "VALUES (%s, %s, %s, 'user', 'bot_demote')",
                        (target["tg_uid"], chat_id, prev_role),
                    )
                    outcome = "ok"
        except Exception:
            log.exception("bot3: demote failed")
            await cq.answer("Failed.", show_alert=True)
            return
        if outcome == "__forbidden__":
            await cq.answer("Their role changed — not allowed anymore.", show_alert=True)
            return
        if outcome == "__noop__":
            await cq.answer("Already User.", show_alert=True)
            return
        await cq.answer("Demoted.")
        await send(client, chat_id,
                   f"✅ <code>{target['user_id']}</code> ({_esc(target['name'])}) is now <b>User</b>.\n"
                   "Any existing panel session for them has been signed out.")
        try:
            await send(client, target["tg_uid"],
                       "🔔 Your role has been changed to <b>User</b>.\n\n"
                       "Send /get_credential to fetch a fresh password.")
        except Exception:
            pass
        await send_main_menu(client, chat_id)
        return

    # ─── Access Users ─────────────────────────────────────────────────────
    if data == "acu":
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        await cq.answer()
        if not role:
            return
        users = await db.fetchall(
            "SELECT u.tg_uid, u.user_id, u.name, u.role, u.access_expires_at, u.access_granted "
            "FROM users u "
            "JOIN access_keys k ON u.tg_uid = k.redeemed_by_tg_uid "
            "WHERE k.created_by_tg_uid = %s "
            "ORDER BY k.redeemed_at DESC LIMIT 40",
            (chat_id,),
        )
        if not users:
            await send(client, chat_id,
                       "👥 <b>Access Users</b>\n\nNo users found.",
                       [[btn("⬅️ Back", cb=CB_CANCEL)]])
            return
        await send(client, chat_id,
                   f"👥 <b>Access Users</b> — {len(users)} user(s)\n\n"
                   "✅ = active  ⛔ = expired  ♾ = unlimited\n\nTap a user to view details:",
                   access_users_list_keyboard(users))
        return

    if data.startswith("acu_u:"):
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        if not role:
            return
        try:
            target_tg_uid = int(data[6:])
        except ValueError:
            await cq.answer("Bad selection.", show_alert=True)
            return
        await cq.answer()
        u = await db.get_user_by_tg_uid(target_tg_uid)
        if not u:
            await send(client, chat_id, "❌ User not found.", [[btn("⬅️ Back", cb="acu")]])
            return
        exp_dt = u.get("access_expires_at")
        remaining = _time_remaining(exp_dt)
        expires_line = f"<b>Expires:</b> {_ts_utc(exp_dt)}\n" if exp_dt else "<b>Expires:</b> Never\n"
        await send(client, chat_id,
                   f"👤 <b>User Details</b>\n\n"
                   f"<b>Name:</b> {_esc(u['name'])}\n"
                   f"<b>Panel ID:</b> <code>{u['user_id']}</code>\n"
                   f"<b>Telegram ID:</b> <code>{target_tg_uid}</code>\n"
                   f"<b>Role:</b> {role_label(u['role'])}\n"
                   f"<b>Access:</b> {'✅ Granted' if u.get('access_granted') else '⛔ Revoked'}\n"
                   f"{expires_line}"
                   f"<b>Time remaining:</b> {remaining}",
                   [[btn("🚫 Stop User Access", cb=f"acu_stop:{target_tg_uid}")],
                    [btn("⬅️ Back", cb="acu")]])
        return

    if data.startswith("acu_stop:"):
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        if not role:
            return
        try:
            target_tg_uid = int(data[9:])
        except ValueError:
            await cq.answer("Bad selection.", show_alert=True)
            return
        await cq.answer()
        u = await db.get_user_by_tg_uid(target_tg_uid)
        if not u:
            await send(client, chat_id, "❌ User not found.", [[btn("⬅️ Back", cb="acu")]])
            return
        await send(client, chat_id,
                   f"⚠️ <b>Stop Access?</b>\n\n"
                   f"<b>{_esc(u['name'])}</b> (<code>{u['user_id']}</code>) will be immediately "
                   "logged out of the panel and their access will be revoked.",
                   [[btn("✅ Confirm — Stop Access", cb=f"acu_stop_c:{target_tg_uid}")],
                    [btn("⬅️ Cancel", cb=f"acu_u:{target_tg_uid}")]])
        return

    if data.startswith("acu_stop_c:"):
        role = await _cb_gate(cq, ["owner", "dev_admin", "base_admin"])
        if not role:
            return
        try:
            target_tg_uid = int(data[11:])
        except ValueError:
            await cq.answer("Bad selection.", show_alert=True)
            return
        u = await db.get_user_by_tg_uid(target_tg_uid)
        if not u:
            await cq.answer("User not found.", show_alert=True)
            return
        try:
            await db.execute(
                "UPDATE users SET access_granted = false, "
                "access_expires_at = now(), "
                "token_version = token_version + 1, "
                "updated_at = now() "
                "WHERE tg_uid = %s",
                (target_tg_uid,),
            )
        except Exception:
            log.exception("bot3: acu_stop_c failed")
            await cq.answer("Failed to stop access.", show_alert=True)
            return
        await cq.answer("Access stopped.")
        await send(client, chat_id,
                   f"🚫 Access for <b>{_esc(u['name'])}</b> (<code>{u['user_id']}</code>) "
                   "has been immediately revoked. Their session is now invalid.")
        try:
            await send(client, target_tg_uid,
                       "🚫 <b>Your access has been revoked.</b>\n\n"
                       "Your panel session has been ended by an admin. "
                       "Contact support if you think this was a mistake.")
        except Exception:
            pass
        await send_main_menu(client, chat_id)
        return

    # ─── Promote Admin (owner only) ───────────────────────────────────────
    if data == "padm":
        role = await _cb_gate(cq, ["owner"])
        await cq.answer()
        if not role:
            return
        base_count = await db.fetchone(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'base_admin'"
        )
        dev_count = await db.fetchone(
            "SELECT COUNT(*) AS cnt FROM users WHERE role = 'dev_admin'"
        )
        bc = (base_count or {}).get("cnt", 0)
        dc = (dev_count or {}).get("cnt", 0)
        demotable = await count_demotable_for(role)
        await send(client, chat_id,
                   f"👑 <b>Manage Admins</b>\n\n"
                   f"🔧 <b>Base Admins:</b> {bc}\n"
                   f"⚙️ <b>Dev Admins:</b> {dc}\n\n"
                   "Select an action:",
                   promote_admin_keyboard(bc, dc, demotable))
        return

    if data == "padm_b":
        role = await _cb_gate(cq, ["owner"])
        await cq.answer()
        if not role:
            return
        _set_dialog(chat_id, {"kind": "awaiting_promote_tguid", "role": "base_admin"})
        await send(client, chat_id,
                   "🔧 <b>Promote → Base Admin</b>\n\n"
                   "Send the target's <b>Telegram user ID</b> (digits only).",
                   [[btn("⬅️ Cancel", cb=CB_CANCEL)]])
        return

    if data == "padm_d":
        role = await _cb_gate(cq, ["owner"])
        await cq.answer()
        if not role:
            return
        _set_dialog(chat_id, {"kind": "awaiting_promote_tguid", "role": "dev_admin"})
        await send(client, chat_id,
                   "⚙️ <b>Promote → Dev Admin</b>\n\n"
                   "Send the target's <b>Telegram user ID</b> (digits only).",
                   [[btn("⬅️ Cancel", cb=CB_CANCEL)]])
        return

    # ─── Auto Verify (bot1) section ───────────────────────────────────────
    # av:home — entry point from the main menu.  Auto-connect the user when
    # they access Auto Verify for the first time (they already have a Zenin
    # account at this point, so the join/account checks pass immediately).
    if data == "av:home":
        await cq.answer()
        import bot1 as _av
        av_state = store.get_user_state(chat_id)
        if av_state.get("kind") == "connected":
            await _av._send_user_home(client, chat_id)
        else:
            await _av._on_join_confirmed(client, chat_id)
        return

    # verify_join / user: / owner: — bot1's interactive flow callbacks.
    if data == "verify_join" or data.startswith("user:") or data.startswith("owner:"):
        import bot1 as _av
        await _av._on_callback(client, cq)
        return

    # ─── Notifications (bot4) section ─────────────────────────────────────
    # nk: prefixed callbacks, plus del_msg / help which bot4 owns.
    if data.startswith("nk:") or data in ("del_msg", "help"):
        import bot4 as _nf
        await _nf._on_callback(client, cq)
        return

    # ─── Panel Bot (pb:) section ────────────────────────────────────────────
    if data.startswith("pb:"):
        action = data.split(":", 1)[1]
        role = await effective_role(chat_id)
        if not role or role not in _PB_ROLES:
            await cq.answer("Not available for your role.", show_alert=True)
            return
        await cq.answer()
        if action == "home":
            _set_dialog(chat_id, {"kind": "idle"})
            await _pb_send_home(client, chat_id)
        elif action == "reg":
            _set_dialog(chat_id, {"kind": "pb_awaiting_chat"})
            await _pb_prompt_for_chat(client, chat_id)
        elif action == "del":
            _set_dialog(chat_id, {"kind": "idle"})
            try:
                await db.remove_panel_section(chat_id)
            except Exception as err:
                log.warning("pb remove_panel_section failed uid=%s: %s", chat_id, err)
            await send(client, chat_id, "🗑 Your section channel has been disconnected.")
            await _pb_send_home(client, chat_id)
        elif action in ("ch_add", "ch_list", "ch_rm", "bc", "bc_go", "bc_cancel") \
                or action.startswith("ch_rmx:"):
            if role not in _PB_PROMO_ROLES:
                await send(client, chat_id, "🚫 Management / owner only.")
                return
            if action == "ch_add":
                _set_dialog(chat_id, {"kind": "pb_awaiting_channel"})
                await send(client, chat_id,
                           "➕ <b>Add channel</b>\n\n"
                           "Send the channel as <code>@handle</code> or a "
                           "<code>https://t.me/...</code> link, optionally followed "
                           "by a display title:\n<code>@mychannel My Channel</code>",
                           [[btn("✖️ Cancel", cb="pb:home")]])
            elif action == "ch_list":
                chans = store.required_channels()
                if not chans:
                    body = "📋 <b>Channels</b>\n\nNo channels configured yet."
                else:
                    body_lines = "\n".join(
                        f"{i + 1}. <b>{_esc(c.get('title'))}</b> — <code>{_esc(c.get('chatId'))}</code>"
                        + (f"\n    {_esc(c.get('inviteLink'))}" if c.get("inviteLink") else "")
                        for i, c in enumerate(chans)
                    )
                    body = f"📋 <b>Channels ({len(chans)})</b>\n\n{body_lines}"
                await send(client, chat_id, body, [[btn("⬅️ Back", cb="pb:home")]])
            elif action == "ch_rm":
                chans = store.required_channels()
                if not chans:
                    await send(client, chat_id, "No channels to remove.",
                               [[btn("⬅️ Back", cb="pb:home")]])
                else:
                    kb = [[btn(f"➖ {c.get('title')}", cb=f"pb:ch_rmx:{i}")]
                          for i, c in enumerate(chans)]
                    kb.append([btn("⬅️ Back", cb="pb:home")])
                    await send(client, chat_id, "Tap a channel to remove it:", kb)
            elif action.startswith("ch_rmx:"):
                try:
                    idx = int(action[len("ch_rmx:"):])
                except ValueError:
                    idx = -1
                removed = store.remove_required_channel_at(idx) if idx >= 0 else None
                if removed:
                    await send(client, chat_id,
                               f"✅ Removed <b>{_esc(removed.get('title'))}</b>.")
                else:
                    await send(client, chat_id, "Already gone.")
                await _pb_send_home(client, chat_id)
            elif action == "bc":
                _set_dialog(chat_id, {"kind": "pb_awaiting_broadcast"})
                await send(client, chat_id,
                           "📢 <b>Broadcast to all users</b>\n\n"
                           "Send me what you want to broadcast — text, photo, file, "
                           "voice note, audio or video.\n\n"
                           "It will be delivered to <b>everyone who has started "
                           "this bot</b>.",
                           [[btn("✖️ Cancel", cb="pb:home")]])
            elif action == "bc_go":
                st = _get_dialog(chat_id)
                if st.get("kind") != "pb_confirm_broadcast" or not st.get("content"):
                    await send(client, chat_id,
                               "Nothing to broadcast — it may have expired. "
                               "Tap 📢 Broadcast All to start again.")
                    await _pb_send_home(client, chat_id)
                    return
                content = st["content"]
                recipients = st.get("recipients") or []
                _set_dialog(chat_id, {"kind": "idle"})
                await _pb_run_broadcast(client, chat_id, content, recipients)
            elif action == "bc_cancel":
                _set_dialog(chat_id, {"kind": "idle"})
                await send(client, chat_id, "❌ Broadcast cancelled.")
                await _pb_send_home(client, chat_id)
        return

    await cq.answer()


# ─── Bootstrap management user ───────────────────────────────────────────
async def ensure_management_user() -> dict[str, bool]:
    existing = await db.get_user_by_tg_uid(HARDCODED_MANAGEMENT_ID)
    if existing:
        if existing["role"] != "management":
            await db.execute(
                "UPDATE users SET role = 'management', token_version = token_version + 1, "
                "updated_at = now() WHERE tg_uid = %s",
                (HARDCODED_MANAGEMENT_ID,),
            )
            return {"created": False, "migrated": True}
        return {"created": False, "migrated": False}
    user_id = await _generate_unique_user_id()
    pwd_hash, pwd_salt = hash_password(gen_password())
    await db.execute(
        "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, name, role, access_granted) "
        "VALUES (%s, %s, %s, %s, 'Management', 'management', true)",
        (HARDCODED_MANAGEMENT_ID, user_id, pwd_hash, pwd_salt),
    )
    return {"created": True, "migrated": False}


# ─── Legacy password backfill notice ─────────────────────────────────────
async def notify_legacy_password_users(app: Client) -> int:
    """One-time DM to legacy accounts that have no stored panel password.

    Accounts created before the plaintext-mirror column (or via key redeem /
    promote) have `panel_password = NULL`. The first "Get Credentials" tap
    issues a fresh password and invalidates whatever they may have been using.
    To avoid that surprise we proactively warn each such account exactly once,
    tracking delivery with `password_backfill_notified` so restarts don't spam.

    Returns the number of users successfully notified.
    """
    try:
        rows = await db.fetchall(
            "SELECT tg_uid, access_expires_at FROM users "
            "WHERE panel_password IS NULL "
            "AND password_backfill_notified = false "
            "AND tg_uid <> %s",
            (HARDCODED_MANAGEMENT_ID,),
        )
    except Exception:
        log.exception("bot3: failed to load legacy password users")
        return 0

    notified = 0
    for row in rows:
        # Expired accounts can't use the panel, so a fresh password is moot —
        # skip them (and leave the flag unset in case access is renewed later).
        if _expired(row.get("access_expires_at")):
            continue
        tg_uid = row["tg_uid"]
        msg = await send(
            app, tg_uid,
            "🔐 <b>Heads-up about your panel password</b>\n\n"
            "Your account was set up before we started keeping a copy of your "
            "panel password, so we can't re-show your current one.\n\n"
            "The next time you tap <b>Get Credentials</b>, a brand-new password "
            "is generated and any password you're using now will stop working.\n\n"
            "• If you already have a working panel password, keep using it — you "
            "don't need to do anything.\n"
            "• When you want a fresh one, tap <b>Get Credentials</b> and use the "
            "new password everywhere from then on.",
        )
        # Only mark notified on a successful send so an unreachable user (e.g.
        # blocked the bot) is retried on a later startup rather than silently
        # skipped. The panel_password IS NULL guard avoids racing a first tap.
        if msg is not None:
            try:
                await db.execute(
                    "UPDATE users SET password_backfill_notified = true, "
                    "updated_at = now() WHERE tg_uid = %s "
                    "AND panel_password IS NULL "
                    "AND password_backfill_notified = false",
                    (tg_uid,),
                )
                notified += 1
            except Exception:
                log.exception("bot3: failed to mark backfill notice tg_uid=%s", tg_uid)
    if notified:
        log.info("bot3: sent legacy password backfill notice to %d user(s)", notified)
    return notified


# ─── Registration ─────────────────────────────────────────────────────────
def register(app: Client) -> None:
    app.add_handler(MessageHandler(_on_message, filters.private))
    app.add_handler(CallbackQueryHandler(_on_callback))


async def on_startup(app: Client) -> None:
    try:
        mgmt = await ensure_management_user()
        if mgmt["created"]:
            log.info("bot3: management account created")
            try:
                await send(app, HARDCODED_MANAGEMENT_ID,
                           "🛠️ <b>Management account ready.</b>\n\nSend /start to see your commands.")
            except Exception:
                pass
        elif mgmt["migrated"]:
            log.info("bot3: existing user migrated to management role")
    except Exception:
        log.exception("bot3: ensure_management_user failed")
    try:
        await notify_legacy_password_users(app)
    except Exception:
        log.exception("bot3: notify_legacy_password_users failed")
