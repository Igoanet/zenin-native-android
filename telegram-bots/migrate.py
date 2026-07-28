"""One-shot idempotent schema migration for the Zenin shared Postgres database.

Run before starting the bot service (start.sh):
    python3 migrate.py

The schema is the UNION of what the Python bot (raw SQL) and the Node API
server (drizzle-orm) expect, so a single shared database serves both:

  • users.id            — text PK with gen_random_uuid() default, so the bot can
                          INSERT without it and the API can reference it as FK.
  • users.username      — nullable UNIQUE (bot does not set it; API does).
  • access_keys         — has BOTH `key` (drizzle) and `code` (bot) columns,
                          kept in sync by a trigger. Plus bot's redeemed_at.
  • role_events         — has BOTH ts (drizzle) and created_at (bot-era).

Set DB_RESET=1 in the service environment ONE TIME to drop and recreate all
tables (used once on the fresh database after the old schema conflict). It
refuses to run a reset without the flag, so normal deploys are always safe.
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

TABLES = [
    "users", "access_keys", "role_events", "panel_sections", "app_settings",
    "required_channels", "login_events", "otp_sessions", "panel_configs",
    "notify_settings", "push_tokens", "user_panel_data", "bot_store",
]

MIGRATIONS = [
    # ── users (union of bot + drizzle) ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS users (
        id              TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
        name            TEXT        NOT NULL DEFAULT '',
        username        TEXT        UNIQUE,
        user_id         TEXT        NOT NULL UNIQUE,
        role            TEXT        NOT NULL DEFAULT 'user',
        password_hash   TEXT        NOT NULL,
        password_salt   TEXT        NOT NULL,
        tg_username     TEXT,
        tg_chat_id      TEXT,
        tg_uid          BIGINT      UNIQUE,
        panel_password  TEXT,
        password_backfill_notified BOOLEAN NOT NULL DEFAULT false,
        access_granted  BOOLEAN     NOT NULL DEFAULT false,
        access_expires_at TIMESTAMPTZ,
        token_version   INT         NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── access_keys (key + code kept in sync by trigger below) ───────────────
    """
    CREATE TABLE IF NOT EXISTS access_keys (
        id                  TEXT        PRIMARY KEY,
        key                 TEXT        UNIQUE,
        code                TEXT        UNIQUE,
        role                TEXT        NOT NULL DEFAULT 'user',
        label               TEXT        NOT NULL DEFAULT '',
        duration_seconds    INT,
        expires_at          TIMESTAMPTZ,
        revoked             BOOLEAN     NOT NULL DEFAULT false,
        redeemed_by_tg_uid  BIGINT,
        redeemed_at         TIMESTAMPTZ,
        created_by_tg_uid   BIGINT,
        created_by_role     TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE OR REPLACE FUNCTION sync_access_key_code() RETURNS trigger AS $$
    BEGIN
        IF NEW.key IS NULL THEN NEW.key := NEW.code; END IF;
        IF NEW.code IS NULL THEN NEW.code := NEW.key; END IF;
        RETURN NEW;
    END
    $$ LANGUAGE plpgsql
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'access_keys_sync_code') THEN
            CREATE TRIGGER access_keys_sync_code
            BEFORE INSERT OR UPDATE ON access_keys
            FOR EACH ROW EXECUTE FUNCTION sync_access_key_code();
        END IF;
    END
    $$
    """,

    # ── role_events (ts + created_at both defaulted) ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS role_events (
        id              BIGSERIAL   PRIMARY KEY,
        target_tg_uid   BIGINT      NOT NULL,
        actor_tg_uid    BIGINT,
        old_role        TEXT,
        new_role        TEXT        NOT NULL,
        reason          TEXT,
        ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── panel_sections ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS panel_sections (
        tg_uid      BIGINT      PRIMARY KEY,
        chat_id     BIGINT      NOT NULL,
        title       TEXT        NOT NULL,
        role        TEXT        NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── app_settings ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        key         TEXT        PRIMARY KEY,
        value       TEXT,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── required_channels (drizzle shape; bot only SELECTs) ──────────────────
    """
    CREATE TABLE IF NOT EXISTS required_channels (
        chat_id         BIGINT      PRIMARY KEY,
        title           TEXT        NOT NULL,
        invite_link     TEXT,
        added_by_tg_uid BIGINT      NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── login_events (drizzle shape; API owns) ────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS login_events (
        id              TEXT        PRIMARY KEY,
        user_id         TEXT        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        ip_address      TEXT,
        user_agent      TEXT,
        city            TEXT,
        region          TEXT,
        country         TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_active_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        terminated_at   TIMESTAMPTZ
    )
    """,

    # ── otp_sessions (drizzle shape; API owns) ────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS otp_sessions (
        id          TEXT        PRIMARY KEY,
        user_id     TEXT        NOT NULL,
        code        TEXT        NOT NULL,
        expires_at  TIMESTAMPTZ NOT NULL,
        used        BOOLEAN     NOT NULL DEFAULT false,
        attempts    INT         NOT NULL DEFAULT 0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── panel_configs (drizzle shape; API owns) ───────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS panel_configs (
        id              TEXT        PRIMARY KEY,
        owner_id        TEXT        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name            TEXT        NOT NULL,
        firebase_url    TEXT        NOT NULL,
        firebase_secret TEXT        NOT NULL,
        is_active       BOOLEAN     NOT NULL DEFAULT true,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── notify_settings (drizzle shape; API owns) ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS notify_settings (
        tg_uid          BIGINT      NOT NULL,
        device_id       TEXT        NOT NULL,
        "transaction"   BOOLEAN     NOT NULL DEFAULT true,
        login           BOOLEAN     NOT NULL DEFAULT true,
        online_offline  BOOLEAN     NOT NULL DEFAULT true,
        enabled_at      BIGINT,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (tg_uid, device_id)
    )
    """,

    # ── push_tokens (drizzle shape; API owns) ─────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS push_tokens (
        id          BIGSERIAL   PRIMARY KEY,
        user_id     TEXT        NOT NULL,
        token       TEXT        NOT NULL,
        platform    TEXT        NOT NULL DEFAULT 'android',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS push_tokens_user_token_idx
        ON push_tokens (user_id, token)
    """,

    # ── user_panel_data (drizzle shape; API owns) ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS user_panel_data (
        user_id     TEXT        PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        data        JSON        NOT NULL DEFAULT '{}',
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    # ── bot_store (drizzle shape) ─────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS bot_store (
        key         TEXT        PRIMARY KEY,
        data        JSONB       NOT NULL DEFAULT '{}',
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]


async def run() -> None:
    log.info("connecting to database …")
    conn = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    async with conn:
        if os.environ.get("DB_RESET") == "1":
            log.warning("DB_RESET=1 — dropping all tables (one-time fresh setup)")
            for t in TABLES:
                await conn.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
            await conn.execute("DROP FUNCTION IF EXISTS sync_access_key_code() CASCADE")
            log.warning("all tables dropped")
        for stmt in MIGRATIONS:
            sql = stmt.strip()
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
