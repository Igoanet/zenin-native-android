#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p .runtime .sessions

# Kill any previous Python bot process that may still hold SQLite file locks.
pkill -TERM -f "python main.py" 2>/dev/null || true

for i in 1 2 3 4; do
  if ! pgrep -f "python main.py" > /dev/null 2>&1; then
    break
  fi
  sleep 1
done

pkill -KILL -f "python main.py" 2>/dev/null || true
sleep 0.5

# Remove stale SQLite rollback-journal / WAL files left by unclean shutdowns.
find .sessions -name "*.session-journal" -delete 2>/dev/null || true
find .sessions -name "*.session-wal"     -delete 2>/dev/null || true
find .sessions -name "*.session-shm"     -delete 2>/dev/null || true

if ! python -c "import pyrogram, g4f" 2>/dev/null; then
  echo "[bots] Installing Python dependencies..."
  pip install -q -r requirements.txt
fi

# ── GPT4Free API server ───────────────────────────────────────────────────────
# Start g4f in the background so the Zenin API can use it as a free AI backend.
# Kill any stale instance first so we get a clean port 1337.
pkill -TERM -f "g4f_server.py" 2>/dev/null || true
sleep 0.3
pkill -KILL -f "g4f_server.py" 2>/dev/null || true

if python -c "import g4f" 2>/dev/null; then
  echo "[g4f] Starting GPT4Free API server on port 1337..."
  python g4f_server.py >> .runtime/g4f.log 2>&1 &
  disown $!
else
  echo "[g4f] g4f not installed — AI will require OPENAI_API_KEY"
fi
# ─────────────────────────────────────────────────────────────────────────────

echo "[bots] Starting Zenin Telegram bot service..."
exec python3 main.py
