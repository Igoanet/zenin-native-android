# Zenin Telegram Bot Service

Python/Pyrogram Telegram bot backend for the **Zenin Panel** system.  
Handles user onboarding, access control, credentials delivery, SMS notifications, panel forwarding, and promotion tools — all running on a single shared Pyrogram client.

---

## Architecture

All bots share **one Pyrogram bot-mode client** (`app3`).  
`bot3` is the **sole dispatcher** — it receives every update and routes internally.

| Module | Role |
|--------|------|
| `main.py` | Entry point — initialises the single client, registers all handlers, starts the bridge |
| `bot3.py` | Portal bot — account creation, credentials, role management, Panel Bot (pb:), promotion tools |
| `bot1.py` | Auto-Verify — monitors required channels, auto-resolves pending memberships |
| `bot2.py` | Member Checker — channel-admin logic, owner-panel helpers (reused by bot3 via import) |
| `bot4.py` | Notification router — forwards dashboard alerts to user-bound channels |
| `bot5.py` | Panel forwarding — startup announcements and section-channel reminders |
| `bridge.py` | aiohttp micro-server on a Unix socket — receives `/internal/*` calls from the Node API |
| `db.py` | PostgreSQL helpers (asyncpg-style via psycopg) |
| `store.py` | JSON file store — required channels, support button, access-bot starters |
| `auth.py` | Password hashing (scrypt / Argon2id) |
| `membership.py` | Channel membership verification via the bot client |
| `sender.py` | Thin helpers: `send()`, `btn()`, `edit()` |
| `config.py` | All environment variable resolution with safe fallbacks |
| `g4f_server.py` | Internal g4f proxy (optional AI feature) |

---

## Bot Features

### Portal Bot (bot3) — all users
- `/start` — channel gate → main menu
- Get Credentials — displays panel `user_id` + password
- Change Password
- Access Keys — generate / list / revoke invite keys

### Management / Owner extras (bot3)
- Promote / Demote users
- Find User by ID
- 🗂 Panel Bot section
  - Connect / Update / Remove section channel
  - ➕ Add Channel / 📋 List Channels / ➖ Remove Channel (required-join management)
  - 📢 Broadcast All — one-tap send to all users with confirm step

### Auto-Verify (bot1)
- Watches required channels for new members
- Resolves "pending" channel entries automatically

### Bridge endpoints (Unix socket)
| Path | Description |
|------|-------------|
| `POST /internal/notify-sms-result` | DM an SMS send result to the user |
| `POST /internal/panel-send` | Forward panel summary + details to section channels |
| `POST /internal/panel-send-apk` | Forward an APK file to section channels |
| `POST /internal/notify-channel` | Post a dashboard notification to a bound channel |
| `GET  /internal/health` | Liveness check |

---

## Configuration

All values are read from environment variables / Replit Secrets:

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Main portal bot token |
| `BOT3_TOKEN` / `TG_BOT_TOKEN` | fallback | Alternative names for the same token |
| `API_ID` | ✅ | Telegram app API ID |
| `API_HASH` | ✅ | Telegram app API hash |
| `DATABASE_URL` | ✅ | PostgreSQL connection string |
| `HARDCODED_MANAGEMENT_ID` | ✅ | Telegram user ID of the super-admin |
| `DEPLOYMENT_ID` | ✅ | Unique string per deployment (for dedup) |
| `DASHBOARD_URL` | optional | URL shown in credentials messages |
| `APK_URL` | optional | Android APK download URL (defaults to GitHub Release) |
| `NOTIFY_BOT_TOKEN` | optional | Dedicated notification bot token |
| `PANEL_BOT_TOKEN` | optional | Dedicated panel-forwarding bot token |
| `BOT_SESSION_NAME` | optional | Pyrogram session name (default: `bot3`) |

A `.env` file is supported for local development.  
**Token, secret, and hash keys in `.env` are intentionally skipped** — they must come from Replit Secrets to prevent stale credential shadowing.

---

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python main.py
```

## Running with Docker

```bash
docker build -t zenin-bot .
docker run --env-file .env zenin-bot
```

---

## Database

Requires a PostgreSQL database.  
Run migrations from the `artifacts/api-server` workspace before starting the bot:

```bash
pnpm --filter @workspace/api-server run db:migrate
```

---

## Testing

```bash
pytest tests/ -v
```

---

## Notes

- **Single-client architecture**: only `bot3` registers `filters.private` handlers.  
  All other modules expose pure helper functions; `bot3` imports and calls them directly.  
  Never register a second private-message or callback handler in another module — it will be silently shadowed.

- **Spam prevention**: every button callback sets dialog state before any async send, and the confirm step for Broadcast All is explicitly gated so rapid re-taps cannot double-fire.
