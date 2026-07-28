"""Entrypoint for the Zenin Telegram bot service (Pyrogram, BOT MODE only).

Single bot, single token:
  • PortalBot  (TG_BOT_TOKEN)  — Account, Auto Verify, Notifications, Panel Backup,
                                  Member Checker, Owner channel management, Broadcast

bot1 (Auto Verify), bot2 (Owner channel management / Member Checker),
bot4 (Notifications), and bot5 (Panel Bot) are all integrated into PortalBot.
bot1.register() attaches channel-post and admin-status handlers to app3;
bot2.register() attaches owner panel + membership handlers to app3;
bot4.register() is a no-op (its routing is wired into bot3's handlers);
bot5.register() attaches the panel-section handlers to app3.
"""
from __future__ import annotations

import asyncio
import logging

from pyrogram import Client

import bot1
import bot2
import bot3
import bot4
import bot5
import bridge
import membership
from config import (
    API_HASH,
    API_ID,
    BOT3_TOKEN,
    SESSIONS_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("zenin.main")


def _make_client(name: str, token: str) -> Client:
    return Client(
        name,
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=token,
        workdir=SESSIONS_DIR,
        parse_mode=None,  # each helper sets HTML explicitly
    )


async def _maybe_on_started(mod, app: Client) -> None:
    fn = getattr(mod, "on_started", None) or getattr(mod, "on_startup", None)
    if fn is not None:
        await fn(app)


async def _safe_start(name: str, app: Client | None) -> Client | None:
    """Start one bot, but never let a single failure take down the others.

    A bot can fail to start independently (e.g. AUTH_KEY_DUPLICATED when the
    same token's session is live elsewhere). We log and return None so the rest
    of the service — including the notification bot — still comes up.

    "database is locked" is a transient race: the previous process may still
    hold a kernel file-lock on the SQLite session for a brief window after
    SIGTERM. We retry once after a short delay before giving up.
    """
    if app is None:
        return None
    for attempt in range(3):
        try:
            await app.start()
            log.info("%s started", name)
            return app
        except Exception as err:
            msg = str(err).lower()
            if "database is locked" in msg:
                delay = (attempt + 1) * 3  # 3 s, 6 s, then give up
                if attempt < 2:
                    log.warning("%s: database locked — retrying in %d s", name, delay)
                    await asyncio.sleep(delay)
                    continue
            log.error("%s failed to start: %s", name, err)
            return None
    return None


async def main() -> None:
    app3 = _make_client("bot3_account_panel", BOT3_TOKEN)

    # Register handlers before starting so no early update is missed.
    # • bot3.register() — account/panel handlers; also owns the catch-all
    #   private-message and callback-query handlers that route to bot1/bot4.
    # • bot1.register() — channel-post forwarder + admin-status watcher only.
    # • bot2.register() — owner channel management + member checker (now on Portal Bot).
    # • bot4.register() — no-op; routing is handled inside bot3.
    # • bot5.register() — panel-section handlers (section connect/remove/home).
    bot3.register(app3)
    bot1.register(app3)
    bot2.register(app3)
    bot4.register(app3)
    bot5.register(app3)

    # Start the single Portal Bot.
    app3 = await _safe_start("bot3_account_panel", app3)

    # Cross-module client wiring (after start, so failed bots wire as None).
    # Use app3 for membership checks (bot2's membership helpers now run on Portal Bot).
    membership.set_bot2_client(app3)
    # app3 is the sole bot — handles Auto Verify, Notifications, Panel Bot,
    # Member Checker, and Owner channel management functions.
    bridge.set_clients(app3, app3, notify_bot=app3, panel_bot=app3)

    if app3 is not None:
        log.info("bots started: bot3 (portal — all features)")
    else:
        log.error("no bots started — check TG_BOT_TOKEN / session duplication")

    # Run per-bot startup tasks (caches, notifications, background verifier).
    # bot1.on_started initialises its _me identity cache and the verifier loop
    # on the same client (app3) that now carries its channel handlers.
    # bot5.on_started sends the one-time "panel bot is live" announcement via app3.
    if app3 is not None:
        await _maybe_on_started(bot1, app3)
        await _maybe_on_started(bot2, app3)
        await _maybe_on_started(bot3, app3)
        await _maybe_on_started(bot5, app3)

    runner = await bridge.start_bridge()

    import store as _store

    async def _heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                _store.set_bot_heartbeat("bot3")
            except Exception:
                pass

    asyncio.create_task(_heartbeat_loop())

    log.info("zenin bot service ready")
    try:
        await asyncio.Event().wait()  # run forever
    finally:
        await runner.cleanup()
        if app3 is not None:
            try:
                await app3.stop()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
