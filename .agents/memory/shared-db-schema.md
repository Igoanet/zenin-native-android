---
name: Shared bot/API database schema
description: One Postgres DB serves both the Python bot (raw SQL) and the Node api-server (drizzle) — the schemas conflict; migrate.py holds the union schema and is the ONLY migration runner
---

## Rule
`telegram-bots/migrate.py` (run from start.sh on every bot boot) owns the shared schema. It is the union of the bot's raw-SQL expectations and drizzle's `lib/db/src/schema/index.ts`. Never run drizzle's own migrate (lib/db/drizzle/*.sql is the API server's stale generated SQL and does NOT match what the bot expects — e.g. access_keys has `key` but bot queries `code`).

**Why:** The two codebases disagree on column names: access_keys (`key` vs `code`), role_events (`ts` vs `created_at`), users PK (`id` text vs `tg_uid`). The union schema resolves this: `id TEXT PK DEFAULT gen_random_uuid()::text` (bot inserts omit it), username nullable UNIQUE, BOTH `key` and `code` columns synced by the `sync_access_key_code()` trigger, role_events has both `ts` and `created_at`.

**How to apply:** When adding a column either codebase needs, add it to migrate.py with CREATE/ALTER IF NOT EXISTS semantics — never hand-run SQL against the Railway DB. For a fresh DB or destructive schema change, set `DB_RESET=1` env on the bot service, deploy once, then DELETE the variable immediately (it drops ALL tables on boot).
