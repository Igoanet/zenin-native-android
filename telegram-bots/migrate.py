"""One-shot idempotent schema migration for the Zenin bot Postgres database.

Run before starting the bot service (Railway preDeployCommand):
    python3 migrate.py

All statements use CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so
re-running on an already-initialised DB is always safe.
"""
import asyncio
import logging
import os
import sys

import psycopg

log = logging.getLogger("zenin.migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    log.error("DATABASE_URL is not set — cannot run migrations")
    sys.exit(1)

MIGRATIONS = [
    # ── users ─────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS users (
        tg_uid              BIGINT      PRIMARY KEY,
        user_id             TEXT        NOT NULL UNIQUE,
        password_hash       TEXT        NOT NULL,
        password_salt       TEXT        NOT NULL,
        name                TEXT        NOT NULL DEFAULT '',
        tg_username         TEXT,
        role                TEXT        NOT NULL DEFAULT 'user',
        access_granted      BOOLEAN     NOT NULL DEFAULT false,
        access_expires_at   TIMESTAMPTZ,
        token_version       INT         NOT NULL DEFAULT 1,
        password_backfill_notified BOOLEAN NOT NULL DEFAULT false,
        panel_password      TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── access_keys ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS access_keys (
        id                  TEXT        PRIMARY KEY,
        code                TEXT        NOT NULL UNIQUE,
        role                TEXT        NOT NULL,
        label               TEXT        NOT NULL DEFAULT '',
        duration_seconds    INT         NOT NULL DEFAULT 0,
        expires_at          TIMESTAMPTZ,
        created_by_tg_uid   BIGINT      NOT NULL,
        created_by_role     TEXT        NOT NULL,
        redeemed_by_tg_uid  BIGINT,
        redeemed_at         TIMESTAMPTZ,
        revoked             BOOLEAN     NOT NULL DEFAULT false,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── role_events ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS role_events (
        id              BIGSERIAL   PRIMARY KEY,
        target_tg_uid   BIGINT      NOT NULL,
        actor_tg_uid    BIGINT,
        old_role        TEXT,
        new_role        TEXT        NOT NULL,
        reason          TEXT        NOT NULL DEFAULT '',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── panel_sections ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS panel_sections (
        tg_uid      BIGINT  PRIMARY KEY,
        role        TEXT    NOT NULL,
        chat_id     BIGINT  NOT NULL,
        title       TEXT    NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── app_settings ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key         TEXT    PRIMARY KEY,
        value       TEXT,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── required_channels ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS required_channels (
        id          BIGSERIAL   PRIMARY KEY,
        chat_id     BIGINT      NOT NULL UNIQUE,
        title       TEXT        NOT NULL DEFAULT '',
        invite_link TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── login_events ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS login_events (
        id          BIGSERIAL   PRIMARY KEY,
        tg_uid      BIGINT      NOT NULL,
        user_id     TEXT        NOT NULL,
        ip          TEXT,
        user_agent  TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── otp_sessions ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS otp_sessions (
        id          TEXT        PRIMARY KEY,
        tg_uid      BIGINT      NOT NULL,
        otp         TEXT        NOT NULL,
        expires_at  TIMESTAMPTZ NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── panel_configs ─────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS panel_configs (
        id          TEXT        PRIMARY KEY,
        tg_uid      BIGINT      NOT NULL,
        label       TEXT        NOT NULL DEFAULT '',
        config      JSONB       NOT NULL DEFAULT '{}',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── notify_settings ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS notify_settings (
        id          BIGSERIAL   PRIMARY KEY,
        tg_uid      BIGINT      NOT NULL,
        device_id   TEXT        NOT NULL,
        category    TEXT        NOT NULL,
        enabled     BOOLEAN     NOT NULL DEFAULT true,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tg_uid, device_id, category)
    )
    """,

    # ── push_tokens ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS push_tokens (
        id          BIGSERIAL   PRIMARY KEY,
        tg_uid      BIGINT      NOT NULL,
        token       TEXT        NOT NULL UNIQUE,
        device_id   TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── user_panel_data ───────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS user_panel_data (
        tg_uid      BIGINT      PRIMARY KEY,
        data        JSONB       NOT NULL DEFAULT '{}',
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── bot_store (JSON blob — used if DB-backed store is ever enabled) ───────
    """
    CREATE TABLE IF NOT EXISTS bot_store (
        key         TEXT        PRIMARY KEY,
        value       JSONB       NOT NULL DEFAULT '{}',
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]


async def run() -> None:
    log.info("connecting to database …")
    conn = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    async with conn:
        for stmt in MIGRATIONS:
            sql = stmt.strip()
            # Derive a short label from the first line for logging.
            label = sql.split("\n")[0][:80]
            try:
                await conn.execute(sql)
                log.info("OK  %s", label)
            except Exception as exc:
                log.error("FAIL %s — %s", label, exc)
                raise
    log.info("migrations complete")


if __name__ == "__main__":
    asyncio.run(run())
