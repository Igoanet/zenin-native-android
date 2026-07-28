"""Async Postgres access (psycopg 3) for the shared dashboard database.

Low query volume, so we open a short-lived async connection per call. Results
come back as dicts (psycopg dict_row). Use `transaction()` when a mutation and
its audit row must commit atomically (e.g. role change + role_events insert).
"""
from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator, Optional

import psycopg
from psycopg.rows import dict_row

from config import DATABASE_URL, OWNER_CAP_LOCK_KEY


async def fetchone(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


async def execute(sql: str, params: tuple = ()) -> int:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            await conn.commit()
            return cur.rowcount


@contextlib.asynccontextmanager
async def transaction() -> AsyncIterator[psycopg.AsyncCursor]:
    """Yield a cursor inside a transaction; commits on success, rolls back on error."""
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            try:
                yield cur
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise


async def acquire_owner_cap_lock(cur: psycopg.AsyncCursor) -> None:
    """Take the transaction-scoped advisory lock guarding the owner cap.

    Must be called inside ``transaction()`` before counting + writing an owner
    row so two concurrent writers cannot both pass the ``MAX_OWNERS`` check.

    **Call sites — exhaustive list of every path that creates an owner account:**

    1. ``bot3.handle_key_submission`` — key-redeem path. A new-user row is
       inserted with ``role='owner'`` when an owner access key is redeemed.
       The lock is taken after the single-use key is atomically claimed and
       before the ``COUNT(*)`` + ``INSERT INTO users``.

    2. ``bot3._apply_role_change`` — existing-user promote path. An already-
       existing user row is ``UPDATE``d to ``role='owner'`` by a bot-admin
       promotion. The lock is taken inside the same transaction, before the
       ``COUNT(*)`` + ``UPDATE users``.

    3. ``bot3.apply_promotion`` (pre-create branch) — new-user promote path.
       A brand-new user who has never sent /start is pre-created at
       ``role='owner'`` by a bot-admin promotion. The lock is taken before the
       ``COUNT(*)`` + ``INSERT INTO users``.

    **Why the list is complete (no un-locked owner-creation path exists):**

    ``bot3.cmd_new_key`` contains a defensive cap precheck for ``role='owner'``
    but that block is unreachable in practice: ``can_create_key()`` returns
    ``False`` for ``target_role='owner'`` for every caller role, so the
    ``can_create_key`` gate at the top of ``cmd_new_key`` always fires first.
    Owner access keys cannot be created via ``/newkey`` (blocked by
    ``can_create_key``) and are also disallowed by the dashboard API layer
    (``canCreateKey`` in the web client). The only way to mint an owner is
    through one of the three locked paths listed above (promotion or owner-key
    redemption, both of which require a management actor to have pre-created the
    key through the same locked path). This means a concurrent ``/newkey owner``
    call and an ``apply_promotion`` call cannot race on the owner cap — the
    ``/newkey`` path never reaches the count-then-write window at all.

    Because the three call sites above are the only code paths that commit an
    owner row, and each takes this lock before its count-then-write, two
    parallel owner-creation attempts on any combination of these paths are
    serialized by Postgres's transaction-scoped advisory lock mechanism —
    ``pg_advisory_xact_lock`` blocks the second caller until the first
    transaction commits or rolls back, at which point the second caller
    re-counts under the lock and sees the accurate final owner total.
    """
    await cur.execute("SELECT pg_advisory_xact_lock(%s)", (OWNER_CAP_LOCK_KEY,))


# ─── users ────────────────────────────────────────────────────────────────
async def get_user_by_tg_uid(tg_uid: int) -> Optional[dict[str, Any]]:
    return await fetchone("SELECT * FROM users WHERE tg_uid = %s", (tg_uid,))


async def get_user_by_user_id(user_id: str) -> Optional[dict[str, Any]]:
    return await fetchone("SELECT * FROM users WHERE user_id = %s", (user_id,))


async def count_owners() -> int:
    row = await fetchone("SELECT COUNT(*)::int AS n FROM users WHERE role = 'owner'")
    return int(row["n"]) if row else 0


async def count_users() -> int:
    row = await fetchone("SELECT COUNT(*)::int AS n FROM users")
    return int(row["n"]) if row else 0


async def list_users_by_roles(roles: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return every user row whose role is one of `roles`."""
    if not roles:
        return []
    placeholders = ",".join(["%s"] * len(roles))
    return await fetchall(
        f"SELECT * FROM users WHERE role IN ({placeholders})", tuple(roles)
    )


async def list_all_user_tg_uids() -> list[int]:
    """Return the Telegram UID of every account-bot user (deduped, non-null).

    These are everyone who onboarded through the account bot, so the account
    bot is allowed to DM each of them. Used by bot2's owner broadcast.
    """
    rows = await fetchall("SELECT DISTINCT tg_uid FROM users WHERE tg_uid IS NOT NULL")
    return [int(r["tg_uid"]) for r in rows if r.get("tg_uid") is not None]


# ─── access keys ───────────────────────────────────────────────────────────
async def get_access_key_by_code(code: str) -> Optional[dict[str, Any]]:
    return await fetchone("SELECT * FROM access_keys WHERE code = %s", (code,))


# ─── required channels (bot3, DB-backed) ──────────────────────────────────
async def list_required_channels() -> list[dict[str, Any]]:
    return await fetchall("SELECT * FROM required_channels ORDER BY created_at")


# ─── panel sections (bot5, DB-backed) ──────────────────────────────────────
# One section channel per role-holder, keyed by tg_uid. The Panel Bot forwards
# a linked-panel summary + details file to every section on the linker's
# onboarding chain (management gets every panel; owner/dev_admin get their
# subtree). The chat_id is verified (panel bot must be admin) before write.
async def get_panel_section(tg_uid: int) -> Optional[dict[str, Any]]:
    return await fetchone("SELECT * FROM panel_sections WHERE tg_uid = %s", (tg_uid,))


async def upsert_panel_section(tg_uid: int, role: str, chat_id: int, title: str) -> None:
    await execute(
        "INSERT INTO panel_sections (tg_uid, role, chat_id, title, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, now(), now()) "
        "ON CONFLICT (tg_uid) DO UPDATE SET "
        "role = EXCLUDED.role, chat_id = EXCLUDED.chat_id, "
        "title = EXCLUDED.title, updated_at = now()",
        (tg_uid, role, chat_id, title),
    )


async def remove_panel_section(tg_uid: int) -> int:
    return await execute("DELETE FROM panel_sections WHERE tg_uid = %s", (tg_uid,))


# ─── app settings ──────────────────────────────────────────────────────────
async def get_setting(key: str) -> Optional[str]:
    row = await fetchone("SELECT value FROM app_settings WHERE key = %s", (key,))
    return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    await execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, value),
    )


async def claim_setting_increase(key: str, new_value: float) -> bool:
    """Atomically raise a numeric app_setting to `new_value`, returning True iff
    this call performed the raise (the stored value was missing or strictly
    lower). Used to debounce "one per crossing" alerts: two concurrent callers
    observing the same boundary will both attempt the raise, but only the row
    actually updated returns a row, so exactly one caller "wins" and fires.

    The whole read-compare-write happens in a single statement under the row's
    lock, so there is no count-then-write race.
    """
    async with transaction() as cur:
        await cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now() "
            "WHERE app_settings.value IS NULL "
            "OR app_settings.value::double precision < EXCLUDED.value::double precision "
            "RETURNING key",
            (key, repr(float(new_value))),
        )
        return await cur.fetchone() is not None
