"""Shared pytest setup for the telegram-bots unit tests.

The bot modules use flat imports (``import bot5``, ``import db``) relative to the
``telegram-bots`` directory, so put that directory on ``sys.path`` before any
test imports a bot module. ``config`` reads a few env vars at import time; supply
inert defaults so importing the bot never touches a real database or secret.
"""
from __future__ import annotations

import os
import sys

_BOTS_DIR = os.path.dirname(os.path.dirname(__file__))
if _BOTS_DIR not in sys.path:
    sys.path.insert(0, _BOTS_DIR)

# Inert defaults so `config` imports cleanly without real credentials. These are
# never used by the announcement code under test (no Telegram client or DB
# connection is constructed during these unit tests).
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("BOT_BRIDGE_SECRET", "test-bridge-secret")
