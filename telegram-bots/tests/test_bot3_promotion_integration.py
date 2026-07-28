"""Real-Postgres integration test for the bot3 concurrent pre-create promotion.

The owner-cap and pre-create races are otherwise verified with in-memory fakes
(``test_bot3_owner_cap.py``, ``test_bot3_change_password.py``) that *simulate*
the duplicate-key rejection. The actual guarantee, though, rests on two pieces of
live Postgres behaviour that no fake can prove:

  * the ``users.tg_uid`` PRIMARY KEY rejecting a second concurrent INSERT of the
    SAME brand-new Telegram user, and
  * ``db.transaction()`` rolling the whole unit of work back when that rejection
    fires — so the ``role_events`` audit insert that runs *after* the users
    insert in the same transaction never lands as an orphan.

This module drives the *real* ``bot3.apply_promotion`` pre-create branch against
a *real* Postgres (the live ``DATABASE_URL`` with the actual schema). It does so
two ways:

  * ``test_concurrent_pre_create_one_winner_loser_rolls_back`` runs two genuine,
    parallel ``apply_promotion`` calls (each in its own OS thread, event loop and
    DB connection) for the same never-seen ``tg_uid``. A ``threading.Barrier``
    holds both past their "no existing row" pre-check so they truly collide on
    the INSERT. Exactly one ``users`` row and one ``role_events`` row must remain.

  * ``test_lost_race_insert_rolls_back_without_orphan_event`` deterministically
    pins the loser path: a winner row is committed first, the pre-check is forced
    to still report "no row" (the race window), and the real INSERT then hits the
    live PK. The whole transaction must roll back, leaving the audit feed
    untouched (no orphan ``role_events`` row).

  * ``test_concurrent_pre_create_single_event_loop_one_winner`` exercises the
    *exact production shape*: two ``apply_promotion`` coroutines racing the same
    never-seen ``tg_uid`` on a SINGLE asyncio event loop (one Pyrogram loop is
    how the live bot services both), driven with ``asyncio.gather``. The same
    one-winner / loser-rolls-back invariants must hold.

The concurrent-demotion inverse is also covered:

  * ``test_concurrent_demotion_of_same_owner_is_idempotent_threaded`` races
    two admins demoting the SAME owner to ``'user'`` at the same instant on
    genuinely parallel Postgres connections. The ``SELECT … FOR UPDATE`` row-lock
    in ``_demote_owner_direct`` serializes the two transactions: the winner
    updates the role and inserts exactly one audit row; when the loser acquires
    the lock the role is already ``'user'``, so it returns ``__noop__`` without
    inserting a second row. Asserts: final role ``'user'``, exactly one
    ``role_events`` row, one ``"ok"`` and one ``"__noop__"`` reply (no
    duplicate-success).

The within-row demotion ↔ re-promotion race is covered:

  * ``test_concurrent_demotion_and_repromote_same_target_threaded`` races one
    admin demoting SAME_TARGET (owner→user, via ``_demote_owner_direct``) while
    a second admin simultaneously re-promotes THAT SAME TARGET (user→owner, via
    ``apply_promotion``). Both transactions race on the ``SELECT … FOR UPDATE``
    lock on the same row — whichever transaction acquires the lock first
    determines the outcome. Two coherent orderings are possible:

      (a) Demotion wins the row-lock first: target goes owner→user, then the
          promotion's ``_apply_role_change`` re-acquires the lock, sees ``'user'``,
          acquires the advisory lock, counts owners (now MAX_OWNERS − 1), promotes
          back to ``'owner'``. Final role: ``'owner'``, two ``role_events`` rows.

      (b) Promotion wins the row-lock first: ``_apply_role_change`` sees
          ``role == target_role == 'owner'`` → ``__noop__``, releases the lock
          without writing. Demotion then acquires the lock, demotes to ``'user'``.
          Final role: ``'user'``, one ``role_events`` row.

    In both cases the final role is exactly one of ``{'owner', 'user'}`` (no
    torn write), exactly one ``role_events`` row per committed action, and the
    owner count never exceeds ``MAX_OWNERS``.

The owner-cap pre-create race — two admins each promoting a DIFFERENT never-seen
user straight to OWNER with one slot free — has no PRIMARY KEY to serialize it;
only the transaction-scoped advisory lock (``db.acquire_owner_cap_lock``) stands
between "one new owner" and "cap exceeded". It is covered two ways, mirroring the
duplicate-key pair above:

  * ``test_concurrent_pre_create_owner_cap_one_winner`` drives both promotions on
    a SINGLE asyncio event loop (the live bot's shape) via ``asyncio.gather``.

  * ``test_concurrent_pre_create_owner_cap_one_winner_threaded`` is the strictly-
    more-concurrent variant: each promotion runs in its own OS thread, event loop
    and DB connection, so the two count-then-INSERT units execute on genuinely
    parallel Postgres connections. A ``threading.Barrier`` holds both past their
    "no existing row" pre-check so they collide on the advisory lock for real,
    proving ``pg_advisory_xact_lock`` serializes across separate backends and not
    just across coroutines multiplexed onto one connection-per-call loop. Exactly
    one new owner must result, the other rejected with "Owner cap reached", and
    ``db.count_owners()`` must equal ``MAX_OWNERS`` — never overrun.

A single-loop concurrency test *seems* prone to hanging, and an earlier attempt
did hang — but the cause was the rendezvous primitive, not psycopg. The threaded
test gates both racers with a ``threading.Barrier``; reused verbatim on one
event loop that ``Barrier.wait()`` blocks the *only* OS thread synchronously, so
the second coroutine can never reach the barrier and the loop wedges forever.
psycopg's async connections do **not** deadlock here: when the loser's INSERT
blocks on the winner's uncommitted row lock, the ``await`` yields control back to
the loop, the winner commits, and the loser then cleanly takes the duplicate-key
rollback. The fix is simply an *async* rendezvous (``asyncio.Barrier``) that
yields instead of blocking the thread, so the single-loop path is now covered
directly rather than only by the strictly-more-concurrent threaded test.

When the resolved ``DATABASE_URL`` is the conftest's inert localhost placeholder
(no real DB wired up, as on a bare CI runner) the connection fails and the whole
module is skipped rather than reported as a failure.
"""
from __future__ import annotations

import asyncio
import random
import threading
import uuid

import psycopg
import pytest

import bot3
import db

# Sentinel Telegram uids for the brand-new target and the two racing admins.
# Real Telegram uids are positive, so large negative values cannot collide with a
# genuine account in a shared dev database. A per-process random run id offsets
# them so two CI workers sharing the same database don't collide on each other's
# sentinels. All cleanup is keyed strictly on these exact values.
_RUN = random.randint(1, 9_000_000)
_BASE = -(990_000_000 + _RUN * 10)
TARGET_TG_UID = _BASE - 1
ACTOR_A_TG_UID = _BASE - 2
ACTOR_B_TG_UID = _BASE - 3

# Two DISTINCT brand-new owner candidates for the owner-cap pre-create race, plus
# a pool of sentinel "seed owner" rows used to pad the owners table up to exactly
# one free slot below MAX_OWNERS. All keyed on sentinel uids so cleanup never
# touches real owners.
OWNER_TARGET_A_TG_UID = _BASE - 4
OWNER_TARGET_B_TG_UID = _BASE - 5

# Two DISTINCT users that ALREADY exist (non-owner) in the users table, promoted
# to owner simultaneously. Unlike the pre-create pair above, these have a row up
# front, so the promotion runs through _apply_role_change (the existing-row
# branch) rather than the INSERT branch — a second owner-cap entry point.
EXIST_TARGET_A_TG_UID = _BASE - 6
EXIST_TARGET_B_TG_UID = _BASE - 7

# Demote+promote race: all slots are full at race start. One admin demotes
# DEMOTE_OWNER_TG_UID (owner→user) while another simultaneously promotes
# PROMOTE_TARGET_TG_UID (user→owner). The demotion holds no advisory lock, so
# the two transactions overlap freely; the promotion must never push the count
# above MAX_OWNERS.
DEMOTE_OWNER_TG_UID = _BASE - 8
PROMOTE_TARGET_TG_UID = _BASE - 9

# Concurrent-demotion race: TWO admins try to demote the SAME owner to 'user'
# at the same instant. The FOR UPDATE row-lock in _demote_owner_direct
# serializes them; the loser must get __noop__ (already user), not write a
# second audit row.
CONC_DEMOTE_OWNER_TG_UID = _BASE - 10

# Within-row demotion ↔ re-promotion race: one admin demotes SAME_TARGET
# (owner→user) while another simultaneously re-promotes THAT SAME TARGET
# (user→owner). The FOR UPDATE lock on the single row serializes the two
# transactions; the final state must be exactly 'owner' or 'user' with
# exactly one role_events row per committed action.
SAME_TARGET_TG_UID = _BASE - 11

SEED_OWNER_TG_UIDS = tuple(_BASE - 100 - i for i in range(bot3.MAX_OWNERS))


def _run(coro):
    return asyncio.run(coro)


async def _db_reachable() -> bool:
    """True when the resolved DATABASE_URL points at a real, schema-bearing
    Postgres. False (→ skip) when it is the inert placeholder or the required
    tables are absent."""
    try:
        async with await psycopg.AsyncConnection.connect(db.DATABASE_URL) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT to_regclass('public.users'), "
                    "to_regclass('public.role_events')"
                )
                users_tbl, events_tbl = await cur.fetchone()
                return users_tbl is not None and events_tbl is not None
    except Exception:
        return False


async def _cleanup() -> None:
    """Remove any rows this test created (or a previous crashed run left), keyed
    strictly on the sentinel uid so no real data is ever touched."""
    await db.execute("DELETE FROM role_events WHERE target_tg_uid = %s", (TARGET_TG_UID,))
    await db.execute("DELETE FROM users WHERE tg_uid = %s", (TARGET_TG_UID,))


async def _count(sql: str, params: tuple) -> int:
    row = await db.fetchone(sql, params)
    return int(row["n"]) if row else 0


async def _users_for_target() -> int:
    return await _count(
        "SELECT COUNT(*)::int AS n FROM users WHERE tg_uid = %s", (TARGET_TG_UID,)
    )


async def _events_for_target() -> int:
    return await _count(
        "SELECT COUNT(*)::int AS n FROM role_events WHERE target_tg_uid = %s",
        (TARGET_TG_UID,),
    )


@pytest.fixture
def real_db():
    if not _run(_db_reachable()):
        pytest.skip("no real Postgres with the dashboard schema is available")
    _run(_cleanup())
    try:
        yield
    finally:
        _run(_cleanup())


class _Recorder:
    """Stand-in for ``bot3.send`` that records every (chat_id, text). ``list``
    append is atomic under the GIL, so it is safe to share across the two
    promotion threads."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def send(self, client, chat_id, text, rows=None):
        self.calls.append((chat_id, text))
        return object()

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.calls]


# ─── the race: two genuinely parallel pre-creates, one winner, no orphan ──────
def test_concurrent_pre_create_one_winner_loser_rolls_back(monkeypatch, real_db):
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)

    # Each promotion mints its own candidate user_id so the collision is forced
    # purely by the shared tg_uid PRIMARY KEY (not the user_id unique index) —
    # the exact guarantee the pre-create branch leans on.
    _ids = iter(f"itest_{uuid.uuid4().hex[:10]}_{i}" for i in range(2))
    _ids_lock = threading.Lock()

    async def _uid():
        with _ids_lock:
            return next(_ids)

    monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

    # Hold both promotions until EACH has finished its "is there an existing
    # row?" pre-check and seen None. Without this, one could fully commit before
    # the other even reads, and the loser would take the harmless "already
    # exists" branch instead of the duplicate-key rollback we want to force.
    barrier = threading.Barrier(2)
    real_get = db.get_user_by_tg_uid

    async def _gated_get(uid: int):
        result = await real_get(uid)
        if uid == TARGET_TG_UID:
            barrier.wait(timeout=15)  # cross-thread rendezvous
        return result

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

    def _promote(actor_uid: int) -> None:
        actor = {"tg_uid": actor_uid, "role": "owner"}  # owner may pre-create a user
        _run(bot3.apply_promotion(object(), actor, TARGET_TG_UID, "user"))

    t_a = threading.Thread(target=_promote, args=(ACTOR_A_TG_UID,))
    t_b = threading.Thread(target=_promote, args=(ACTOR_B_TG_UID,))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)
    assert not t_a.is_alive() and not t_b.is_alive(), "a promotion thread hung"

    # Exactly one users row exists for the contested uid …
    assert _run(_users_for_target()) == 1
    # … and exactly one audit row — the loser's role_events insert rolled back
    # in lockstep with its users insert, leaving no orphan.
    assert _run(_events_for_target()) == 1

    # The surviving user row is internally consistent with its audit row,
    # proving the winner committed atomically. Read it with raw SQL (not the
    # monkeypatched db.get_user_by_tg_uid, which is still gated on the barrier).
    winner = _run(db.fetchone(
        "SELECT user_id, role FROM users WHERE tg_uid = %s", (TARGET_TG_UID,)
    ))
    event = _run(db.fetchone(
        "SELECT target_user_id, new_role FROM role_events WHERE target_tg_uid = %s",
        (TARGET_TG_UID,),
    ))
    assert winner["user_id"] == event["target_user_id"]
    assert winner["role"] == "user" == event["new_role"]

    # Both admins were answered: one a success, the other the lost-race retry
    # prompt — the observable signal that the loser's transaction rolled back.
    assert any("Promoted" in t for t in recorder.texts)
    assert any("Run /promote again" in t for t in recorder.texts)


# ─── the same race on a SINGLE event loop (the real production shape) ─────────
# The threaded test above is strictly more concurrent (separate loops + threads),
# but the live bot services both promotions on ONE Pyrogram event loop. This test
# pins exactly that shape: two apply_promotion coroutines on a single loop via
# asyncio.gather. The rendezvous is an *async* asyncio.Barrier — not the threaded
# test's threading.Barrier, whose blocking wait() would freeze the only thread and
# wedge the loop (see the module docstring). psycopg's async connections yield on
# the loser's blocked INSERT, so the winner commits and the loser rolls back.
async def test_concurrent_pre_create_single_event_loop_one_winner(monkeypatch):
    if not await _db_reachable():
        pytest.skip("no real Postgres with the dashboard schema is available")
    await _cleanup()
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        # Each promotion mints its own candidate user_id so the collision is
        # forced purely by the shared tg_uid PRIMARY KEY. No lock is needed
        # around next(): on a single loop, coroutines never preempt mid-statement.
        _ids = iter(f"itest_{uuid.uuid4().hex[:10]}_{i}" for i in range(2))

        async def _uid():
            return next(_ids)

        monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

        # Hold both coroutines until EACH has finished its "is there an existing
        # row?" pre-check and seen None, so they truly collide on the INSERT
        # rather than one committing before the other reads. asyncio.Barrier
        # yields control back to the loop while waiting (unlike threading.Barrier).
        barrier = asyncio.Barrier(2)
        real_get = db.get_user_by_tg_uid

        async def _gated_get(uid: int):
            result = await real_get(uid)
            if uid == TARGET_TG_UID:
                await asyncio.wait_for(barrier.wait(), timeout=15)
            return result

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

        actor_a = {"tg_uid": ACTOR_A_TG_UID, "role": "owner"}
        actor_b = {"tg_uid": ACTOR_B_TG_UID, "role": "owner"}
        await asyncio.wait_for(
            asyncio.gather(
                bot3.apply_promotion(object(), actor_a, TARGET_TG_UID, "user"),
                bot3.apply_promotion(object(), actor_b, TARGET_TG_UID, "user"),
            ),
            timeout=30,
        )

        # Exactly one users row …
        assert await _users_for_target() == 1
        # … and exactly one audit row — the loser's role_events insert rolled
        # back in lockstep with its users insert, leaving no orphan.
        assert await _events_for_target() == 1

        # The surviving user row is internally consistent with its audit row,
        # proving the winner committed atomically. Read it with raw SQL (not the
        # monkeypatched db.get_user_by_tg_uid, which is still gated on the barrier).
        winner = await db.fetchone(
            "SELECT user_id, role FROM users WHERE tg_uid = %s", (TARGET_TG_UID,)
        )
        event = await db.fetchone(
            "SELECT target_user_id, new_role FROM role_events WHERE target_tg_uid = %s",
            (TARGET_TG_UID,),
        )
        assert winner["user_id"] == event["target_user_id"]
        assert winner["role"] == "user" == event["new_role"]

        # Both admins were answered: one a success, the other the lost-race retry
        # prompt — the observable signal that the loser's transaction rolled back.
        assert any("Promoted" in t for t in recorder.texts)
        assert any("Run /promote again" in t for t in recorder.texts)
    finally:
        await _cleanup()


# ─── the loser path, pinned deterministically ────────────────────────────────
# A winner row is committed first, then the pre-check is forced to still report
# "no existing row" (the exact race window apply_promotion guards). The real
# INSERT then hits the live tg_uid PK. We assert the whole transaction rolled
# back: the pre-existing winner is the ONLY users row, and — critically — no
# orphan role_events row was left behind from the loser's in-transaction audit
# insert. This needs no scheduling luck, so it pins the rollback guarantee.
async def test_lost_race_insert_rolls_back_without_orphan_event(monkeypatch):
    if not await _db_reachable():
        pytest.skip("no real Postgres with the dashboard schema is available")
    await _cleanup()
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        # A concurrent admin already committed this brand-new user (the winner).
        winner_user_id = f"itest_{uuid.uuid4().hex[:10]}"
        await db.execute(
            "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
            "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, %s, true)",
            (TARGET_TG_UID, winner_user_id, "h", "s",
             bot3.placeholder_name(TARGET_TG_UID), "user"),
        )

        # Force apply_promotion down the pre-create branch even though the row
        # now exists — i.e. its pre-check ran inside the race window, before the
        # winner committed.
        async def _sees_nothing(uid: int):
            return None

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _sees_nothing)

        loser_user_id = f"itest_{uuid.uuid4().hex[:10]}"

        async def _uid():
            return loser_user_id

        monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

        actor = {"tg_uid": ACTOR_A_TG_UID, "role": "owner"}
        await bot3.apply_promotion(object(), actor, TARGET_TG_UID, "user")

        # Still exactly one users row: the committed winner, untouched. Read it
        # with raw SQL — db.get_user_by_tg_uid is monkeypatched to feign "no row".
        assert await _users_for_target() == 1
        row = await db.fetchone(
            "SELECT user_id FROM users WHERE tg_uid = %s", (TARGET_TG_UID,)
        )
        assert row["user_id"] == winner_user_id

        # The loser's transaction rolled back whole: no audit row leaked from the
        # role_events insert that follows the users insert in the same tx.
        assert await _events_for_target() == 0

        # The losing admin is told the account already exists and to retry.
        assert any("Run /promote again" in t for t in recorder.texts)
    finally:
        await _cleanup()


# ─── owner-cap helpers ───────────────────────────────────────────────────────
async def _cleanup_owner_cap() -> None:
    """Remove every row this owner-cap test created (or a crashed run left): the
    two contested owner candidates and all sentinel seed-owner rows. Keyed
    strictly on the sentinel uids so no real owner is ever deleted."""
    for uid in (OWNER_TARGET_A_TG_UID, OWNER_TARGET_B_TG_UID, *SEED_OWNER_TG_UIDS):
        await db.execute("DELETE FROM role_events WHERE target_tg_uid = %s", (uid,))
        await db.execute("DELETE FROM users WHERE tg_uid = %s", (uid,))


async def _seed_one_free_owner_slot() -> bool:
    """Pad the owners table with sentinel owner rows until exactly one slot
    remains below MAX_OWNERS, then return True. Return False (→ skip) when real
    owners already occupy that many slots, since we cannot manufacture the
    one-free-slot state without deleting real data."""
    base = await db.count_owners()
    need = (bot3.MAX_OWNERS - 1) - base
    if need < 0:
        return False
    for i in range(need):
        uid = SEED_OWNER_TG_UIDS[i]
        await db.execute(
            "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
            "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'owner', true)",
            (uid, f"seedowner_{uuid.uuid4().hex[:10]}", "h", "s",
             bot3.placeholder_name(uid)),
        )
    return True


async def _owner_role_for(tg_uid: int) -> str | None:
    row = await db.fetchone(
        "SELECT role FROM users WHERE tg_uid = %s", (tg_uid,)
    )
    return row["role"] if row else None


# ─── existing-user owner-cap helpers ─────────────────────────────────────────
async def _cleanup_existing_owner_cap() -> None:
    """Remove every row the existing-user owner-cap test created (or a crashed
    run left): the two pre-seeded non-owner targets and all sentinel seed-owner
    rows. Keyed strictly on the sentinel uids so no real owner is ever deleted."""
    for uid in (EXIST_TARGET_A_TG_UID, EXIST_TARGET_B_TG_UID, *SEED_OWNER_TG_UIDS):
        await db.execute("DELETE FROM role_events WHERE target_tg_uid = %s", (uid,))
        await db.execute("DELETE FROM users WHERE tg_uid = %s", (uid,))


async def _seed_existing_nonowners() -> None:
    """Insert the two DISTINCT existing non-owner ('user') rows that the test
    will both promote to owner at the same instant. Because they already exist,
    apply_promotion drives them through the _apply_role_change owner-cap branch."""
    for uid in (EXIST_TARGET_A_TG_UID, EXIST_TARGET_B_TG_UID):
        await db.execute(
            "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
            "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'user', true)",
            (uid, f"existuser_{uuid.uuid4().hex[:10]}", "h", "s",
             bot3.placeholder_name(uid)),
        )


# ─── the owner-cap race: two DISTINCT brand-new owners, one slot left ─────────
# Two admins each promote a DIFFERENT never-seen Telegram user straight to OWNER
# at the same moment, with exactly one owner slot free under MAX_OWNERS. Because
# the targets are distinct, the tg_uid PRIMARY KEY does NOT serialize them — the
# only thing standing between "one new owner" and "cap exceeded" is the
# transaction-scoped advisory lock (db.acquire_owner_cap_lock) wrapping the
# count-then-INSERT in the pre-create branch. Both coroutines run on a SINGLE
# asyncio event loop (the live bot's shape), gated past their "no existing row"
# pre-check by an async barrier so they truly contend on the lock. Exactly one
# must become owner; the other must be cleanly rejected with "Owner cap reached",
# and the global owner count must never exceed MAX_OWNERS.
async def test_concurrent_pre_create_owner_cap_one_winner(monkeypatch):
    if not await _db_reachable():
        pytest.skip("no real Postgres with the dashboard schema is available")
    await _cleanup_owner_cap()
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        if not await _seed_one_free_owner_slot():
            pytest.skip(
                "real owners already fill MAX_OWNERS-1 slots; cannot create the "
                "one-free-slot state without disturbing real data"
            )

        # Each promotion mints its own candidate user_id; the targets already
        # differ by tg_uid, so no unique index serializes them — the advisory
        # lock is the sole guard under test.
        _ids = iter(f"itest_{uuid.uuid4().hex[:10]}_{i}" for i in range(2))

        async def _uid():
            return next(_ids)

        monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

        # Hold both coroutines until EACH has cleared its "is there an existing
        # row?" pre-check, so they enter their transactions together and truly
        # race on the owner-cap advisory lock rather than one finishing first.
        barrier = asyncio.Barrier(2)
        real_get = db.get_user_by_tg_uid

        async def _gated_get(uid: int):
            result = await real_get(uid)
            if uid in (OWNER_TARGET_A_TG_UID, OWNER_TARGET_B_TG_UID):
                await asyncio.wait_for(barrier.wait(), timeout=15)
            return result

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

        actor_a = {"tg_uid": ACTOR_A_TG_UID, "role": "management"}
        actor_b = {"tg_uid": ACTOR_B_TG_UID, "role": "management"}
        await asyncio.wait_for(
            asyncio.gather(
                bot3.apply_promotion(object(), actor_a, OWNER_TARGET_A_TG_UID, "owner"),
                bot3.apply_promotion(object(), actor_b, OWNER_TARGET_B_TG_UID, "owner"),
            ),
            timeout=30,
        )

        # Exactly one of the two candidates became owner; the loser was rejected
        # by the cap check BEFORE its INSERT, so it has no users row at all.
        role_a = await _owner_role_for(OWNER_TARGET_A_TG_UID)
        role_b = await _owner_role_for(OWNER_TARGET_B_TG_UID)
        roles = [r for r in (role_a, role_b) if r is not None]
        assert roles == ["owner"], (
            f"expected exactly one new owner row, got A={role_a!r} B={role_b!r}"
        )

        # The winner's audit row landed; the loser's never did (its whole
        # transaction rolled back before reaching the role_events insert).
        winner_uid = OWNER_TARGET_A_TG_UID if role_a == "owner" else OWNER_TARGET_B_TG_UID
        loser_uid = OWNER_TARGET_B_TG_UID if role_a == "owner" else OWNER_TARGET_A_TG_UID
        win_events = await _count(
            "SELECT COUNT(*)::int AS n FROM role_events "
            "WHERE target_tg_uid = %s AND new_role = 'owner'",
            (winner_uid,),
        )
        lose_events = await _count(
            "SELECT COUNT(*)::int AS n FROM role_events WHERE target_tg_uid = %s",
            (loser_uid,),
        )
        assert win_events == 1
        assert lose_events == 0

        # The cap held exactly: the freed slot is now filled and not overrun.
        assert await db.count_owners() == bot3.MAX_OWNERS

        # Both admins were answered — one a success, the other the cap rejection.
        assert any("Promoted" in t for t in recorder.texts)
        assert any("Owner cap reached" in t for t in recorder.texts)
    finally:
        await _cleanup_owner_cap()


# ─── the owner-cap race on TRULY parallel connections (threads) ──────────────
# The single-loop test above proves the advisory lock serializes two coroutines
# multiplexed onto one event loop. But db opens a fresh connection per call, so
# on one loop the two promotions still run on two SEPARATE Postgres backends that
# the loop interleaves cooperatively. This threaded variant is strictly more
# concurrent: each promotion runs in its OWN OS thread, event loop and DB
# connection, so the count-then-INSERT units execute on genuinely parallel
# backends with no cooperative scheduler between them. A threading.Barrier holds
# both past their "no existing row" pre-check so they truly collide on the
# transaction-scoped advisory lock — proving pg_advisory_xact_lock serializes
# across distinct backends, not merely across awaited coroutines. With exactly
# one owner slot free, one candidate must become owner, the other be rejected
# with "Owner cap reached", and db.count_owners() must never exceed MAX_OWNERS.
def test_concurrent_pre_create_owner_cap_one_winner_threaded(monkeypatch):
    if not _run(_db_reachable()):
        pytest.skip("no real Postgres with the dashboard schema is available")
    _run(_cleanup_owner_cap())
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        if not _run(_seed_one_free_owner_slot()):
            pytest.skip(
                "real owners already fill MAX_OWNERS-1 slots; cannot create the "
                "one-free-slot state without disturbing real data"
            )

        # Each promotion mints its own candidate user_id; the targets already
        # differ by tg_uid, so no unique index serializes them — the advisory
        # lock is the sole guard under test. next() on the shared iterator is
        # guarded by a lock because the two promotions run on separate threads.
        _ids = iter(f"itest_{uuid.uuid4().hex[:10]}_{i}" for i in range(2))
        _ids_lock = threading.Lock()

        async def _uid():
            with _ids_lock:
                return next(_ids)

        monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

        # Hold both promotions until EACH has cleared its "is there an existing
        # row?" pre-check, so they enter their transactions together and truly
        # race on the owner-cap advisory lock. A threading.Barrier is correct
        # here (unlike the single-loop test): each promotion owns its own thread,
        # so the blocking wait() parks that thread without wedging the other's
        # loop. No DB lock is held while waiting, so the barrier cannot deadlock
        # against the advisory lock.
        barrier = threading.Barrier(2)
        real_get = db.get_user_by_tg_uid

        async def _gated_get(uid: int):
            result = await real_get(uid)
            if uid in (OWNER_TARGET_A_TG_UID, OWNER_TARGET_B_TG_UID):
                barrier.wait(timeout=15)  # cross-thread rendezvous
            return result

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

        def _promote_owner(actor_uid: int, target_uid: int) -> None:
            actor = {"tg_uid": actor_uid, "role": "management"}
            _run(bot3.apply_promotion(object(), actor, target_uid, "owner"))

        t_a = threading.Thread(
            target=_promote_owner, args=(ACTOR_A_TG_UID, OWNER_TARGET_A_TG_UID)
        )
        t_b = threading.Thread(
            target=_promote_owner, args=(ACTOR_B_TG_UID, OWNER_TARGET_B_TG_UID)
        )
        t_a.start()
        t_b.start()
        t_a.join(timeout=30)
        t_b.join(timeout=30)
        assert not t_a.is_alive() and not t_b.is_alive(), "a promotion thread hung"

        # Exactly one of the two candidates became owner; the loser was rejected
        # by the cap check BEFORE its INSERT, so it has no users row at all.
        role_a = _run(_owner_role_for(OWNER_TARGET_A_TG_UID))
        role_b = _run(_owner_role_for(OWNER_TARGET_B_TG_UID))
        roles = [r for r in (role_a, role_b) if r is not None]
        assert roles == ["owner"], (
            f"expected exactly one new owner row, got A={role_a!r} B={role_b!r}"
        )

        # The winner's audit row landed; the loser's never did (its whole
        # transaction rolled back before reaching the role_events insert).
        winner_uid = OWNER_TARGET_A_TG_UID if role_a == "owner" else OWNER_TARGET_B_TG_UID
        loser_uid = OWNER_TARGET_B_TG_UID if role_a == "owner" else OWNER_TARGET_A_TG_UID
        win_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events "
            "WHERE target_tg_uid = %s AND new_role = 'owner'",
            (winner_uid,),
        ))
        lose_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events WHERE target_tg_uid = %s",
            (loser_uid,),
        ))
        assert win_events == 1
        assert lose_events == 0

        # The cap held exactly: the freed slot is now filled and not overrun.
        assert _run(db.count_owners()) == bot3.MAX_OWNERS

        # Both admins were answered — one a success, the other the cap rejection.
        assert any("Promoted" in t for t in recorder.texts)
        assert any("Owner cap reached" in t for t in recorder.texts)
    finally:
        _run(_cleanup_owner_cap())


# ─── the SECOND owner-cap entry point: two EXISTING users, one slot left ──────
# The threaded test above proves the advisory lock holds for the pre-create
# branch (two never-seen users INSERTed straight to owner). But there is a second
# owner-cap entry point that the pre-create test does NOT exercise: promoting two
# people who ALREADY have a users row to owner at the same instant. That path runs
# through _apply_role_change, which takes the SAME db.acquire_owner_cap_lock
# advisory lock before its count-then-UPDATE. The two existing rows have distinct
# tg_uids, so the FOR UPDATE row locks target different rows and do NOT serialize
# them — exactly like the pre-create case, only the transaction-scoped advisory
# lock stands between "one new owner" and "cap exceeded". This is the strictly-
# parallel proof for that branch: each promotion runs in its OWN OS thread, event
# loop and DB connection, so the two count-then-UPDATE units execute on genuinely
# parallel Postgres backends; a threading.Barrier holds both past their "existing
# row?" pre-check so they truly collide on the advisory lock inside
# _apply_role_change. With exactly one owner slot free, one existing user must
# become owner, the other be rejected with "Owner cap reached" (its row left at
# its original 'user' role), and db.count_owners() must never exceed MAX_OWNERS.
def test_concurrent_promote_existing_owner_cap_one_winner_threaded(monkeypatch):
    if not _run(_db_reachable()):
        pytest.skip("no real Postgres with the dashboard schema is available")
    _run(_cleanup_existing_owner_cap())
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        if not _run(_seed_one_free_owner_slot()):
            pytest.skip(
                "real owners already fill MAX_OWNERS-1 slots; cannot create the "
                "one-free-slot state without disturbing real data"
            )
        # Both targets must already exist so the promotion takes the
        # _apply_role_change branch, not the pre-create INSERT branch.
        _run(_seed_existing_nonowners())

        # Hold both promotions until EACH has cleared its "is there an existing
        # row?" pre-check, so they enter their transactions together and truly
        # race on the owner-cap advisory lock inside _apply_role_change. A
        # threading.Barrier is correct here: each promotion owns its own thread,
        # so the blocking wait() parks that thread without wedging the other's
        # loop, and no DB lock is held while waiting so it cannot deadlock against
        # the advisory lock.
        barrier = threading.Barrier(2)
        real_get = db.get_user_by_tg_uid

        async def _gated_get(uid: int):
            result = await real_get(uid)
            if uid in (EXIST_TARGET_A_TG_UID, EXIST_TARGET_B_TG_UID):
                barrier.wait(timeout=15)  # cross-thread rendezvous
            return result

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

        def _promote_owner(actor_uid: int, target_uid: int) -> None:
            actor = {"tg_uid": actor_uid, "role": "management"}
            _run(bot3.apply_promotion(object(), actor, target_uid, "owner"))

        t_a = threading.Thread(
            target=_promote_owner, args=(ACTOR_A_TG_UID, EXIST_TARGET_A_TG_UID)
        )
        t_b = threading.Thread(
            target=_promote_owner, args=(ACTOR_B_TG_UID, EXIST_TARGET_B_TG_UID)
        )
        t_a.start()
        t_b.start()
        t_a.join(timeout=30)
        t_b.join(timeout=30)
        assert not t_a.is_alive() and not t_b.is_alive(), "a promotion thread hung"

        # Exactly one of the two existing users became owner; the loser was
        # rejected by the cap check before its UPDATE, so its row is untouched —
        # still at its original 'user' role (NOT missing, unlike the pre-create
        # loser which never gets a row at all).
        role_a = _run(_owner_role_for(EXIST_TARGET_A_TG_UID))
        role_b = _run(_owner_role_for(EXIST_TARGET_B_TG_UID))
        assert {role_a, role_b} == {"owner", "user"}, (
            f"expected exactly one new owner, got A={role_a!r} B={role_b!r}"
        )

        # The winner's audit row landed; the loser's never did (its whole
        # transaction rolled back before reaching the role_events insert).
        winner_uid = EXIST_TARGET_A_TG_UID if role_a == "owner" else EXIST_TARGET_B_TG_UID
        loser_uid = EXIST_TARGET_B_TG_UID if role_a == "owner" else EXIST_TARGET_A_TG_UID
        win_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events "
            "WHERE target_tg_uid = %s AND new_role = 'owner'",
            (winner_uid,),
        ))
        lose_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events WHERE target_tg_uid = %s",
            (loser_uid,),
        ))
        assert win_events == 1
        assert lose_events == 0

        # The cap held exactly: the freed slot is now filled and not overrun.
        assert _run(db.count_owners()) == bot3.MAX_OWNERS

        # Both admins were answered — one a success ("is now owner"), the other
        # the cap rejection.
        assert any("is now" in t for t in recorder.texts)
        assert any("Owner cap reached" in t for t in recorder.texts)
    finally:
        _run(_cleanup_existing_owner_cap())


# ─── the demote+promote race: one slot freed as a new one tries to claim it ───
# All MAX_OWNERS slots are filled at race start. One management actor demotes
# DEMOTE_OWNER_TG_UID (owner→user) while a second management actor simultaneously
# promotes PROMOTE_TARGET_TG_UID (user→owner). The demotion runs through a
# direct transaction (replicating bot3's pmxc handler path) — it acquires no
# advisory lock — so the two transactions truly overlap on separate Postgres
# backends.
#
# Note: can_promote("management", "user") == False, so demoting an owner to user
# cannot go through apply_promotion/apply_role_change. In production the
# "remove promotion" button handler runs a direct transaction (bot3 lines
# ~1851-1872). The helper below replicates that path so the advisory-lock-free
# demotion and the advisory-lock-guarded promotion genuinely race.
#
# Two coherent outcomes are possible:
#
#   (a) The promotion's advisory lock fires BEFORE the demotion commits:
#       count_owners() returns MAX_OWNERS inside the promotion's tx, the
#       promotion is rejected with "Owner cap reached", and the demotion then
#       commits. Final owner count = MAX_OWNERS − 1.
#
#   (b) The demotion commits BEFORE the promotion counts:
#       count_owners() inside the promotion's tx returns MAX_OWNERS − 1, the
#       promotion succeeds, and the freed slot is immediately refilled.
#       Final owner count = MAX_OWNERS.
#
# The invariant under test: count_owners() ≤ MAX_OWNERS in BOTH cases, the
# demoted owner is no longer 'owner', and both actors received a coherent reply.
# Each action runs in its own OS thread, event loop and DB connection, so the two
# transactions run on genuinely parallel Postgres backends (same shape as the
# existing threaded owner-cap tests above).
async def _cleanup_demote_promote_race() -> None:
    """Remove every row the demote+promote race test created (or a crashed run
    left): DEMOTE_OWNER_TG_UID, PROMOTE_TARGET_TG_UID, and all sentinel seed-
    owner rows. Keyed strictly on sentinel uids so no real data is touched."""
    for uid in (DEMOTE_OWNER_TG_UID, PROMOTE_TARGET_TG_UID, *SEED_OWNER_TG_UIDS):
        await db.execute("DELETE FROM role_events WHERE target_tg_uid = %s", (uid,))
        await db.execute("DELETE FROM users WHERE tg_uid = %s", (uid,))


async def _seed_demote_promote_state() -> bool:
    """Seed the database so exactly MAX_OWNERS owners exist (sentinel seed pool
    fills MAX_OWNERS − 1 slots; DEMOTE_OWNER_TG_UID fills the last) and
    PROMOTE_TARGET_TG_UID exists as a non-owner 'user'.

    Returns False (→ skip) when real owners already occupy enough rows that we
    cannot reach MAX_OWNERS without touching real data."""
    base = await db.count_owners()
    # We need MAX_OWNERS − 1 sentinel seed owners plus DEMOTE_OWNER_TG_UID.
    need = (bot3.MAX_OWNERS - 1) - base
    if need < 0:
        return False  # real owners already exceed MAX_OWNERS − 1; cannot seed safely
    if need > len(SEED_OWNER_TG_UIDS):
        return False  # sentinel pool too small (should never happen)
    for i in range(need):
        uid = SEED_OWNER_TG_UIDS[i]
        await db.execute(
            "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
            "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'owner', true)",
            (uid, f"seedowner_{uuid.uuid4().hex[:10]}", "h", "s",
             bot3.placeholder_name(uid)),
        )
    # The owner whose slot will be freed by the racing demotion.
    await db.execute(
        "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
        "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'owner', true)",
        (DEMOTE_OWNER_TG_UID, f"demoteowner_{uuid.uuid4().hex[:10]}", "h", "s",
         bot3.placeholder_name(DEMOTE_OWNER_TG_UID)),
    )
    # The non-owner racing to claim the freed slot.
    await db.execute(
        "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
        "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'user', true)",
        (PROMOTE_TARGET_TG_UID, f"promotetgt_{uuid.uuid4().hex[:10]}", "h", "s",
         bot3.placeholder_name(PROMOTE_TARGET_TG_UID)),
    )
    return True


async def _demote_owner_direct(target_tg_uid: int, actor_tg_uid: int) -> str:
    """Demote target from owner to user via a direct transaction, replicating
    bot3's "remove promotion" pmxc handler path. apply_promotion cannot demote
    an owner to user because can_promote("management", "user") == False — the
    UI demotion goes through a separate inline transaction that skips that guard.

    Reads the target row first (via db.get_user_by_tg_uid, which is gated by
    the test's monkeypatch barrier) then runs the UPDATE + audit INSERT in a
    single atomic transaction. Returns "ok", "__noop__", or "__error__"."""
    # This read is the gate point: the test monkeypatches db.get_user_by_tg_uid
    # to hold both threads at a barrier so they enter their transactions together.
    target = await db.get_user_by_tg_uid(target_tg_uid)
    if not target:
        return "__missing__"
    try:
        async with db.transaction() as cur:
            await cur.execute(
                "SELECT role FROM users WHERE tg_uid = %s FOR UPDATE",
                (target_tg_uid,),
            )
            row = await cur.fetchone()
            if not row or row["role"] == "user":
                return "__noop__"
            prev_role = row["role"]
            await cur.execute(
                "UPDATE users SET role = 'user', token_version = token_version + 1, "
                "updated_at = now() WHERE tg_uid = %s",
                (target_tg_uid,),
            )
            await cur.execute(
                "INSERT INTO role_events (target_tg_uid, target_user_id, "
                "target_name, target_username, prev_role, new_role, source, "
                "actor_tg_uid, actor_role) "
                "VALUES (%s, %s, %s, %s, %s, 'user', 'bot_promote', %s, 'management')",
                (target_tg_uid, target["user_id"], target["name"],
                 target.get("tg_username"), prev_role, actor_tg_uid),
            )
            return "ok"
    except Exception:
        return "__error__"


def test_concurrent_demote_and_promote_owner_cap_consistent_threaded(monkeypatch):
    """Owner count stays ≤ MAX_OWNERS when a demotion and a promotion race.

    Start with MAX_OWNERS owners (all slots full). Race:
      Thread A — demotes DEMOTE_OWNER_TG_UID owner → user (direct transaction,
                 no advisory lock, mirroring the bot's "remove promotion" UI
                 path which cannot go through apply_promotion).
      Thread B — management actor promotes PROMOTE_TARGET_TG_UID user → owner
                 via apply_promotion (acquires advisory lock, counts owners).

    Whichever ordering Postgres produces, the final owner count must be exactly
    MAX_OWNERS − 1 (promotion rejected) or MAX_OWNERS (promotion accepted),
    never above MAX_OWNERS. The demoted owner must always end up as 'user'.
    """
    if not _run(_db_reachable()):
        pytest.skip("no real Postgres with the dashboard schema is available")
    _run(_cleanup_demote_promote_race())
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        if not _run(_seed_demote_promote_state()):
            pytest.skip(
                "real owners already fill enough slots that we cannot reach the "
                "MAX_OWNERS state without disturbing real data"
            )

        # Gate both threads past their get_user_by_tg_uid pre-check so they
        # enter their transactions at the same instant. A threading.Barrier is
        # correct here: each action owns its own thread so the blocking wait()
        # parks that thread without wedging the other's loop, and no DB lock is
        # held while waiting so there is no risk of deadlock against the
        # advisory lock.
        barrier = threading.Barrier(2)
        real_get = db.get_user_by_tg_uid

        async def _gated_get(uid: int):
            result = await real_get(uid)
            if uid in (DEMOTE_OWNER_TG_UID, PROMOTE_TARGET_TG_UID):
                barrier.wait(timeout=15)
            return result

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

        def _demote() -> None:
            _run(_demote_owner_direct(DEMOTE_OWNER_TG_UID, ACTOR_A_TG_UID))

        def _promote() -> None:
            actor = {"tg_uid": ACTOR_B_TG_UID, "role": "management"}
            _run(bot3.apply_promotion(object(), actor, PROMOTE_TARGET_TG_UID, "owner"))

        t_demote = threading.Thread(target=_demote)
        t_promote = threading.Thread(target=_promote)
        t_demote.start()
        t_promote.start()
        t_demote.join(timeout=30)
        t_promote.join(timeout=30)
        assert not t_demote.is_alive() and not t_promote.is_alive(), (
            "a demote/promote thread hung"
        )

        # The demotion is unconditionally uncapped — the demoted owner must
        # have been moved to 'user' regardless of what the promotion did.
        demote_role = _run(_owner_role_for(DEMOTE_OWNER_TG_UID))
        assert demote_role == "user", (
            f"DEMOTE_OWNER_TG_UID should be 'user' after demotion, got {demote_role!r}"
        )

        # The cap must never have been overrun.
        final_count = _run(db.count_owners())
        assert final_count <= bot3.MAX_OWNERS, (
            f"owner cap overrun: final count {final_count} > MAX_OWNERS {bot3.MAX_OWNERS}"
        )

        # Determine which of the two coherent outcomes landed and assert the
        # matching invariants for each.
        promote_role = _run(_owner_role_for(PROMOTE_TARGET_TG_UID))
        if promote_role == "owner":
            # Outcome (b): demotion committed before promotion's count → slot
            # was visible, promotion accepted, count back to MAX_OWNERS.
            assert final_count == bot3.MAX_OWNERS, (
                f"promotion accepted but final count is {final_count}, "
                f"expected {bot3.MAX_OWNERS}"
            )
            # One audit row for the promotion to 'owner'.
            promo_events = _run(_count(
                "SELECT COUNT(*)::int AS n FROM role_events "
                "WHERE target_tg_uid = %s AND new_role = 'owner'",
                (PROMOTE_TARGET_TG_UID,),
            ))
            assert promo_events == 1, (
                f"expected 1 owner-promotion audit row, got {promo_events}"
            )
            # Promotion actor received a success reply via apply_promotion.
            assert any("is now" in t for t in recorder.texts), (
                "expected at least one 'is now' success reply from the promotion"
            )
        else:
            # Outcome (a): promotion counted MAX_OWNERS before the demotion
            # committed → rejected; only the demotion committed.
            assert promote_role == "user", (
                f"PROMOTE_TARGET_TG_UID expected 'user' (rejected), got {promote_role!r}"
            )
            assert final_count == bot3.MAX_OWNERS - 1, (
                f"promotion rejected but final count is {final_count}, "
                f"expected {bot3.MAX_OWNERS - 1}"
            )
            # No audit row for a rejected promotion.
            promo_events = _run(_count(
                "SELECT COUNT(*)::int AS n FROM role_events "
                "WHERE target_tg_uid = %s AND new_role = 'owner'",
                (PROMOTE_TARGET_TG_UID,),
            ))
            assert promo_events == 0, (
                f"expected 0 owner-promotion audit rows for rejected promotion, "
                f"got {promo_events}"
            )
            assert any("Owner cap reached" in t for t in recorder.texts), (
                "expected 'Owner cap reached' reply to the rejected promotion"
            )

        # One audit row for the demotion from 'owner' regardless of outcome.
        demote_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events "
            "WHERE target_tg_uid = %s AND prev_role = 'owner' AND new_role = 'user'",
            (DEMOTE_OWNER_TG_UID,),
        ))
        assert demote_events == 1, (
            f"expected 1 demotion audit row for DEMOTE_OWNER_TG_UID, got {demote_events}"
        )

        # Promotion actor received at least one reply (no silent failure).
        assert len(recorder.texts) >= 1, (
            f"expected at least 1 reply to the promoting actor, got: {recorder.texts!r}"
        )
    finally:
        _run(_cleanup_demote_promote_race())


# ─── concurrent-demotion helpers ─────────────────────────────────────────────
async def _cleanup_conc_demote() -> None:
    """Remove every row the concurrent-demotion test created (or a crashed run
    left). Keyed strictly on the sentinel uid so no real data is touched."""
    await db.execute(
        "DELETE FROM role_events WHERE target_tg_uid = %s", (CONC_DEMOTE_OWNER_TG_UID,)
    )
    await db.execute(
        "DELETE FROM users WHERE tg_uid = %s", (CONC_DEMOTE_OWNER_TG_UID,)
    )


async def _seed_conc_demote() -> None:
    """Insert CONC_DEMOTE_OWNER_TG_UID as an 'owner' so both racing threads
    find a real owner row to demote."""
    await db.execute(
        "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
        "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'owner', true)",
        (
            CONC_DEMOTE_OWNER_TG_UID,
            f"concdmowner_{uuid.uuid4().hex[:10]}",
            "h",
            "s",
            bot3.placeholder_name(CONC_DEMOTE_OWNER_TG_UID),
        ),
    )


# ─── the race: two parallel demotions of the SAME owner, loser gets __noop__ ─
# Two management admins independently decide to demote the same owner to user at
# the same instant. Each runs _demote_owner_direct, which takes a
# SELECT … FOR UPDATE row-lock on the target before doing any work. The lock
# guarantees strict serial execution of the two transactions:
#
#   Winner: reads role='owner' under the lock, updates → 'user', inserts 1 audit
#           row, commits.
#   Loser:  acquires the lock after the winner commits, reads role='user', hits
#           the early-return guard (row["role"] == "user") → returns "__noop__",
#           never reaches the UPDATE or role_events INSERT.
#
# Each action runs in its own OS thread, event loop and DB connection (the
# strictly-more-concurrent shape). A threading.Barrier holds both past their
# get_user_by_tg_uid pre-read so they enter their transactions together.
def test_concurrent_demotion_of_same_owner_is_idempotent_threaded(monkeypatch):
    """Two admins demoting the SAME owner simultaneously: loser is __noop__, not
    a duplicate success.

    Invariants after the race:
      - CONC_DEMOTE_OWNER_TG_UID has role 'user'.
      - Exactly one role_events row (prev_role='owner', new_role='user').
      - One thread returned "ok" and the other returned "__noop__" — the loser
        was silently elided, not double-applied.
    """
    if not _run(_db_reachable()):
        pytest.skip("no real Postgres with the dashboard schema is available")
    _run(_cleanup_conc_demote())
    try:
        _run(_seed_conc_demote())

        # Gate both threads past their get_user_by_tg_uid pre-read so they
        # enter their FOR UPDATE transactions at the same instant. A
        # threading.Barrier is correct here: each action owns its own thread,
        # so the blocking wait() parks that thread without wedging the other's
        # loop, and no DB lock is held while waiting so there is no risk of
        # deadlock against the row lock.
        barrier = threading.Barrier(2)
        real_get = db.get_user_by_tg_uid

        async def _gated_get(uid: int):
            result = await real_get(uid)
            if uid == CONC_DEMOTE_OWNER_TG_UID:
                barrier.wait(timeout=15)
            return result

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

        results: list[str] = []

        def _demote(actor_uid: int) -> None:
            r = _run(_demote_owner_direct(CONC_DEMOTE_OWNER_TG_UID, actor_uid))
            results.append(r)  # list.append is GIL-atomic

        t_a = threading.Thread(target=_demote, args=(ACTOR_A_TG_UID,))
        t_b = threading.Thread(target=_demote, args=(ACTOR_B_TG_UID,))
        t_a.start()
        t_b.start()
        t_a.join(timeout=30)
        t_b.join(timeout=30)
        assert not t_a.is_alive() and not t_b.is_alive(), "a demotion thread hung"

        # The owner must have been moved to 'user' regardless of which thread
        # won the row lock.
        final_role = _run(_owner_role_for(CONC_DEMOTE_OWNER_TG_UID))
        assert final_role == "user", (
            f"expected 'user' after concurrent demotions, got {final_role!r}"
        )

        # Exactly one audit row — the loser read role='user' under the FOR
        # UPDATE lock and returned __noop__ before ever reaching the INSERT.
        demote_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events "
            "WHERE target_tg_uid = %s AND prev_role = 'owner' AND new_role = 'user'",
            (CONC_DEMOTE_OWNER_TG_UID,),
        ))
        assert demote_events == 1, (
            f"expected exactly 1 demotion audit row, got {demote_events}"
        )

        # One actor got "ok", the other got "__noop__" — not a double success.
        # Sort for a stable assertion (thread ordering is non-deterministic).
        assert sorted(results) == ["__noop__", "ok"], (
            f"expected one 'ok' and one '__noop__' reply, got {results!r}"
        )
    finally:
        _run(_cleanup_conc_demote())


# ─── within-row demotion ↔ re-promotion race helpers ─────────────────────────
async def _cleanup_repromote_race() -> None:
    """Remove every row the demotion/re-promotion race test created (or a
    crashed run left): SAME_TARGET_TG_UID and all sentinel seed-owner rows.
    Keyed strictly on sentinel uids so no real data is touched."""
    for uid in (SAME_TARGET_TG_UID, *SEED_OWNER_TG_UIDS):
        await db.execute("DELETE FROM role_events WHERE target_tg_uid = %s", (uid,))
        await db.execute("DELETE FROM users WHERE tg_uid = %s", (uid,))


async def _seed_repromote_state() -> bool:
    """Seed the database so exactly MAX_OWNERS owners exist (sentinel seed pool
    fills MAX_OWNERS − 1 slots; SAME_TARGET_TG_UID fills the last slot as an
    owner). Returns False (→ skip) when real owners already occupy enough rows
    that we cannot reach MAX_OWNERS without touching real data."""
    base = await db.count_owners()
    need = (bot3.MAX_OWNERS - 1) - base
    if need < 0:
        return False
    if need > len(SEED_OWNER_TG_UIDS):
        return False
    for i in range(need):
        uid = SEED_OWNER_TG_UIDS[i]
        await db.execute(
            "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
            "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'owner', true)",
            (uid, f"seedowner_{uuid.uuid4().hex[:10]}", "h", "s",
             bot3.placeholder_name(uid)),
        )
    await db.execute(
        "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, "
        "name, role, access_granted) VALUES (%s, %s, %s, %s, %s, 'owner', true)",
        (SAME_TARGET_TG_UID, f"sametgt_{uuid.uuid4().hex[:10]}", "h", "s",
         bot3.placeholder_name(SAME_TARGET_TG_UID)),
    )
    return True


# ─── the race: one admin demotes SAME_TARGET, another re-promotes SAME_TARGET ─
# Start: MAX_OWNERS owners (all slots full), SAME_TARGET_TG_UID is one of them.
# Thread A demotes SAME_TARGET (owner→user) via _demote_owner_direct (no
# advisory lock). Thread B promotes SAME_TARGET (→owner) via apply_promotion
# (acquires advisory lock inside _apply_role_change). Both take a
# SELECT … FOR UPDATE on the SAME row, so they strictly serialize.
#
# Two coherent orderings:
#
#   (a) Demotion wins the row-lock first → target becomes 'user', advisory lock
#       freed; promotion then re-acquires the row-lock, sees 'user', acquires
#       advisory lock, counts MAX_OWNERS − 1, promotes back to 'owner'.
#       Final role: 'owner'. Two role_events rows. Owner count: MAX_OWNERS.
#
#   (b) Promotion wins the row-lock first → _apply_role_change sees
#       current_role == target_role == 'owner' → __noop__, releases without
#       writing. Demotion then acquires the lock, demotes to 'user'.
#       Final role: 'user'. One role_events row. Owner count: MAX_OWNERS − 1.
#
# In both orderings: final role ∈ {'owner', 'user'} (no torn write), exactly
# one role_events row per committed action, and owner count ≤ MAX_OWNERS.
def test_concurrent_demotion_and_repromote_same_target_threaded(monkeypatch):
    """A concurrent demotion and re-promotion of the SAME owner row serializes
    correctly via the FOR UPDATE row-lock.

    Invariants after the race (either outcome):
      - SAME_TARGET_TG_UID role is exactly 'owner' or 'user' — no corruption.
      - role_events row count equals the number of committed transitions:
          1 row (demotion only, outcome b) or 2 rows (demotion + re-promotion,
          outcome a).
      - The single users row for SAME_TARGET has no double-write: exactly one
        row exists, its role matches the last committed UPDATE.
      - Owner count never exceeds MAX_OWNERS.
    """
    if not _run(_db_reachable()):
        pytest.skip("no real Postgres with the dashboard schema is available")
    _run(_cleanup_repromote_race())
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        if not _run(_seed_repromote_state()):
            pytest.skip(
                "real owners already fill enough slots that we cannot reach the "
                "MAX_OWNERS state without disturbing real data"
            )

        # Gate both threads past their get_user_by_tg_uid pre-read so they
        # enter their transactions at the same instant. A threading.Barrier is
        # correct here: each action owns its own thread, so the blocking wait()
        # parks that thread without wedging the other's loop, and no DB lock is
        # held while waiting so there is no risk of deadlock against the row lock.
        barrier = threading.Barrier(2)
        real_get = db.get_user_by_tg_uid

        async def _gated_get(uid: int):
            result = await real_get(uid)
            if uid == SAME_TARGET_TG_UID:
                barrier.wait(timeout=15)
            return result

        monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

        def _demote() -> None:
            _run(_demote_owner_direct(SAME_TARGET_TG_UID, ACTOR_A_TG_UID))

        def _promote() -> None:
            actor = {"tg_uid": ACTOR_B_TG_UID, "role": "management"}
            _run(bot3.apply_promotion(object(), actor, SAME_TARGET_TG_UID, "owner"))

        t_demote = threading.Thread(target=_demote)
        t_promote = threading.Thread(target=_promote)
        t_demote.start()
        t_promote.start()
        t_demote.join(timeout=30)
        t_promote.join(timeout=30)
        assert not t_demote.is_alive() and not t_promote.is_alive(), (
            "a demotion/re-promotion thread hung"
        )

        # The target must have exactly one users row — no double-write.
        user_count = _run(_count(
            "SELECT COUNT(*)::int AS n FROM users WHERE tg_uid = %s",
            (SAME_TARGET_TG_UID,),
        ))
        assert user_count == 1, (
            f"expected exactly 1 users row for SAME_TARGET, got {user_count}"
        )

        # The final role must be exactly 'owner' or 'user' (no torn/NULL value).
        final_role = _run(_owner_role_for(SAME_TARGET_TG_UID))
        assert final_role in ("owner", "user"), (
            f"final role must be 'owner' or 'user', got {final_role!r}"
        )

        # Owner count must never have exceeded MAX_OWNERS.
        final_count = _run(db.count_owners())
        assert final_count <= bot3.MAX_OWNERS, (
            f"owner cap overrun: final count {final_count} > MAX_OWNERS {bot3.MAX_OWNERS}"
        )

        # Verify per-outcome invariants.
        all_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events WHERE target_tg_uid = %s",
            (SAME_TARGET_TG_UID,),
        ))
        demotion_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events "
            "WHERE target_tg_uid = %s AND prev_role = 'owner' AND new_role = 'user'",
            (SAME_TARGET_TG_UID,),
        ))
        repromotion_events = _run(_count(
            "SELECT COUNT(*)::int AS n FROM role_events "
            "WHERE target_tg_uid = %s AND prev_role = 'user' AND new_role = 'owner'",
            (SAME_TARGET_TG_UID,),
        ))

        if final_role == "owner":
            # Outcome (a): demotion committed first, then re-promotion succeeded.
            # Exactly one demotion row + one re-promotion row; count back to MAX_OWNERS.
            assert demotion_events == 1, (
                f"outcome (a): expected 1 demotion audit row, got {demotion_events}"
            )
            assert repromotion_events == 1, (
                f"outcome (a): expected 1 re-promotion audit row, got {repromotion_events}"
            )
            assert all_events == 2, (
                f"outcome (a): expected 2 total role_events rows, got {all_events}"
            )
            assert final_count == bot3.MAX_OWNERS, (
                f"outcome (a): re-promotion accepted but count is {final_count}, "
                f"expected {bot3.MAX_OWNERS}"
            )
        else:
            # Outcome (b): promotion was a __noop__ (target already owner under
            # the lock), then demotion committed. One demotion row; no re-promotion.
            assert demotion_events == 1, (
                f"outcome (b): expected 1 demotion audit row, got {demotion_events}"
            )
            assert repromotion_events == 0, (
                f"outcome (b): expected 0 re-promotion audit rows, got {repromotion_events}"
            )
            assert all_events == 1, (
                f"outcome (b): expected 1 total role_events row, got {all_events}"
            )
            assert final_count == bot3.MAX_OWNERS - 1, (
                f"outcome (b): demotion only but count is {final_count}, "
                f"expected {bot3.MAX_OWNERS - 1}"
            )

        # The promotion actor (Thread B) must always receive a reply from
        # apply_promotion — either a success message or an __noop__ "already
        # owner" notice. (_demote_owner_direct is a raw transaction helper that
        # never calls send, so ACTOR_A_TG_UID won't appear in recorder.calls.)
        assert any(chat_id == ACTOR_B_TG_UID for chat_id, _ in recorder.calls), (
            f"expected at least one reply to the promotion actor (ACTOR_B), "
            f"got: {recorder.calls!r}"
        )
    finally:
        _run(_cleanup_repromote_race())
