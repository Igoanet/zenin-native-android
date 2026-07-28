"""Tests for the bot3 self-service "change my panel password" flow.

``handle_new_password_submission`` is money-sensitive: it mints the credential a
user logs into the panel with. A regression here could accept an invalid
password, fail to invalidate old panel sessions (the ``token_version`` bump), or
break the plaintext mirror (``panel_password``) that the "Get Credentials"
footer relies on to re-show the same password. None of that is observable
without a live bot + DB, so we exercise the real handler with in-memory fakes
that capture exactly what it would write.

The companion ``handle_change_password_start`` simply arms the dialog; the
submission handler is where the safety-critical write happens, so that is what
these tests focus on.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

import pytest

import bot3
from auth import verify_password
from config import MAX_OWNERS


# ─── fakes ──────────────────────────────────────────────────────────────────
class FakeSender:
    """Stand-in for sender.send. Records every (chat_id, text) attempt."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def send(self, client, chat_id, text, rows=None):
        self.calls.append((chat_id, text))
        return object()  # a truthy "Message"

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.calls]


class FakeCallback:
    """Stand-in for a Pyrogram CallbackQuery. Carries the tapped button's
    ``data`` and the tapping user's id, and records ``answer`` calls so a test
    can confirm the tap was acknowledged."""

    def __init__(self, data: str, uid: int) -> None:
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False):
        self.answers.append((text, show_alert))
        return None


class FakeTxCursor:
    """Stand-in for the psycopg cursor yielded by ``db.transaction()``. Records
    every ``execute`` so a test can assert on the exact SQL + params, serves the
    ``SELECT ... FOR UPDATE`` re-read and the owner-cap ``COUNT(*)`` read through
    ``fetchone`` (honouring an optional mid-flow role override so the ``__noop__``
    / ``__forbidden__`` re-check outcomes are reachable), and applies the role
    UPDATE + role_events INSERT to the in-memory db so the role flip and audit
    row are visible to the test. It serves *both* directions: the demote path
    (``role = 'user'`` literal) and the promote path (parameterised new role)."""

    def __init__(self, db: "FakeDb") -> None:
        self._db = db
        self._last_row: dict | None = None

    async def execute(self, sql: str, params: tuple = ()):
        self._db.tx_calls.append((sql, params))
        if "FOR UPDATE" in sql:
            uid = params[0]
            if uid in self._db.tx_role_override:
                role = self._db.tx_role_override[uid]
            else:
                row = self._db.users.get(uid)
                role = row["role"] if row else None
            self._last_row = None if role is None else {"role": role}
        elif "COUNT(*)" in sql and "owner" in sql:
            # The owner-cap count read taken (under the advisory lock) inside the
            # owner-promotion transaction.
            self._last_row = {"n": self._db.owner_count}
        elif sql.lstrip().startswith("UPDATE users"):
            # Demote writes a literal ``role = 'user'`` (params = (uid,)); promote
            # parameterises the new role (params = (new_role, uid)). Honour both so
            # the in-memory row reflects whichever transition actually ran.
            if "role = %s" in sql:
                new_role, uid = params[0], params[1]
            else:
                new_role, uid = "user", params[0]
            row = self._db.users.get(uid)
            if row is not None:
                row["role"] = new_role
                row["token_version"] = row.get("token_version", 0) + 1
        elif sql.lstrip().startswith("INSERT INTO users"):
            # The pre-create path for a target with no row yet. If a test has
            # armed ``insert_users_error`` (simulating a concurrent admin who
            # committed the SAME tg_uid between our read and this INSERT), raise
            # it here — exactly as the tg_uid primary key would — so the handler
            # exercises its duplicate-key recovery. The raise propagates out of
            # the ``db.transaction()`` block, so nothing further (the
            # role_events insert) runs and no in-memory row is materialised.
            if self._db.insert_users_error is not None:
                raise self._db.insert_users_error
            # Otherwise materialise the new row in-memory so the test can assert
            # the assigned role, access_granted, and freshly-minted credential.
            tg_uid, user_id, pwd_hash, pwd_salt, name, role = params
            self._db.users[tg_uid] = {
                "tg_uid": tg_uid, "user_id": user_id, "password_hash": pwd_hash,
                "password_salt": pwd_salt, "name": name, "tg_username": None,
                "role": role, "access_granted": True, "access_expires_at": None,
                "token_version": 0,
            }
        elif "INSERT INTO role_events" in sql:
            self._db.role_events.append(params)
        return 1

    async def fetchone(self):
        return self._last_row


class FakeDb:
    """In-memory ``users`` access. ``get_user_by_tg_uid`` returns whatever row
    was seeded for the uid (or None for a missing account); ``execute`` records
    every write so a test can assert on the exact SQL + params the handler used,
    and applies the UPDATE to the seeded row so the lockstep mirror is visible.

    ``transaction()`` yields a :class:`FakeTxCursor` so the demote-confirm apply
    path (``pmxc:``) — which runs inside ``db.transaction()`` — can be exercised
    end-to-end. ``tx_role_override`` lets a test simulate the target's role
    changing *after* the initial read but *before* the in-tx ``FOR UPDATE``
    re-read, which is what the ``__noop__`` and ``__forbidden__`` outcomes guard.
    ``owner_count`` is what the in-tx owner-cap ``COUNT(*)`` read returns, so an
    owner promotion can be put under or at the cap."""

    def __init__(self, users: dict[int, dict] | None = None) -> None:
        self.users: dict[int, dict] = {uid: dict(r) for uid, r in (users or {}).items()}
        self.execute_calls: list[tuple[str, tuple]] = []
        self.tx_calls: list[tuple[str, tuple]] = []
        self.role_events: list[tuple] = []
        self.tx_role_override: dict[int, str | None] = {}
        self.owner_count: int = 0
        # When set to an exception, the pre-create ``INSERT INTO users`` raises
        # it (simulating a concurrent admin who committed the SAME tg_uid first,
        # so the tg_uid primary key rejects this duplicate). Default None = the
        # insert succeeds normally.
        self.insert_users_error: Exception | None = None

    async def get_user_by_tg_uid(self, uid: int):
        u = self.users.get(uid)
        return dict(u) if u is not None else None

    async def get_user_by_user_id(self, user_id: str):
        # Backs ``_generate_unique_user_id``'s collision check on the pre-create
        # path. None means the candidate id is free, so generation succeeds.
        for u in self.users.values():
            if u.get("user_id") == user_id:
                return dict(u)
        return None

    async def execute(self, sql: str, params: tuple = ()):
        self.execute_calls.append((sql, params))
        # Mirror the production UPDATE: (hash, salt, plaintext, tg_uid).
        pwd_hash, pwd_salt, pw, tg_uid = params
        row = self.users.get(tg_uid)
        if row is not None:
            row["password_hash"] = pwd_hash
            row["password_salt"] = pwd_salt
            row["panel_password"] = pw
            row["token_version"] = row.get("token_version", 0) + 1
        return 1

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield FakeTxCursor(self)


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=30)


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=1)


def _install(monkeypatch, db: FakeDb, sender: FakeSender, mgmt_id: int = 99):
    monkeypatch.setattr(bot3, "HARDCODED_MANAGEMENT_ID", mgmt_id)
    monkeypatch.setattr(bot3, "send", sender.send)
    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", db.get_user_by_tg_uid)
    monkeypatch.setattr(bot3.db, "get_user_by_user_id", db.get_user_by_user_id)
    monkeypatch.setattr(bot3.db, "execute", db.execute)
    monkeypatch.setattr(bot3.db, "transaction", db.transaction)
    # send_main_menu fans out to send/db; stub it so these tests stay focused on
    # the password write itself.
    async def _noop_menu(client, chat_id, header_override=None):
        return None
    monkeypatch.setattr(bot3, "send_main_menu", _noop_menu)
    # Start from a clean dialog table and arm the awaiting-password state, as the
    # real "Change Password" tap would.
    bot3._dialogs.clear()


# ─── rejection of invalid passwords ──────────────────────────────────────────
async def test_too_short_password_is_rejected_without_db_write(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    await bot3.handle_new_password_submission(object(), 10, "abc12")  # 5 chars

    assert db.execute_calls == []  # nothing written
    assert "6–64 characters" in sender.texts[0]  # re-prompted
    # Still awaiting input so the user can retry.
    assert bot3._get_dialog(10)["kind"] == "awaiting_new_password"
    assert db.users[10]["panel_password"] == "oldpass"  # untouched


async def test_too_long_password_is_rejected_without_db_write(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    await bot3.handle_new_password_submission(object(), 10, "x" * 65)  # 65 chars

    assert db.execute_calls == []
    assert "6–64 characters" in sender.texts[0]
    assert bot3._get_dialog(10)["kind"] == "awaiting_new_password"


async def test_password_with_space_is_rejected_without_db_write(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    await bot3.handle_new_password_submission(object(), 10, "valid pass")  # has a space

    assert db.execute_calls == []
    assert "no spaces" in sender.texts[0]
    assert bot3._get_dialog(10)["kind"] == "awaiting_new_password"


async def test_boundary_lengths_are_treated_consistently(monkeypatch):
    # 6 chars (the lower bound) is accepted; 5 is rejected. This pins the
    # inclusive boundary so a future "<" vs "<=" slip is caught.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    await bot3.handle_new_password_submission(object(), 10, "abcdef")  # exactly 6

    assert len(db.execute_calls) == 1  # accepted, exactly one write
    assert db.users[10]["panel_password"] == "abcdef"


# ─── a valid submission writes everything in lockstep ─────────────────────────
async def test_valid_submission_writes_mirror_and_bumps_token_version(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    await bot3.handle_new_password_submission(object(), 10, "brandnewpass")

    assert len(db.execute_calls) == 1
    sql, params = db.execute_calls[0]
    # All four columns move together in the one UPDATE.
    assert "password_hash" in sql
    assert "password_salt" in sql
    assert "panel_password" in sql
    assert "token_version = token_version + 1" in sql

    pwd_hash, pwd_salt, plaintext, tg_uid = params
    assert tg_uid == 10
    assert plaintext == "brandnewpass"  # plaintext mirror "Get Credentials" re-shows
    # The stored hash/salt actually verify against the new password (lockstep,
    # not a stale or mismatched pair).
    assert verify_password("brandnewpass", pwd_hash, pwd_salt)
    assert not verify_password("oldpass", pwd_hash, pwd_salt)

    # The row now reflects all four updated fields including the bumped version.
    assert db.users[10]["panel_password"] == "brandnewpass"
    assert db.users[10]["token_version"] == 4  # old panel sessions invalidated

    # The user is shown the new password and told the old one is dead.
    confirm = sender.texts[-1]
    assert "brandnewpass" in confirm
    assert "U-10" in confirm
    assert "no longer works" in confirm
    # Dialog is cleared so the next message isn't treated as another password.
    assert bot3._get_dialog(10)["kind"] == "idle"


# ─── missing account is handled gracefully ────────────────────────────────────
async def test_missing_account_writes_nothing_and_resets_dialog(monkeypatch):
    db = FakeDb({})  # no users at all
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(404, {"kind": "awaiting_new_password"})

    await bot3.handle_new_password_submission(object(), 404, "validpassword")

    assert db.execute_calls == []  # never wrote a password for a ghost account
    assert "don't have an account" in sender.texts[-1]
    assert bot3._get_dialog(404)["kind"] == "idle"  # dialog reset


# ─── handle_change_password_start: who is allowed to even begin ───────────────
# These guard the *gate* in front of the (separately tested) submission handler.
# Arming the dialog on the wrong account, or failing to arm it for a valid one,
# is just as dangerous as a bad write: it either lets the wrong actor in or
# silently swallows a real user's next message.
async def test_management_is_refused_and_dialog_not_armed(monkeypatch):
    # The hardcoded management account is bot-only and has no panel password, so
    # it must never enter the change-password flow.
    db = FakeDb({})
    sender = FakeSender()
    _install(monkeypatch, db, sender, mgmt_id=99)

    await bot3.handle_change_password_start(object(), 99)

    assert "bot-only" in sender.texts[-1]
    assert db.execute_calls == []
    # Critically: the dialog was NOT armed, so management's next message won't be
    # consumed as a new password.
    assert bot3._get_dialog(99)["kind"] == "idle"


async def test_missing_account_is_told_to_start_and_dialog_not_armed(monkeypatch):
    # A uid with no row gets pointed at /start, and the dialog stays disarmed so
    # a stranger can't begin setting a password for a non-existent account.
    db = FakeDb({})  # no users at all
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.handle_change_password_start(object(), 404)

    assert "/start" in sender.texts[-1]
    assert db.execute_calls == []
    assert bot3._get_dialog(404)["kind"] == "idle"


async def test_expired_access_is_refused_and_dialog_not_armed(monkeypatch):
    # An account whose access window has lapsed must be refused (and told to ask
    # for a new key) rather than allowed to mint a fresh panel password.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "role": "user",
                      "access_expires_at": _past()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.handle_change_password_start(object(), 10)

    assert "expired" in sender.texts[-1].lower()
    assert db.execute_calls == []
    assert bot3._get_dialog(10)["kind"] == "idle"


async def test_valid_active_user_is_prompted_and_dialog_armed(monkeypatch):
    # The happy path: a real, in-window account is shown the Change Password
    # prompt AND the dialog is armed so their next message is read as the new
    # password.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "role": "user",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.handle_change_password_start(object(), 10)

    assert "Change Password" in sender.texts[-1]
    assert db.execute_calls == []  # the start handler never writes
    # The dialog is now armed; the submission handler (tested above) takes over.
    assert bot3._get_dialog(10)["kind"] == "awaiting_new_password"


# ─── Cancel safely backs out of a password change ────────────────────────────
# The Cancel button (CB_CANCEL) is the user's way to keep their current
# password. If it failed to disarm the dialog, the user's *next* ordinary
# message would be silently consumed as a brand-new password — a money-sensitive
# regression. These pin the safe back-out: dialog returns to idle, nothing is
# written, and the user is reassured their password is unchanged.
async def test_cancel_resets_dialog_to_idle_without_db_write(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    cq = FakeCallback(bot3.CB_CANCEL, 10)
    await bot3._on_callback(object(), cq)

    # The tap was acknowledged and the dialog is back to idle, so the user's
    # next message is NOT read as a password.
    assert cq.answers  # cq.answer() was called
    assert bot3._get_dialog(10)["kind"] == "idle"
    # Nothing was written, and the stored password is untouched.
    assert db.execute_calls == []
    assert db.users[10]["panel_password"] == "oldpass"


async def test_cancel_tells_user_password_is_unchanged(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    await bot3._on_callback(object(), FakeCallback(bot3.CB_CANCEL, 10))

    # The user is explicitly reassured their existing password still works.
    assert any("unchanged" in t.lower() for t in sender.texts)
    assert db.execute_calls == []


async def test_cancel_after_invalid_resubmission_still_backs_out_safely(monkeypatch):
    # A user can submit an invalid password (here "abc12", too short), get
    # re-prompted with the Cancel button still shown — the dialog stays in
    # ``awaiting_new_password`` — and only then tap Cancel. That re-prompt path
    # must offer the same safe back-out as the initial prompt: dialog returns to
    # idle, nothing is written, and the user is told their password is unchanged.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "access_expires_at": _future(),
                      "panel_password": "oldpass", "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    # Invalid submission re-prompts but keeps the dialog armed (Cancel offered).
    await bot3.handle_new_password_submission(object(), 10, "abc12")  # 5 chars
    assert db.execute_calls == []
    assert bot3._get_dialog(10)["kind"] == "awaiting_new_password"

    # Now tap Cancel from that re-prompt.
    cq = FakeCallback(bot3.CB_CANCEL, 10)
    await bot3._on_callback(object(), cq)

    # Same safety holds: tap acknowledged, dialog back to idle, nothing written,
    # stored password untouched, and the user is reassured it's unchanged.
    assert cq.answers
    assert bot3._get_dialog(10)["kind"] == "idle"
    assert db.execute_calls == []
    assert db.users[10]["panel_password"] == "oldpass"
    assert any("unchanged" in t.lower() for t in sender.texts)


# ─── any unrelated menu tap also disarms an armed password dialog ─────────────
# Cancel (above) is the user's explicit back-out, but ``_on_callback`` ALSO
# defensively disarms an armed ``awaiting_new_password`` dialog when the user
# taps ANY other menu button mid-typing (the ``_DIALOG_TEXT_KINDS`` reset block
# at the top of the handler). Without it, the next routed handler runs while the
# dialog is still armed, and the user's following ordinary message could be
# silently consumed as a brand-new password. This pins that reset so a future
# refactor that moves or drops it is caught.
async def test_unrelated_button_tap_disarms_password_dialog_without_db_write(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "user",
                      "access_expires_at": _future(), "panel_password": "oldpass",
                      "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    # Tap an unrelated button ("me" = My Info) while the password dialog is armed.
    cq = FakeCallback("me", 10)
    await bot3._on_callback(object(), cq)

    # The defensive reset fired: the dialog is back to idle, so the user's next
    # message is NOT read as a password.
    assert bot3._get_dialog(10)["kind"] == "idle"
    # Nothing was written and the stored password is untouched.
    assert db.execute_calls == []
    assert db.users[10]["panel_password"] == "oldpass"


# ─── typing a command mid-password also disarms the dialog safely ─────────────
# The button-tap path is covered above, but ``_on_message`` has a SEPARATE,
# parallel reset: the same ``_DIALOG_TEXT_KINDS`` block at the top of the
# message handler disarms an armed ``awaiting_new_password`` dialog the moment a
# user types ANY slash-command, *before* routing that command. Without it, a
# typed command would run while the dialog is still armed and the user's next
# ordinary message would be silently consumed as a brand-new password — the same
# money-sensitive regression Cancel guards against, just down the message path.
#
# ``/me`` is the key case here: unlike ``/start``, ``/menu`` and ``/help`` (which
# each redundantly set the dialog idle in their own branch), ``/me`` relies
# SOLELY on the ``_DIALOG_TEXT_KINDS`` reset, so this test actually catches a
# refactor that moves or drops that block.
class FakeMsg:
    """Stand-in for a Pyrogram private-chat Message carrying typed text."""

    def __init__(self, chat_id: int, text: str, uid: int | None = None) -> None:
        self.chat = type(
            "C", (), {"id": chat_id, "type": type("T", (), {"name": "PRIVATE"})()}
        )()
        self.from_user = type("U", (), {"id": uid if uid is not None else chat_id})()
        self.text = text


@pytest.mark.parametrize("command", ["/menu", "/help", "/me"])
async def test_typed_command_disarms_password_dialog_without_db_write(monkeypatch, command):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "user",
                      "access_expires_at": _future(), "panel_password": "oldpass",
                      "token_version": 3}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    # ``/menu`` and ``/help`` fan out to these; stub them so the test stays
    # focused on the dialog reset rather than the menu/help rendering.
    async def _noop_start(client, chat_id, from_user):
        return None
    async def _noop_help(client, chat_id):
        return None
    monkeypatch.setattr(bot3, "show_start", _noop_start)
    monkeypatch.setattr(bot3, "send_help", _noop_help)

    bot3._set_dialog(10, {"kind": "awaiting_new_password"})

    await bot3._on_message(object(), FakeMsg(10, command))

    # The message-side reset fired: the dialog is back to idle, so the user's
    # next ordinary message is NOT read as a password.
    assert bot3._get_dialog(10)["kind"] == "idle"
    # No password was written and the stored password is untouched.
    assert db.execute_calls == []
    assert db.users[10]["panel_password"] == "oldpass"


# ─── typing a command mid-promote / mid-revoke also disarms safely ────────────
# ``awaiting_new_password`` (above) is only one of the kinds the ``_on_message``
# ``_DIALOG_TEXT_KINDS`` reset block guards. The same block also disarms the two
# admin dialogs — ``awaiting_promote_tguid`` (waiting for a target Telegram user
# id) and ``awaiting_revoke_code`` (waiting for a key/code to revoke) — the
# moment a user types ANY slash-command, *before* that command is routed.
# Without the reset, a typed command would run while the dialog stays armed and
# the user's NEXT ordinary message would be silently consumed as a promotion
# target uid / a revoke code — an unintended privileged action.
#
# ``/me`` is the case that actually exercises the shared block: unlike ``/start``
# and ``/menu`` (which redundantly set the dialog idle in their own branch),
# ``/me`` relies SOLELY on the ``_DIALOG_TEXT_KINDS`` reset, so these catch a
# refactor that moves or drops that block.
async def test_typed_command_disarms_promote_dialog_and_isnt_taken_as_uid(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    # If the dialog stayed armed and the command leaked through, this is what the
    # promote branch would invoke; record any call so the test can prove it isn't.
    promotions: list[tuple] = []
    async def _spy_apply_promotion(client, actor, target_tg_uid, target_role):
        promotions.append((target_tg_uid, target_role))
    monkeypatch.setattr(bot3, "apply_promotion", _spy_apply_promotion)

    bot3._set_dialog(10, {"kind": "awaiting_promote_tguid", "role": "base_admin"})

    await bot3._on_message(object(), FakeMsg(10, "/me"))

    # The reset fired: the dialog is idle, so the next ordinary message is NOT
    # read as a target uid, and the typed command was never taken as one.
    assert bot3._get_dialog(10)["kind"] == "idle"
    assert promotions == []


async def test_typed_command_disarms_revoke_dialog_and_isnt_taken_as_code(monkeypatch):
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    # The revoke branch would feed the typed text to cmd_revoke_key; record any
    # call so the test can prove the command wasn't consumed as a revoke code.
    revokes: list[str] = []
    async def _spy_cmd_revoke_key(client, viewer, args):
        revokes.append(args)
    monkeypatch.setattr(bot3, "cmd_revoke_key", _spy_cmd_revoke_key)

    bot3._set_dialog(10, {"kind": "awaiting_revoke_code"})

    await bot3._on_message(object(), FakeMsg(10, "/me"))

    # The reset fired: the dialog is idle, so the next ordinary message is NOT
    # read as a revoke code, and the typed command was never taken as one.
    assert bot3._get_dialog(10)["kind"] == "idle"
    assert revokes == []


# ─── the productive awaiting_promote_tguid branch ────────────────────────────
# The disarm tests above prove a *typed command* safely cancels an armed promote
# dialog. These cover the other side: what happens when the user types the thing
# the dialog is actually waiting for — a target Telegram user id. This branch is
# privileged (it changes someone's role) so each guard matters:
#   • the actor's role is re-checked live (a since-demoted admin must be refused),
#   • can_promote gates which role the actor may assign,
#   • the input must be 5–15 digits (a typo must re-prompt, never promote),
#   • the hardcoded management uid can never be re-assigned.
# A refactor that weakens any guard would let an unprivileged or malformed
# promotion through with nothing to catch it; these pin the behaviour.
async def test_promote_happy_path_invokes_apply_promotion_and_resets(monkeypatch):
    # An owner typing a valid target uid promotes them to the picked role: the
    # real branch parses the digits, passes the actor + target + role straight to
    # apply_promotion, and clears the dialog so the next message isn't re-read.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    promotions: list[tuple] = []
    async def _spy_apply_promotion(client, actor, target_tg_uid, target_role):
        promotions.append((actor["tg_uid"], actor["role"], target_tg_uid, target_role))
    monkeypatch.setattr(bot3, "apply_promotion", _spy_apply_promotion)

    bot3._set_dialog(10, {"kind": "awaiting_promote_tguid", "role": "base_admin"})

    await bot3._on_message(object(), FakeMsg(10, "1234567"))

    # apply_promotion got the live actor, the parsed numeric uid, and the picked
    # role from the armed dialog.
    assert promotions == [(10, "owner", 1234567, "base_admin")]
    # Dialog cleared so the user's next message isn't taken as another uid.
    assert bot3._get_dialog(10)["kind"] == "idle"


async def test_promote_non_digit_input_reprompts_without_promoting(monkeypatch):
    # A target uid must be 5–15 digits. Anything else (here a name typed by
    # mistake) is rejected with a re-prompt — and crucially the dialog stays
    # armed so the user can simply retry, and no promotion is performed.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    promotions: list[tuple] = []
    async def _spy_apply_promotion(client, actor, target_tg_uid, target_role):
        promotions.append((target_tg_uid, target_role))
    monkeypatch.setattr(bot3, "apply_promotion", _spy_apply_promotion)

    bot3._set_dialog(10, {"kind": "awaiting_promote_tguid", "role": "base_admin"})

    await bot3._on_message(object(), FakeMsg(10, "not-a-uid"))

    # No promotion happened and the dialog is still waiting for a valid uid.
    assert promotions == []
    assert bot3._get_dialog(10)["kind"] == "awaiting_promote_tguid"
    assert "Telegram user ID" in sender.texts[-1]


async def test_promote_refuses_actor_whose_role_no_longer_permits(monkeypatch):
    # The dialog was armed when the actor was privileged, but their role is
    # re-checked at submission time. Here the actor is now a plain ``user`` (e.g.
    # demoted mid-dialog): they must be refused outright and the dialog reset, so
    # a stale armed promote dialog can't be used to escalate.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "user",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    promotions: list[tuple] = []
    async def _spy_apply_promotion(client, actor, target_tg_uid, target_role):
        promotions.append((target_tg_uid, target_role))
    monkeypatch.setattr(bot3, "apply_promotion", _spy_apply_promotion)

    bot3._set_dialog(10, {"kind": "awaiting_promote_tguid", "role": "base_admin"})

    await bot3._on_message(object(), FakeMsg(10, "1234567"))

    # Refused, nothing promoted, dialog cleared.
    assert promotions == []
    assert "Not allowed" in sender.texts[-1]
    assert bot3._get_dialog(10)["kind"] == "idle"


async def test_promote_rejects_hardcoded_management_uid(monkeypatch):
    # The management account is hardcoded; even a syntactically valid uid that
    # equals it must be rejected (not promoted) and the dialog reset. mgmt_id is
    # set to a 6-digit value so it clears the ``^\d{5,15}$`` guard and actually
    # reaches the dedicated management-uid check.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender, mgmt_id=123456)
    promotions: list[tuple] = []
    async def _spy_apply_promotion(client, actor, target_tg_uid, target_role):
        promotions.append((target_tg_uid, target_role))
    monkeypatch.setattr(bot3, "apply_promotion", _spy_apply_promotion)

    bot3._set_dialog(10, {"kind": "awaiting_promote_tguid", "role": "base_admin"})

    await bot3._on_message(object(), FakeMsg(10, "123456"))  # == mgmt_id

    # No promotion of the management account; dialog cleared.
    assert promotions == []
    assert "management account is hardcoded" in sender.texts[-1]
    assert bot3._get_dialog(10)["kind"] == "idle"


# ─── can_promote: an actor can't assign a role above their own level ──────────
# The actor-role re-check (above) refuses someone who lost privilege entirely.
# This guard is finer: even a *still-privileged* actor (one that clears the
# management/owner gate and so actually reaches ``can_promote``) may only assign
# roles their own level permits. ``can_promote`` blocks an owner from assigning
# ``owner`` (no peer-level escalation) and blocks management — which may ONLY
# mint owners — from assigning any lesser admin role. Without this branch a
# refactor could let such an actor escalate a target past what their level
# allows with nothing to catch it; these pin the refusal: the "cannot assign"
# message is shown, the dialog is reset, and apply_promotion is never called.
#
# (base_admin/dev_admin can't exercise can_promote here: they're stopped earlier
# by the management/owner-only gate, covered by the "actor no longer permits"
# test above.)
@pytest.mark.parametrize(
    "actor_role,picked_role",
    [
        ("owner", "owner"),        # owner can't assign a peer-level owner
        ("management", "base_admin"),  # management may only assign owner
    ],
)
async def test_promote_refuses_role_above_actor_level(monkeypatch, actor_role, picked_role):
    # The actor clears the management/owner gate but the role the dialog is armed
    # to assign exceeds what their level may grant, so can_promote must refuse it
    # before the target uid is ever applied.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann",
                      "role": actor_role, "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    promotions: list[tuple] = []
    async def _spy_apply_promotion(client, actor, target_tg_uid, target_role):
        promotions.append((target_tg_uid, target_role))
    monkeypatch.setattr(bot3, "apply_promotion", _spy_apply_promotion)

    bot3._set_dialog(10, {"kind": "awaiting_promote_tguid", "role": picked_role})

    # Even a syntactically valid target uid must not get through.
    await bot3._on_message(object(), FakeMsg(10, "1234567"))

    # Refused by can_promote: nothing promoted, the actor is told they can't
    # assign that role, and the dialog is reset so the uid isn't re-read.
    assert promotions == []
    assert "cannot assign" in sender.texts[-1].lower()
    assert bot3._get_dialog(10)["kind"] == "idle"


# ─── the productive awaiting_revoke_code branch ──────────────────────────────
# Mirror of the promote tests for the revoke dialog. When the user types the
# key/code the dialog is waiting for, the branch re-checks the actor is not a
# plain ``user`` and only then hands the typed code to cmd_revoke_key. A weakened
# role check would let an ordinary user revoke others' keys; these pin both the
# privileged happy path and the plain-user refusal.
async def test_revoke_privileged_actor_code_reaches_cmd_revoke_key(monkeypatch):
    # A privileged actor (here a base_admin) typing a code has it passed straight
    # to cmd_revoke_key, and the dialog is reset afterward.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "base_admin",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    revokes: list[tuple] = []
    async def _spy_cmd_revoke_key(client, viewer, args):
        revokes.append((viewer["tg_uid"], viewer["role"], args))
    monkeypatch.setattr(bot3, "cmd_revoke_key", _spy_cmd_revoke_key)

    bot3._set_dialog(10, {"kind": "awaiting_revoke_code"})

    await bot3._on_message(object(), FakeMsg(10, "ZEN-ABC-123"))

    # The typed code reached cmd_revoke_key with the live actor identity.
    assert revokes == [(10, "base_admin", "ZEN-ABC-123")]
    # Dialog cleared so the next message isn't taken as another code.
    assert bot3._get_dialog(10)["kind"] == "idle"


async def test_revoke_plain_user_is_refused_and_dialog_resets(monkeypatch):
    # A plain ``user`` must never revoke keys. Even with the dialog armed, the
    # live role re-check refuses them, the dialog resets, and cmd_revoke_key is
    # never reached.
    db = FakeDb({10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "user",
                      "access_expires_at": _future()}})
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    revokes: list[tuple] = []
    async def _spy_cmd_revoke_key(client, viewer, args):
        revokes.append(args)
    monkeypatch.setattr(bot3, "cmd_revoke_key", _spy_cmd_revoke_key)

    bot3._set_dialog(10, {"kind": "awaiting_revoke_code"})

    await bot3._on_message(object(), FakeMsg(10, "ZEN-ABC-123"))

    # Refused: no revoke, dialog cleared.
    assert revokes == []
    assert "Not allowed" in sender.texts[-1]
    assert bot3._get_dialog(10)["kind"] == "idle"


# ─── can_demote: who may strip another user's role ───────────────────────────
# Task #85 pinned the *assign* guard (can_promote — an actor can't grant a role
# above their own level). This is its mirror image: can_demote decides who may
# strip another user's role. A refactor that weakens or drops it would let a
# lower-privileged admin demote someone above their own level with nothing to
# catch it. These tests exercise the function directly because demotion has no
# productive ``awaiting_*`` text branch in ``_on_message`` — it is driven by the
# button-confirm callback flow (``pmx:`` → ``pmxc:``), whose refusal is
# exercised end-to-end below.
@pytest.mark.parametrize(
    "actor_role,target_role",
    [
        # An owner may demote lesser admins but NOT a peer-level owner: no
        # owner-on-owner demotion (only management may strip an owner).
        ("owner", "owner"),
        # Neither lesser admin may demote anyone at all — not a peer, not a
        # user, and certainly not someone above them.
        ("base_admin", "owner"),
        ("base_admin", "dev_admin"),
        ("base_admin", "base_admin"),
        ("dev_admin", "owner"),
        ("dev_admin", "dev_admin"),
        ("dev_admin", "base_admin"),
        # The hardcoded management role is never a demotion *target* for anyone,
        # including management itself.
        ("management", "management"),
        ("owner", "management"),
        # A plain user has no role to strip, so demotion is always a no-op/refusal
        # regardless of who asks.
        ("management", "user"),
        ("owner", "user"),
        ("base_admin", "user"),
    ],
)
def test_can_demote_refuses(actor_role, target_role):
    assert bot3.can_demote(actor_role, target_role) is False


@pytest.mark.parametrize(
    "actor_role,target_role",
    [
        # Only management may strip an owner.
        ("management", "owner"),
        # Management may also strip the lesser admins.
        ("management", "dev_admin"),
        ("management", "base_admin"),
        # An owner may strip the lesser admins (just not a peer owner).
        ("owner", "dev_admin"),
        ("owner", "base_admin"),
    ],
)
def test_can_demote_allows(actor_role, target_role):
    assert bot3.can_demote(actor_role, target_role) is True


# ─── the productive demote refusal, end-to-end through the callback flow ──────
# Demotion is initiated by tapping a user in the "Remove promotion" list
# (callback ``pmx:<tg_uid>``), which re-checks can_demote live before ever
# showing the confirm prompt. These mirror the promote refusal tests: a still-
# privileged owner tapping a *peer owner* is refused before the confirm step,
# and a base_admin can't even reach the demote flow (the role gate stops them).
# In both cases no confirm prompt is sent, so the demotion apply path
# (``pmxc:`` → the role-stripping UPDATE) is never reached.
async def test_owner_cannot_demote_peer_owner_via_callback(monkeypatch):
    # Actor is an owner; target (uid 20) is also an owner. The tap clears the
    # management/owner role gate, but can_demote refuses owner-on-owner, so the
    # handler answers "Not allowed" and never sends the confirm prompt.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "role": "owner",
             "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    cq = FakeCallback("pmx:20", 10)
    await bot3._on_callback(object(), cq)

    # Refused at the can_demote check: the tap is answered "Not allowed" and no
    # confirm prompt ("Remove promotion?") is ever shown.
    assert ("Not allowed.", True) in cq.answers
    assert sender.calls == []  # the demotion apply path was never reached
    # The peer owner's role is untouched.
    assert db.users[20]["role"] == "owner"


async def test_base_admin_cannot_reach_demote_flow_via_callback(monkeypatch):
    # A base_admin is not in the management/owner gate for the demote callbacks,
    # so tapping a demote target is refused by the gate before can_demote (and
    # before any confirm prompt or apply).
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "base_admin",
             "access_expires_at": _future()},
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "role": "dev_admin",
             "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    cq = FakeCallback("pmx:20", 10)
    await bot3._on_callback(object(), cq)

    # The role gate refused the tap; no confirm prompt, no demotion.
    assert any(not ok or "not allowed" in t.lower() for t, ok in cq.answers)
    assert sender.calls == []
    assert db.users[20]["role"] == "dev_admin"


# ─── the productive demote apply, end-to-end through the confirm callback ─────
# The refusal tests above stop *before* the confirm step. These cover the other
# side: an allowed actor taps "✅ Confirm" (callback ``pmxc:<tg_uid>``), driving
# the money/permission-sensitive apply path inside ``db.transaction()``. That
# path must, atomically: flip the target's role back to ``user``, bump
# ``token_version`` (signing out the demoted user's active panel session), and
# write a ``role_events`` audit row with the correct prev/new role + actor. A
# refactor could silently drop the session-invalidation bump or the audit insert
# with nothing to catch it; these pin all three moving together.
async def test_owner_demote_confirm_strips_role_bumps_token_and_audits(monkeypatch):
    # An owner confirms demoting a dev_admin. The in-tx FOR UPDATE re-read still
    # sees ``dev_admin`` (no mid-flow change), so the apply runs in full.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "tg_username": "bob",
             "role": "dev_admin", "token_version": 5, "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    cq = FakeCallback("pmxc:20", 10)
    await bot3._on_callback(object(), cq)

    # The role was stripped back to user and the session-invalidating bump fired.
    assert db.users[20]["role"] == "user"
    assert db.users[20]["token_version"] == 6  # active panel session signed out

    # Exactly one audit row, capturing prev/new role + the acting owner.
    assert len(db.role_events) == 1
    target_tg_uid, target_user_id, target_name, target_username, prev_role, \
        actor_tg_uid, actor_role = db.role_events[0]
    assert target_tg_uid == 20
    assert target_user_id == "U-20"
    assert target_name == "Bob"
    assert target_username == "bob"
    assert prev_role == "dev_admin"   # captured before the flip
    assert actor_tg_uid == 10         # the owner who confirmed
    assert actor_role == "owner"

    # new_role is hardcoded in the audit SQL (not a param), so pin it on the
    # recorded statement so a refactor that changes the audited new role is caught.
    audit_sql = next(s for s, _ in db.tx_calls if "INSERT INTO role_events" in s)
    assert "'user'" in audit_sql

    # The tap was acknowledged as a successful demotion.
    assert ("Demoted.", False) in cq.answers


async def test_management_demote_confirm_strips_owner_and_audits(monkeypatch):
    # Management (the hardcoded account, recognised by uid, not a db row) confirms
    # demoting an owner — the only actor allowed to strip an owner. The full apply
    # must run and the audit row must record management as the actor.
    db = FakeDb({
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "tg_username": "bob",
             "role": "owner", "token_version": 2, "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender, mgmt_id=99)

    cq = FakeCallback("pmxc:20", 99)  # uid == HARDCODED_MANAGEMENT_ID
    await bot3._on_callback(object(), cq)

    assert db.users[20]["role"] == "user"
    assert db.users[20]["token_version"] == 3

    assert len(db.role_events) == 1
    target_tg_uid, _uid, _name, _uname, prev_role, actor_tg_uid, actor_role = \
        db.role_events[0]
    assert target_tg_uid == 20
    assert prev_role == "owner"       # an owner was stripped
    assert actor_tg_uid == 99         # by management
    assert actor_role == "management"

    assert ("Demoted.", False) in cq.answers


async def test_demote_confirm_noop_when_target_already_user(monkeypatch):
    # The dialog was armed against a dev_admin, but by the time the confirm tap
    # lands the target is already a plain ``user`` (the in-tx FOR UPDATE re-read
    # sees ``user``). The handler must answer ``__noop__`` — no UPDATE, no audit
    # row — rather than redundantly "demote" an already-demoted user.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "role": "dev_admin",
             "token_version": 5, "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    db.tx_role_override[20] = "user"  # target demoted by someone else mid-flow

    cq = FakeCallback("pmxc:20", 10)
    await bot3._on_callback(object(), cq)

    # Answered as a no-op; nothing applied and nothing audited.
    assert ("Already User.", True) in cq.answers
    assert db.role_events == []
    assert db.users[20]["role"] == "dev_admin"   # row untouched (no UPDATE ran)
    assert db.users[20]["token_version"] == 5     # no session-invalidation bump


async def test_demote_confirm_forbidden_when_target_role_changed_mid_flow(monkeypatch):
    # The owner armed the confirm against a demotable dev_admin, but mid-flow the
    # target became an owner (which an owner may NOT demote). The in-tx re-check
    # must catch this and answer ``__forbidden__`` — no UPDATE, no audit row — so
    # a stale confirm can't strip a role the actor isn't allowed to touch.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "role": "dev_admin",
             "token_version": 5, "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    db.tx_role_override[20] = "owner"  # target became an owner mid-flow

    cq = FakeCallback("pmxc:20", 10)
    await bot3._on_callback(object(), cq)

    # Refused by the in-tx re-check; nothing applied and nothing audited.
    assert ("Their role changed — not allowed anymore.", True) in cq.answers
    assert db.role_events == []
    assert db.users[20]["role"] == "dev_admin"   # row untouched
    assert db.users[20]["token_version"] == 5


# ─── the productive promote apply, end-to-end through apply_promotion ─────────
# The demote apply tests above pin the role-stripping write. These cover the
# opposite, money/permission-sensitive direction: an allowed actor promoting a
# user. ``apply_promotion`` runs the real write inside ``db.transaction()`` via
# ``_apply_role_change``, which must atomically: set the target's new role, bump
# ``token_version`` (signing out any active panel session so the new privileges
# are minted fresh), and write a ``role_events`` audit row with the correct
# prev/new role + actor. A refactor could silently drop the session bump or the
# audit insert with nothing to catch it; these pin all three moving together.
#
# Unlike the owner path (covered below), a standard non-owner promotion must NOT
# take the owner-cap advisory lock or read the owner count — locking unrelated
# promotions would needlessly serialize them.
async def test_owner_promote_to_dev_admin_sets_role_bumps_token_and_audits(monkeypatch):
    # An owner promotes a plain user to dev_admin. The in-tx FOR UPDATE re-read
    # still sees ``user`` (no mid-flow change), so the apply runs in full.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "tg_username": "bob",
             "role": "user", "token_version": 5, "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.apply_promotion(object(), {"tg_uid": 10, "role": "owner"}, 20, "dev_admin")

    # The target's role is actually set and the session-invalidating bump fired.
    assert db.users[20]["role"] == "dev_admin"
    assert db.users[20]["token_version"] == 6  # active panel session signed out

    # A standard promotion is not owner-bound, so no advisory lock / owner count.
    assert not any("pg_advisory_xact_lock" in s for s, _ in db.tx_calls)
    assert not any("COUNT(*)" in s for s, _ in db.tx_calls)

    # Exactly one audit row, capturing prev/new role + the acting owner. The
    # promote audit parameterises BOTH prev and new role (8 params), unlike the
    # demote audit which hardcodes the new role.
    assert len(db.role_events) == 1
    target_tg_uid, target_user_id, target_name, target_username, prev_role, \
        new_role, actor_tg_uid, actor_role = db.role_events[0]
    assert target_tg_uid == 20
    assert target_user_id == "U-20"
    assert target_name == "Bob"
    assert target_username == "bob"
    assert prev_role == "user"        # captured before the flip
    assert new_role == "dev_admin"    # the assigned role
    assert actor_tg_uid == 10         # the owner who promoted
    assert actor_role == "owner"

    # The requester is told it worked, naming the new role.
    assert any("Dev Admin" in t and "U-20" in t for t in sender.texts)


async def test_promote_noop_when_target_already_has_role(monkeypatch):
    # The target is already a dev_admin by the time apply_promotion runs, so the
    # early in-handler check short-circuits: no transaction, no UPDATE, no audit.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "role": "dev_admin",
             "token_version": 5, "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.apply_promotion(object(), {"tg_uid": 10, "role": "owner"}, 20, "dev_admin")

    # Nothing applied and nothing audited; the row is untouched.
    assert db.role_events == []
    assert db.tx_calls == []
    assert db.users[20]["role"] == "dev_admin"
    assert db.users[20]["token_version"] == 5
    assert any("already" in t.lower() for t in sender.texts)


# ─── the owner-cap path: full is refused, under the cap succeeds ──────────────
# Promoting to ``owner`` is capped at ``MAX_OWNERS``. The decision is made inside
# the transaction, under the advisory lock, against a live ``COUNT(*)``. These
# pin both outcomes end-to-end through apply_promotion: at the cap the write is
# refused (no role change, no audit row), and under the cap the full apply runs.
async def test_owner_promotion_at_cap_is_refused_with_no_write(monkeypatch):
    db = FakeDb({
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "tg_username": "bob",
             "role": "user", "token_version": 5, "access_expires_at": _future()},
    })
    db.owner_count = MAX_OWNERS  # already full
    sender = FakeSender()
    # Only management may assign the owner role.
    _install(monkeypatch, db, sender, mgmt_id=99)

    await bot3.apply_promotion(object(), {"tg_uid": 99, "role": "management"}, 20, "owner")

    # The cap check fired before any write: no role change, no audit row.
    assert db.users[20]["role"] == "user"
    assert db.users[20]["token_version"] == 5
    assert db.role_events == []
    assert not any(s.lstrip().startswith("UPDATE users") for s, _ in db.tx_calls)
    # The lock + count still happened (that is how the cap was detected).
    assert any("pg_advisory_xact_lock" in s for s, _ in db.tx_calls)
    assert any("COUNT(*)" in s for s, _ in db.tx_calls)
    # The requester gets a clear refusal naming the cap.
    refusal = sender.texts[-1]
    assert "Owner cap reached" in refusal
    assert str(MAX_OWNERS) in refusal


async def test_owner_promotion_under_cap_succeeds_and_audits(monkeypatch):
    db = FakeDb({
        20: {"tg_uid": 20, "user_id": "U-20", "name": "Bob", "tg_username": "bob",
             "role": "user", "token_version": 5, "access_expires_at": _future()},
    })
    db.owner_count = MAX_OWNERS - 1  # one slot free
    sender = FakeSender()
    _install(monkeypatch, db, sender, mgmt_id=99)

    await bot3.apply_promotion(object(), {"tg_uid": 99, "role": "management"}, 20, "owner")

    # Under the cap the full apply runs: role set, session bumped, audit written.
    assert db.users[20]["role"] == "owner"
    assert db.users[20]["token_version"] == 6
    # The cap was genuinely consulted under the lock before the write.
    lock_at = next(i for i, (s, _) in enumerate(db.tx_calls) if "pg_advisory_xact_lock" in s)
    count_at = next(i for i, (s, _) in enumerate(db.tx_calls) if "COUNT(*)" in s)
    update_at = next(i for i, (s, _) in enumerate(db.tx_calls) if s.lstrip().startswith("UPDATE users"))
    assert lock_at < count_at < update_at

    assert len(db.role_events) == 1
    _tg, _uid, _name, _uname, prev_role, new_role, actor_tg_uid, actor_role = \
        db.role_events[0]
    assert prev_role == "user"
    assert new_role == "owner"
    assert actor_tg_uid == 99
    assert actor_role == "management"

    assert any("Owner" in t and "U-20" in t for t in sender.texts)


# ─── promoting a brand-new (not-yet-joined) target: the pre-create path ───────
# All the apply tests above promote a target that ALREADY has a ``users`` row.
# ``apply_promotion`` has a SECOND, equally money/permission-sensitive write
# path: when the target has never opened the bot (no row yet), it pre-creates
# the account inside ``db.transaction()`` — minting a unique user id + a fresh
# hashed password, INSERTing the ``users`` row with the assigned role and
# ``access_granted = true``, and writing a ``role_events`` audit row with a NULL
# prev_role. A refactor could silently mis-set the role, drop ``access_granted``,
# or skip the audit insert on this branch with nothing to catch it. These pin
# the full pre-create write, plus the owner-cap guard that gates it.
async def test_promote_new_user_precreates_row_with_role_and_audits(monkeypatch):
    # An owner promotes a uid that has no account yet to dev_admin. The handler
    # finds no existing row and takes the pre-create branch.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.apply_promotion(object(), {"tg_uid": 10, "role": "owner"}, 777, "dev_admin")

    # A brand-new row was inserted for the target, carrying the assigned role and
    # an active access grant so the role is immediately live.
    new_row = db.users[777]
    assert new_row["role"] == "dev_admin"
    assert new_row["access_granted"] is True
    assert new_row["name"] == bot3.placeholder_name(777)
    # A real credential was minted: a well-formed, non-empty hash/salt pair (not
    # blank/None). verify_password must run cleanly against it (a wrong guess
    # returns False rather than raising on a malformed pair).
    assert isinstance(new_row["password_hash"], str) and new_row["password_hash"]
    assert isinstance(new_row["password_salt"], str) and new_row["password_salt"]
    assert verify_password("definitely-not-the-password",
                           new_row["password_hash"], new_row["password_salt"]) is False

    # A standard (non-owner) pre-create must NOT take the owner-cap lock/count —
    # locking unrelated promotions would needlessly serialize them.
    assert not any("pg_advisory_xact_lock" in s for s, _ in db.tx_calls)
    assert not any("COUNT(*)" in s for s, _ in db.tx_calls)

    # Exactly one audit row. Unlike the existing-user promote audit (which
    # parameterises prev_role + target_username), the pre-create audit hardcodes
    # both as NULL in the SQL, so only 6 params are bound: target_tg_uid,
    # target_user_id, target_name, new_role, actor_tg_uid, actor_role.
    assert len(db.role_events) == 1
    target_tg_uid, target_user_id, target_name, new_role, actor_tg_uid, actor_role = \
        db.role_events[0]
    assert target_tg_uid == 777
    assert target_user_id == new_row["user_id"]  # the freshly minted id
    assert target_name == bot3.placeholder_name(777)
    assert new_role == "dev_admin"
    assert actor_tg_uid == 10
    assert actor_role == "owner"

    # The requester is told it worked, naming the new role + minted panel id.
    assert any("Dev Admin" in t and new_row["user_id"] in t for t in sender.texts)


async def test_promote_new_owner_at_cap_inserts_nothing_and_no_audit(monkeypatch):
    # Management pre-creating a brand-new OWNER while the cap is already full:
    # the in-tx advisory lock + COUNT(*) detect the cap and the insert is
    # abandoned — no users row, no audit row — with a clear refusal to the actor.
    db = FakeDb({})  # target 777 has no row
    db.owner_count = MAX_OWNERS  # already full
    sender = FakeSender()
    _install(monkeypatch, db, sender, mgmt_id=99)

    await bot3.apply_promotion(object(), {"tg_uid": 99, "role": "management"}, 777, "owner")

    # The cap fired before any write: no new user, no audit row.
    assert 777 not in db.users
    assert db.role_events == []
    assert not any(s.lstrip().startswith("INSERT INTO users") for s, _ in db.tx_calls)
    # The lock + count still happened (that is how the cap was detected).
    assert any("pg_advisory_xact_lock" in s for s, _ in db.tx_calls)
    assert any("COUNT(*)" in s for s, _ in db.tx_calls)
    # The requester gets a clear refusal naming the cap.
    refusal = sender.texts[-1]
    assert "Owner cap reached" in refusal
    assert str(MAX_OWNERS) in refusal


async def test_promote_new_owner_under_cap_precreates_and_audits(monkeypatch):
    # Management pre-creating a brand-new OWNER with one slot free: the full
    # pre-create runs under the lock — row inserted as owner, audit written —
    # and the lock + count happen BEFORE the insert (so the cap is genuinely
    # consulted, not checked after the fact).
    db = FakeDb({})  # target 777 has no row
    db.owner_count = MAX_OWNERS - 1  # one slot free
    sender = FakeSender()
    _install(monkeypatch, db, sender, mgmt_id=99)

    await bot3.apply_promotion(object(), {"tg_uid": 99, "role": "management"}, 777, "owner")

    new_row = db.users[777]
    assert new_row["role"] == "owner"
    assert new_row["access_granted"] is True

    # The cap was consulted under the lock BEFORE the insert.
    lock_at = next(i for i, (s, _) in enumerate(db.tx_calls) if "pg_advisory_xact_lock" in s)
    count_at = next(i for i, (s, _) in enumerate(db.tx_calls) if "COUNT(*)" in s)
    insert_at = next(i for i, (s, _) in enumerate(db.tx_calls)
                     if s.lstrip().startswith("INSERT INTO users"))
    assert lock_at < count_at < insert_at

    # One audit row. The pre-create audit binds 6 params (prev_role + username
    # are hardcoded NULL in the SQL): target_tg_uid, target_user_id,
    # target_name, new_role, actor_tg_uid, actor_role.
    assert len(db.role_events) == 1
    _tg, _uid, _name, new_role, actor_tg_uid, actor_role = db.role_events[0]
    assert new_role == "owner"
    assert actor_tg_uid == 99
    assert actor_role == "management"

    assert any("Owner" in t and new_row["user_id"] in t for t in sender.texts)


# ─── pre-create when a unique user id can't be minted ─────────────────────────
# The pre-create path mints a fresh panel id via ``_generate_unique_user_id``,
# which retries up to ``_USER_ID_ATTEMPTS`` times (widening the id space as it
# goes) and then raises ``RuntimeError`` if every candidate collides. That call
# sits BEFORE the insert transaction, so an unhandled raise would bubble out of
# ``apply_promotion`` — the promoting admin gets no feedback and the action
# silently dies. This pins the friendly-failure contract: the actor is told to
# try again, and nothing (no ``users`` row, no ``role_events`` audit, no
# transaction) is written.
async def test_promote_new_user_id_exhaustion_fails_cleanly_without_writes(monkeypatch):
    # An owner promotes a brand-new uid, but every minted candidate id collides
    # with an existing user, so ``_generate_unique_user_id`` exhausts its retries.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
        # The id every candidate will collide with.
        55: {"tg_uid": 55, "user_id": "1234567", "name": "Taken", "role": "user",
             "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)
    # Force every generated candidate to be the already-taken id, so every
    # collision check fails (across every widened width) and the helper raises
    # RuntimeError.
    monkeypatch.setattr(bot3, "gen_user_id", lambda *_a, **_k: "1234567")

    await bot3.apply_promotion(object(), {"tg_uid": 10, "role": "owner"}, 777, "dev_admin")

    # No crash bubbled out: the actor is told to try again.
    assert "Please try again" in sender.texts[-1]

    # Nothing was written: no new user row, no audit row, and the insert
    # transaction was never even opened.
    assert 777 not in db.users
    assert db.role_events == []
    assert db.tx_calls == []


# ─── id generation widens the address space so exhaustion stays rare ──────────
# As the user base grows, random 7-digit ids collide more often. Rather than
# burning a fixed handful of retries inside one cramped 9M-id space and failing,
# ``_generate_unique_user_id`` widens by a digit (10x the space) every few
# collisions. These pin that headroom so a future refactor can't quietly shrink
# the retry budget or drop the widening back to the old fixed-width behaviour.
def test_gen_user_id_honours_requested_width_with_nonzero_lead():
    for digits in (7, 8, 12):
        seen = {bot3.gen_user_id(digits) for _ in range(50)}
        for candidate in seen:
            assert len(candidate) == digits  # exactly the requested width
            assert candidate[0] != "0"  # never a leading zero
            assert candidate.isdigit()


async def test_generate_unique_user_id_widens_when_narrow_space_is_saturated(monkeypatch):
    # Simulate a saturated 7-digit space: every 7-digit candidate is taken, so
    # the helper must widen to 8 digits to find a free id rather than give up.
    async def _get(candidate: str):
        # Pretend any 7-digit id already exists; anything wider is free.
        return {"user_id": candidate} if len(candidate) <= 7 else None

    monkeypatch.setattr(bot3.db, "get_user_by_user_id", _get)

    result = await bot3._generate_unique_user_id()

    # It found a free id only by widening past 7 digits — proof the widening
    # kicked in instead of exhausting the cramped space.
    assert len(result) > 7
    assert result.isdigit()
    assert result[0] != "0"


async def test_generate_unique_user_id_retry_budget_is_large(monkeypatch):
    # The retry budget must be generous (far more than the old 8) so transient
    # collisions don't surface the friendly-failure path prematurely. Count how
    # many candidates the helper is willing to try before giving up.
    attempts = 0

    async def _get(candidate: str):
        nonlocal attempts
        attempts += 1
        return {"user_id": candidate}  # every candidate collides

    monkeypatch.setattr(bot3.db, "get_user_by_user_id", _get)

    with pytest.raises(RuntimeError):
        await bot3._generate_unique_user_id()

    assert attempts == bot3._USER_ID_ATTEMPTS
    assert attempts >= 40  # comfortably beyond the old fixed 8 retries


# ─── concurrent pre-create of the SAME brand-new target: the race loser ───────
# Two admins can promote the SAME never-joined Telegram user at the same time.
# Both pass the "no existing row" check (their reads happen before either
# commits) and both take the pre-create branch. The tg_uid primary key (and
# user_id unique index) lets exactly ONE INSERT win; the loser's INSERT raises
# a ``UniqueViolation``, which rolls back its WHOLE transaction — including the
# ``role_events`` audit insert that would otherwise follow. This pins the
# graceful recovery: the losing actor is told the account already exists and to
# retry (rather than a confusing generic failure or an unhandled crash), and NO
# duplicate ``users`` row and NO orphan ``role_events`` audit row are written.
async def test_concurrent_precreate_loser_is_told_to_retry_with_no_writes(monkeypatch):
    import psycopg

    # The actor's initial read sees no row (race not yet lost), so apply_promotion
    # enters the pre-create branch — but by the time its INSERT runs, the other
    # admin has committed the same tg_uid, so the primary key rejects it.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
    })
    db.insert_users_error = psycopg.errors.UniqueViolation(
        'duplicate key value violates unique constraint "users_pkey"'
    )
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.apply_promotion(object(), {"tg_uid": 10, "role": "owner"}, 777, "dev_admin")

    # The loser gets a graceful "already created, try again" message — not the
    # generic "Failed to create the user" and not an unhandled crash.
    last = sender.texts[-1]
    assert "Run /promote again" in last
    assert "another admin" in last

    # The duplicate INSERT was attempted (the race is real), but it raised before
    # the audit insert, so no role_events orphan and no in-memory row survived.
    assert any(s.lstrip().startswith("INSERT INTO users") for s, _ in db.tx_calls)
    assert not any("INSERT INTO role_events" in s for s, _ in db.tx_calls)
    assert 777 not in db.users
    assert db.role_events == []


async def test_concurrent_precreate_winner_succeeds_normally(monkeypatch):
    # The companion to the loser test: with no race (insert_users_error unset),
    # the SAME pre-create path runs to completion — row inserted, audit written —
    # confirming the duplicate-key recovery doesn't disturb the happy path.
    db = FakeDb({
        10: {"tg_uid": 10, "user_id": "U-10", "name": "Ann", "role": "owner",
             "access_expires_at": _future()},
    })
    sender = FakeSender()
    _install(monkeypatch, db, sender)

    await bot3.apply_promotion(object(), {"tg_uid": 10, "role": "owner"}, 777, "dev_admin")

    # The winner's row is materialised and exactly one audit row is written.
    assert db.users[777]["role"] == "dev_admin"
    assert len(db.role_events) == 1
    # No "already created" retry message on the winning path.
    assert not any("Run /promote again" in t for t in sender.texts)
