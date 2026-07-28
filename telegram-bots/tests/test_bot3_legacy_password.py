"""Tests for the bot3 legacy-password safety behaviour.

Two money-sensitive flows are covered here, both otherwise only observable in
production:

  * ``notify_legacy_password_users`` — a one-time startup DM warning accounts
    that have no stored panel password (``panel_password IS NULL``) that their
    next "Get Credentials" tap mints a brand-new password. It must warn each
    eligible account *exactly once*: a restart must not re-spam an already
    notified user, management is never warned, expired accounts are skipped, and
    a failed delivery must leave the flag unset so the next startup retries it.
  * ``handle_get_credential`` — its footer must say "newly issued" the first
    time a password is minted and "current password" on every repeat tap.

The real functions are exercised with the external surface (database access +
Telegram send) replaced by in-memory fakes that honour the same SQL semantics
the production queries rely on.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bot3


# ─── fakes ──────────────────────────────────────────────────────────────────
class FakeSender:
    """Stand-in for sender.send. Records every attempt; returns None (a delivery
    failure, exactly as the real send() does when a user blocked the bot or never
    opened it) for any uid in ``fail_uids``."""

    def __init__(self, fail_uids: set[int] | None = None) -> None:
        self.fail_uids = set(fail_uids or ())
        self.calls: list[tuple[int, str]] = []
        self.sent: list[int] = []

    async def send(self, client, chat_id, text, rows=None):
        self.calls.append((chat_id, text))
        if chat_id in self.fail_uids:
            return None
        self.sent.append(chat_id)
        return object()  # a truthy "Message"

    @property
    def recipients(self) -> list[int]:
        return [uid for uid, _ in self.calls]


class FakeUsersDb:
    """In-memory ``users`` table that honours the exact WHERE clauses the legacy
    notice relies on, so the tests verify real filtering (management excluded,
    already-notified skipped, the NULL-password guard on the marker write) rather
    than a hand-waved mock."""

    def __init__(self, rows: list[dict]) -> None:
        # Keep our own mutable copies keyed by tg_uid.
        self.rows: dict[int, dict] = {r["tg_uid"]: dict(r) for r in rows}
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchall(self, sql: str, params: tuple = ()):
        # The only fetchall in notify_legacy_password_users selects un-notified
        # NULL-password accounts other than management.
        mgmt_id = params[0]
        out = []
        for r in self.rows.values():
            if (
                r.get("panel_password") is None
                and r.get("password_backfill_notified") is False
                and r["tg_uid"] != mgmt_id
            ):
                out.append(
                    {
                        "tg_uid": r["tg_uid"],
                        "access_expires_at": r.get("access_expires_at"),
                    }
                )
        return out

    async def execute(self, sql: str, params: tuple = ()):
        # The only execute here is the guarded marker write keyed by tg_uid.
        self.execute_calls.append((sql, params))
        tg_uid = params[0]
        r = self.rows.get(tg_uid)
        if (
            r is not None
            and r.get("panel_password") is None
            and r.get("password_backfill_notified") is False
        ):
            r["password_backfill_notified"] = True
            return 1
        return 0


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=30)


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


def _install(monkeypatch, db: FakeUsersDb, sender: FakeSender, mgmt_id: int = 99):
    monkeypatch.setattr(bot3, "HARDCODED_MANAGEMENT_ID", mgmt_id)
    monkeypatch.setattr(bot3, "send", sender.send)
    monkeypatch.setattr(bot3.db, "fetchall", db.fetchall)
    monkeypatch.setattr(bot3.db, "execute", db.execute)


# ─── notify_legacy_password_users ────────────────────────────────────────────
async def test_active_null_password_account_warned_exactly_once(monkeypatch):
    db = FakeUsersDb(
        [{"tg_uid": 10, "access_expires_at": _future(),
          "panel_password": None, "password_backfill_notified": False}]
    )
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    notified = await bot3.notify_legacy_password_users(object())

    assert notified == 1
    assert sender.recipients == [10]  # exactly one DM
    assert db.rows[10]["password_backfill_notified"] is True  # flag flipped


async def test_restart_does_not_respam_already_notified(monkeypatch):
    db = FakeUsersDb(
        [{"tg_uid": 10, "access_expires_at": _future(),
          "panel_password": None, "password_backfill_notified": False}]
    )
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    # First startup warns and marks the flag.
    await bot3.notify_legacy_password_users(object())
    # A restart must not re-send to an already-notified account.
    second = await bot3.notify_legacy_password_users(object())

    assert second == 0
    assert sender.recipients == [10]  # still only the original DM


async def test_preexisting_notified_flag_is_skipped(monkeypatch):
    db = FakeUsersDb(
        [{"tg_uid": 10, "access_expires_at": _future(),
          "panel_password": None, "password_backfill_notified": True}]
    )
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    notified = await bot3.notify_legacy_password_users(object())

    assert notified == 0
    assert sender.calls == []  # never DMed


async def test_management_account_is_excluded(monkeypatch):
    db = FakeUsersDb(
        [{"tg_uid": 99, "access_expires_at": _future(),
          "panel_password": None, "password_backfill_notified": False}]
    )
    sender = FakeSender()
    _install(monkeypatch, db, sender, mgmt_id=99)

    notified = await bot3.notify_legacy_password_users(object())

    assert notified == 0
    assert sender.calls == []  # management is bot-only, never warned
    assert db.rows[99]["password_backfill_notified"] is False


async def test_expired_access_account_is_skipped(monkeypatch):
    db = FakeUsersDb(
        [{"tg_uid": 10, "access_expires_at": _past(),
          "panel_password": None, "password_backfill_notified": False}]
    )
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    notified = await bot3.notify_legacy_password_users(object())

    assert notified == 0
    assert sender.calls == []  # a fresh password is moot for expired access
    # Flag left unset so the warning fires if access is renewed later.
    assert db.rows[10]["password_backfill_notified"] is False


async def test_failed_send_leaves_flag_false_and_retries_next_startup(monkeypatch):
    db = FakeUsersDb(
        [{"tg_uid": 10, "access_expires_at": _future(),
          "panel_password": None, "password_backfill_notified": False}]
    )
    failing = FakeSender(fail_uids={10})
    _install(monkeypatch, db, failing)

    notified = await bot3.notify_legacy_password_users(object())

    assert notified == 0
    assert failing.recipients == [10]  # attempted
    assert db.execute_calls == []  # never wrote a marker on a failed delivery
    assert db.rows[10]["password_backfill_notified"] is False

    # Next startup, with delivery now working, retries and succeeds.
    working = FakeSender()
    monkeypatch.setattr(bot3, "send", working.send)
    retried = await bot3.notify_legacy_password_users(object())

    assert retried == 1
    assert working.recipients == [10]
    assert db.rows[10]["password_backfill_notified"] is True


async def test_one_failure_does_not_block_other_recipients(monkeypatch):
    db = FakeUsersDb(
        [
            {"tg_uid": 10, "access_expires_at": _future(),
             "panel_password": None, "password_backfill_notified": False},
            {"tg_uid": 20, "access_expires_at": _future(),
             "panel_password": None, "password_backfill_notified": False},
            {"tg_uid": 30, "access_expires_at": _future(),
             "panel_password": None, "password_backfill_notified": False},
        ]
    )
    failing = FakeSender(fail_uids={20})
    _install(monkeypatch, db, failing)

    notified = await bot3.notify_legacy_password_users(object())

    assert notified == 2  # 10 and 30 succeed
    assert failing.recipients == [10, 20, 30]  # all attempted despite #20 failing
    assert db.rows[10]["password_backfill_notified"] is True
    assert db.rows[20]["password_backfill_notified"] is False  # the failed one retries
    assert db.rows[30]["password_backfill_notified"] is True


# ─── handle_get_credential footer ────────────────────────────────────────────
async def test_get_credential_footer_newly_issued_on_first_tap(monkeypatch):
    monkeypatch.setattr(bot3, "HARDCODED_MANAGEMENT_ID", 99)
    sender = FakeSender()
    monkeypatch.setattr(bot3, "send", sender.send)

    user = {"tg_uid": 10, "user_id": "U-10", "role": "user",
            "panel_password": None, "access_expires_at": _future()}

    async def _get_user(uid):
        return user

    async def _execute(sql, params=()):
        return 1  # the first-issuance conditional update applies

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get_user)
    monkeypatch.setattr(bot3.db, "execute", _execute)

    await bot3.handle_get_credential(object(), 10)

    assert len(sender.calls) == 1
    text = sender.calls[0][1]
    assert "newly issued" in text  # first-tap footer
    assert "current panel password" not in text


async def test_get_credential_footer_current_on_repeat_tap(monkeypatch):
    monkeypatch.setattr(bot3, "HARDCODED_MANAGEMENT_ID", 99)
    sender = FakeSender()
    monkeypatch.setattr(bot3, "send", sender.send)

    user = {"tg_uid": 10, "user_id": "U-10", "role": "user",
            "panel_password": "existingpass", "access_expires_at": _future()}

    async def _get_user(uid):
        return user

    async def _execute(sql, params=()):
        raise AssertionError("repeat tap must not write a password")

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get_user)
    monkeypatch.setattr(bot3.db, "execute", _execute)

    await bot3.handle_get_credential(object(), 10)

    assert len(sender.calls) == 1
    text = sender.calls[0][1]
    assert "current panel password" in text  # repeat-tap footer
    assert "newly issued" not in text
    assert "existingpass" in text  # the stored password is re-shown unchanged
