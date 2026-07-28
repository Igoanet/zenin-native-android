"""Tests for the bot3 owner-cap promotion guard.

Promoting a user to ``owner`` is capped at ``MAX_OWNERS``. Because the decision
depends on a live ``COUNT(*) WHERE role = 'owner'``, a naive count-then-write is
racy: two concurrent promotions can both read ``count < cap`` and both commit,
silently exceeding the cap. Production defends against this with a
transaction-scoped Postgres advisory lock (``pg_advisory_xact_lock``) taken
*before* the count, so the count-then-write window is serialized per resource.

None of that is observable without a live bot + Postgres, so these tests drive
the real handlers (``apply_promotion`` / ``_apply_role_change`` for the promote
path, ``handle_key_submission`` for the key-redeem path) with an in-memory fake
cursor that records the exact SQL — including the advisory-lock acquisition —
in execution order. That lets us assert:

  * a promotion under the cap actually writes the role change + audit row,
  * a promotion at the cap is refused with a clear message to the requester and
    writes nothing,
  * the advisory lock is acquired *inside* the transaction and *before* the
    count + write (the invariant that closes the race), and only on the
    owner path.

The fake leaves ``db.acquire_owner_cap_lock`` as the real implementation so the
``SELECT pg_advisory_xact_lock(...)`` it issues flows through the fake cursor and
shows up in the recorded order exactly as production would order it.
"""
from __future__ import annotations

import contextlib

import bot3
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

    def texts_to(self, chat_id: int) -> list[str]:
        return [t for cid, t in self.calls if cid == chat_id]


class FakeCursor:
    """In-transaction cursor that records every ``execute`` in order and answers
    ``fetchone`` from the SQL it was last handed, honouring the same shapes the
    production queries expect (the locked role read, the owner count, the
    key-redeem ``RETURNING id``)."""

    def __init__(
        self,
        *,
        current_role: str | None = None,
        owner_count: int = 0,
        key_redeem_ok: bool = True,
    ) -> None:
        self.ops: list[tuple[str, tuple]] = []
        self._last_sql = ""
        self.current_role = current_role
        self.owner_count = owner_count
        self.key_redeem_ok = key_redeem_ok

    async def execute(self, sql: str, params: tuple = ()):
        self._last_sql = sql
        self.ops.append((sql, params))

    async def fetchone(self):
        sql = self._last_sql
        if "pg_advisory_xact_lock" in sql:
            return {"pg_advisory_xact_lock": ""}
        if "SELECT role FROM users" in sql:
            return {"role": self.current_role} if self.current_role is not None else None
        if "COUNT(*)" in sql and "owner" in sql:
            return {"n": self.owner_count}
        if "RETURNING id" in sql:  # access_keys redeem
            return {"id": "key-1"} if self.key_redeem_ok else None
        return None

    # ── helpers for assertions ──
    def index_of(self, needle: str) -> int:
        for i, (sql, _) in enumerate(self.ops):
            if needle in sql:
                return i
        return -1

    def has(self, needle: str) -> bool:
        return self.index_of(needle) != -1


def _patch_transaction(monkeypatch, cur: FakeCursor) -> None:
    """Route ``db.transaction()`` to our recording cursor while leaving
    ``db.acquire_owner_cap_lock`` as the real implementation (so the advisory
    lock is genuinely issued against the cursor)."""

    def fake_transaction():
        @contextlib.asynccontextmanager
        async def _cm():
            yield cur

        return _cm()

    monkeypatch.setattr(bot3.db, "transaction", fake_transaction)


def _management_actor() -> dict:
    return {"tg_uid": 99, "role": "management"}


# ─── promotion succeeds while under the cap and writes the role change ─────────
async def test_promotion_under_cap_succeeds_and_writes_role_change(monkeypatch):
    cur = FakeCursor(current_role="user", owner_count=MAX_OWNERS - 1)
    sender = FakeSender()
    _patch_transaction(monkeypatch, cur)
    monkeypatch.setattr(bot3, "send", sender.send)
    existing = {"tg_uid": 200, "user_id": "U-200", "name": "Dana",
                "tg_username": "dana", "role": "user"}

    async def _get(uid):
        return dict(existing) if uid == 200 else None

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get)

    await bot3.apply_promotion(object(), _management_actor(), 200, "owner")

    # The role change and its audit row are both written inside the transaction.
    assert cur.has("UPDATE users SET role")
    role_event = next(p for sql, p in cur.ops if "INSERT INTO role_events" in sql)
    assert "owner" in role_event  # new_role recorded as owner
    assert "user" in role_event   # prev_role recorded as user

    # The requester is told it worked.
    assert any("now" in t and "Owner" in t for t in sender.texts_to(99))


# ─── promotion is refused once the cap is reached, with a clear message ────────
async def test_promotion_at_cap_is_refused_with_clear_message_and_no_write(monkeypatch):
    cur = FakeCursor(current_role="user", owner_count=MAX_OWNERS)
    sender = FakeSender()
    _patch_transaction(monkeypatch, cur)
    monkeypatch.setattr(bot3, "send", sender.send)
    existing = {"tg_uid": 200, "user_id": "U-200", "name": "Dana",
                "tg_username": "dana", "role": "user"}

    async def _get(uid):
        return dict(existing) if uid == 200 else None

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get)

    await bot3.apply_promotion(object(), _management_actor(), 200, "owner")

    # The cap check fired before any write: no role change, no audit row.
    assert not cur.has("UPDATE users SET role")
    assert not cur.has("INSERT INTO role_events")
    # The lock + count still happened (that is how the cap was detected).
    assert cur.has("pg_advisory_xact_lock")
    assert cur.has("COUNT(*)")

    # The requester gets a clear, actionable refusal naming the cap.
    refusal = sender.texts_to(99)[-1]
    assert "Owner cap reached" in refusal
    assert str(MAX_OWNERS) in refusal


# ─── the advisory lock is taken inside the tx, before the count + write ────────
async def test_advisory_lock_acquired_before_count_and_write(monkeypatch):
    cur = FakeCursor(current_role="user", owner_count=MAX_OWNERS - 1)
    _patch_transaction(monkeypatch, cur)

    result = await bot3._apply_role_change(
        200, "U-200", "Dana", "dana", "owner", _management_actor(),
    )
    assert result == "ok"

    lock_at = cur.index_of("pg_advisory_xact_lock")
    count_at = cur.index_of("COUNT(*)")
    update_at = cur.index_of("UPDATE users SET role")
    insert_at = cur.index_of("INSERT INTO role_events")

    # All four steps happened in the same transaction.
    assert lock_at != -1 and count_at != -1 and update_at != -1 and insert_at != -1
    # The lock is held before the count is read (closing the count-then-write
    # race) and the count is read before either write.
    assert lock_at < count_at < update_at
    assert lock_at < count_at < insert_at


async def test_non_owner_promotion_takes_no_owner_lock(monkeypatch):
    # Promoting to a non-owner role must not touch the owner-cap lock — locking
    # unconditionally would needlessly serialize unrelated promotions.
    cur = FakeCursor(current_role="user", owner_count=0)
    _patch_transaction(monkeypatch, cur)
    actor = {"tg_uid": 99, "role": "owner"}  # an owner can promote to dev_admin

    result = await bot3._apply_role_change(
        200, "U-200", "Dana", "dana", "dev_admin", actor,
    )
    assert result == "ok"
    assert not cur.has("pg_advisory_xact_lock")
    assert not cur.has("COUNT(*)")
    assert cur.has("UPDATE users SET role")


# ─── the same guard protects the key-redeem owner path ────────────────────────
def _arm_key_redeem(monkeypatch, cur: FakeCursor, sender: FakeSender) -> None:
    _patch_transaction(monkeypatch, cur)
    monkeypatch.setattr(bot3, "send", sender.send)

    async def _no_existing(uid):
        return None

    async def _key(code):
        return {"id": "key-1", "code": code, "role": "owner", "revoked": False,
                "redeemed_by_tg_uid": None, "expires_at": None,
                "created_by_tg_uid": 99, "created_by_role": "management"}

    async def _members(uid):
        return {"missing": [], "all": [1]}

    async def _uid():
        return "U-300"

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _no_existing)
    monkeypatch.setattr(bot3.db, "get_access_key_by_code", _key)
    monkeypatch.setattr(bot3, "check_channel_membership", _members)
    monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)
    bot3._dialogs.clear()


class _Msg:
    def __init__(self, chat_id: int) -> None:
        self.chat = type("C", (), {"id": chat_id})()


async def test_key_redeem_owner_under_cap_locks_then_counts_then_inserts(monkeypatch):
    cur = FakeCursor(owner_count=MAX_OWNERS - 1, key_redeem_ok=True)
    sender = FakeSender()
    _arm_key_redeem(monkeypatch, cur, sender)
    state = {"name": "Eve", "tg_username": "eve"}

    await bot3.handle_key_submission(object(), _Msg(300), "ZN-ABCD-2345", state)

    redeem_at = cur.index_of("UPDATE access_keys SET redeemed_by_tg_uid")
    lock_at = cur.index_of("pg_advisory_xact_lock")
    count_at = cur.index_of("COUNT(*)")
    insert_user_at = cur.index_of("INSERT INTO users")
    # Lock is taken after the single-use redeem claim but before the cap count,
    # and the owner row is only inserted after the count passes.
    assert redeem_at < lock_at < count_at < insert_user_at
    assert cur.has("INSERT INTO role_events")
    assert any("Access granted" in t for t in sender.texts_to(300))


async def test_key_redeem_owner_at_cap_is_refused_with_clear_message(monkeypatch):
    cur = FakeCursor(owner_count=MAX_OWNERS, key_redeem_ok=True)
    sender = FakeSender()
    _arm_key_redeem(monkeypatch, cur, sender)
    state = {"name": "Eve", "tg_username": "eve"}

    await bot3.handle_key_submission(object(), _Msg(300), "ZN-ABCD-2345", state)

    # No account row is created once the cap is hit.
    assert not cur.has("INSERT INTO users")
    assert not cur.has("INSERT INTO role_events")
    # The redeemer is told the owner slot is full, naming the cap.
    refusal = sender.texts_to(300)[-1]
    assert "owner slot is full" in refusal
    assert str(MAX_OWNERS) in refusal


# ─── demoting an owner frees a slot ──────────────────────────────────────────
# Promotion *into* owner is capped (tests above). The opposite direction —
# demoting an owner back to ``user`` — is what frees a capped slot, and it runs
# through the role-picker confirm callback (``pmxc:<tg_uid>``) in ``_on_callback``.
# A regression in the demote authorization (``can_demote``) would let the wrong
# actor demote, or refuse a legitimate one and leave the cap permanently full; a
# regression in the audit write would silently drop the ``role_events`` trail
# the dashboard relies on. These drive the real handler with the same recording
# fake cursor used above so we can assert the exact in-transaction SQL.
class FakeCallback:
    """Stand-in for a Pyrogram CallbackQuery. Records every ``answer`` attempt."""

    def __init__(self, uid: int, data: str) -> None:
        self.from_user = type("U", (), {"id": uid})()
        self.data = data
        self.message = _Msg(uid)
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False):
        self.answers.append((text, show_alert))


def _arm_demote(monkeypatch, cur: FakeCursor, sender: FakeSender, users: dict) -> None:
    """Route the demote handler at our recording cursor and serve ``users`` rows
    keyed by tg_uid for both the actor gate and the target lookup."""
    _patch_transaction(monkeypatch, cur)
    monkeypatch.setattr(bot3, "send", sender.send)

    async def _get(uid):
        u = users.get(uid)
        return dict(u) if u else None

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get)


def _owner_target(tg_uid: int = 200) -> dict:
    return {"tg_uid": tg_uid, "user_id": "U-200", "name": "Dana",
            "tg_username": "dana", "role": "owner"}


# ─── an authorized actor can demote an owner, writing change + audit row ───────
async def test_authorized_demote_of_owner_writes_role_change_and_audit(monkeypatch):
    cur = FakeCursor(current_role="owner")
    sender = FakeSender()
    target = _owner_target(200)
    # Management is the only role allowed to demote an owner.
    _arm_demote(monkeypatch, cur, sender, {200: target})

    cq = FakeCallback(bot3.HARDCODED_MANAGEMENT_ID, "pmxc:200")
    await bot3._on_callback(object(), cq)

    # The role is dropped back to user inside the transaction.
    assert cur.has("UPDATE users SET role = 'user'")
    # …and the demotion is recorded in the append-only audit feed.
    ev_sql, ev_params = next((sql, p) for sql, p in cur.ops if "INSERT INTO role_events" in sql)
    assert "'user'" in ev_sql      # new_role recorded as user
    assert "owner" in ev_params    # prev_role recorded as owner
    # The actor is told it worked.
    assert any("is now <b>User</b>" in t for t in sender.texts_to(bot3.HARDCODED_MANAGEMENT_ID))


# ─── an unauthorized actor's demote is refused and writes nothing ─────────────
async def test_unauthorized_demote_is_refused_with_clear_message_and_no_write(monkeypatch):
    cur = FakeCursor(current_role="owner")
    sender = FakeSender()
    actor = {"tg_uid": 99, "user_id": "U-99", "name": "Olwen",
             "tg_username": "olwen", "role": "owner"}
    target = _owner_target(200)
    # An owner cannot demote another owner (can_demote('owner','owner') is False).
    _arm_demote(monkeypatch, cur, sender, {99: actor, 200: target})

    cq = FakeCallback(99, "pmxc:200")
    await bot3._on_callback(object(), cq)

    # Nothing is written: no role change and no audit row.
    assert not cur.has("UPDATE users SET role")
    assert not cur.has("INSERT INTO role_events")
    # The actor gets a clear, alerting refusal.
    assert cq.answers
    text, alert = cq.answers[-1]
    assert "Not allowed" in text and alert is True


# ─── after a demote, a subsequent owner promotion succeeds (slot freed) ───────
class _SlotState:
    def __init__(self, owners: int) -> None:
        self.owners = owners


class CountingCursor(FakeCursor):
    """A recording cursor whose live owner count is backed by a shared box, so a
    demote in one transaction is visible to a promotion in the next — exactly how
    freeing a slot must behave against the real ``COUNT(*)``."""

    def __init__(self, slot: _SlotState, *, current_role: str | None = None) -> None:
        super().__init__(current_role=current_role, owner_count=0)
        self.slot = slot

    async def execute(self, sql: str, params: tuple = ()):
        await super().execute(sql, params)
        if "UPDATE users SET role = 'user'" in sql and self.current_role == "owner":
            self.slot.owners -= 1  # the demote frees a slot

    async def fetchone(self):
        if "COUNT(*)" in self._last_sql and "owner" in self._last_sql:
            return {"n": self.slot.owners}
        return await super().fetchone()


async def test_demote_frees_a_slot_so_next_owner_promotion_succeeds(monkeypatch):
    slot = _SlotState(MAX_OWNERS)  # start at the cap — no slot available
    sender = FakeSender()
    monkeypatch.setattr(bot3, "send", sender.send)

    # 1) At the cap, promoting a fresh user to owner is refused.
    promote_cur = CountingCursor(slot, current_role="user")
    _patch_transaction(monkeypatch, promote_cur)

    async def _get_dana(uid):
        return ({"tg_uid": 200, "user_id": "U-200", "name": "Dana",
                 "tg_username": "dana", "role": "user"} if uid == 200 else None)

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get_dana)
    await bot3.apply_promotion(object(), _management_actor(), 200, "owner")
    assert not promote_cur.has("UPDATE users SET role")
    assert any("Owner cap reached" in t for t in sender.texts_to(99))

    # 2) Management demotes an existing owner, freeing exactly one slot.
    demote_cur = CountingCursor(slot, current_role="owner")
    _patch_transaction(monkeypatch, demote_cur)

    async def _get_owner(uid):
        return (_owner_target(300) if uid == 300 else None)

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get_owner)
    cq = FakeCallback(bot3.HARDCODED_MANAGEMENT_ID, "pmxc:300")
    await bot3._on_callback(object(), cq)
    assert demote_cur.has("UPDATE users SET role = 'user'")
    assert slot.owners == MAX_OWNERS - 1

    # 3) With the slot genuinely freed, the same promotion now succeeds.
    promote_cur2 = CountingCursor(slot, current_role="user")
    _patch_transaction(monkeypatch, promote_cur2)
    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _get_dana)
    await bot3.apply_promotion(object(), _management_actor(), 200, "owner")
    assert promote_cur2.has("UPDATE users SET role")
    assert promote_cur2.has("INSERT INTO role_events")


# ─── key *creation* (cmd_new_key) precheck against the owner cap ──────────────
# Redeeming an owner key is guarded transactionally (tests above). But minting
# an owner key with ``/newkey`` does an earlier, non-transactional precheck:
# ``count_owners() >= MAX_OWNERS`` refuses the key before it is ever stored. That
# precheck is the first line of defense — if it regressed, admins could mint
# unlimited owner keys and push all enforcement onto the redeem-time guard. These
# drive the real ``cmd_new_key`` with a fake ``count_owners`` and a recording
# ``db.execute`` so we can assert the precheck refuses / allows and whether a row
# was actually inserted.
#
# The role gate ``can_create_key`` independently forbids minting owner keys for
# every role, so to exercise the *cap* precheck in isolation the owner-key tests
# stub it open. The non-owner test leaves the real gate in place.
class _RecordingExecute:
    """Stand-in for ``db.execute``. Records every SQL it was asked to run so the
    test can assert whether an ``INSERT INTO access_keys`` actually happened."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def __call__(self, sql: str, params: tuple = ()):
        self.calls.append((sql, params))
        return None

    def inserted_key(self) -> bool:
        return any("INSERT INTO access_keys" in sql for sql, _ in self.calls)


def _arm_new_key(monkeypatch, sender: FakeSender, exe: _RecordingExecute) -> None:
    monkeypatch.setattr(bot3, "send", sender.send)
    monkeypatch.setattr(bot3.db, "execute", exe)


async def test_new_owner_key_refused_at_cap_names_cap_and_writes_nothing(monkeypatch):
    sender = FakeSender()
    exe = _RecordingExecute()
    _arm_new_key(monkeypatch, sender, exe)
    # Owners are already at the cap.
    monkeypatch.setattr(bot3, "count_owners", lambda: _async_value(MAX_OWNERS))
    # Isolate the cap precheck from the independent role gate.
    monkeypatch.setattr(bot3, "can_create_key", lambda creator, target: True)

    creator = {"tg_uid": 99, "role": "management"}
    await bot3.cmd_new_key(object(), creator, "owner 7d")

    # No key row was written…
    assert not exe.inserted_key()
    # …and the creator is told the cap is full, naming the cap.
    refusal = sender.texts_to(99)[-1]
    assert "Owner cap reached" in refusal
    assert str(MAX_OWNERS) in refusal


async def test_new_owner_key_under_cap_succeeds_and_writes_key(monkeypatch):
    sender = FakeSender()
    exe = _RecordingExecute()
    _arm_new_key(monkeypatch, sender, exe)
    # One slot still free.
    monkeypatch.setattr(bot3, "count_owners", lambda: _async_value(MAX_OWNERS - 1))
    monkeypatch.setattr(bot3, "can_create_key", lambda creator, target: True)

    creator = {"tg_uid": 99, "role": "management"}
    await bot3.cmd_new_key(object(), creator, "owner 7d")

    # The owner key was actually minted…
    assert exe.inserted_key()
    insert_params = next(p for sql, p in exe.calls if "INSERT INTO access_keys" in sql)
    assert "owner" in insert_params  # the stored key carries the owner role
    # …and the creator is shown the created-key confirmation.
    assert any("Access key created" in t for t in sender.texts_to(99))


async def test_new_non_owner_key_never_consults_owner_count(monkeypatch):
    sender = FakeSender()
    exe = _RecordingExecute()
    _arm_new_key(monkeypatch, sender, exe)

    # If the non-owner path touches the owner count at all, fail loudly.
    def _boom():
        raise AssertionError("owner count must not be read for a non-owner key")

    monkeypatch.setattr(bot3, "count_owners", _boom)

    # A real role/gate combination: dev_admin minting a plain user key.
    creator = {"tg_uid": 99, "role": "dev_admin"}
    await bot3.cmd_new_key(object(), creator, "user 7d")

    # The key is created without ever reading the owner count.
    assert exe.inserted_key()
    insert_params = next(p for sql, p in exe.calls if "INSERT INTO access_keys" in sql)
    assert "user" in insert_params
    assert any("Access key created" in t for t in sender.texts_to(99))


async def _async_value(value):
    return value


# ─── owner keys can NEVER be created by anyone (the locked-in invariant) ───────
# This is the rule the owner-cap tests above quietly depend on: they stub
# ``can_create_key`` *open* to exercise the cap precheck in isolation, so a
# regression that re-allowed owner keys would slip through every other test
# unnoticed. Pin the gate shut here. Owners are appointed only via /promote
# (management -> owner), capped under an advisory lock — no access key may ever
# carry the owner role, regardless of who mints it.
#
# ``can_create_key`` (bot) and ``canCreateKey`` (web, keyAccess.test.ts) are
# kept in lockstep; the web side asserts the identical rule.
import pytest  # noqa: E402  (kept beside the tests that use it)

# Every role that exists in the system, used as a candidate key creator.
_ALL_ROLES = ("management", "owner", "dev_admin", "base_admin", "user")


@pytest.mark.parametrize("creator_role", _ALL_ROLES)
def test_owner_key_never_creatable_by_any_role(creator_role):
    assert bot3.can_create_key(creator_role, "owner") is False


async def test_management_newkey_says_it_creates_no_keys(monkeypatch):
    # Management mints no keys at all: the bare ``/newkey`` hint must say so and
    # must not advertise any creatable role to management.
    sender = FakeSender()
    monkeypatch.setattr(bot3, "send", sender.send)

    creator = {"tg_uid": 99, "role": "management"}
    await bot3.cmd_new_key(object(), creator, "")

    msg = sender.texts_to(99)[-1]
    assert "does not create access keys" in msg
    # The management hint advertises no creatable role and shows no usage line.
    assert "Usage:" not in msg
    assert "role:" not in msg
