"""Real-Postgres integration test for the bot3 owner-cap concurrency guard.

The owner cap (``MAX_OWNERS``) is otherwise verified only with in-memory fakes
(``test_bot3_owner_cap.py``) that *record* the SQL and assert the advisory lock
is issued before the count + write. Those fakes can prove the ordering, but not
the thing that ordering actually buys: that two genuinely simultaneous owner
promotions, racing on two *real* Postgres connections, cannot both pass the
``COUNT(*) < MAX_OWNERS`` check and both commit. That guarantee rests entirely on
``pg_advisory_xact_lock`` (``db.acquire_owner_cap_lock``) serializing the
count-then-insert window across connections — live behaviour no fake can stand in
for.

The companion ``test_bot3_promotion_integration.py`` proves the brand-new-user
pre-create race is safe against the live ``users.tg_uid`` PRIMARY KEY, but it
deliberately promotes to the plain ``user`` role to isolate that PK collision.
This module covers the separate, money/security-sensitive guarantee — the owner
cap itself — against the same real Postgres.

It does so two ways:

  * ``test_concurrent_owner_promotions_one_winner_one_capped`` seeds the global
    owner count to exactly one slot below the cap, then runs two genuine,
    parallel ``apply_promotion(..., "owner")`` calls (each in its own OS thread,
    event loop and DB connection) for two *different* brand-new targets. A
    ``threading.Barrier`` holds both past their "no existing row" pre-check so
    they truly collide on the count-then-insert window. Exactly one promotion
    wins (its owner row + audit row commit); the other is cleanly refused with
    the "Owner cap reached" message and leaves no ``users`` or ``role_events``
    orphan. The global owner count never exceeds ``MAX_OWNERS``.

  * ``test_owner_cap_refusal_rolls_back_without_orphan`` deterministically pins
    the loser branch: the global owner count is seeded right up to the cap, then
    a single owner promotion of a brand-new target is refused. The whole
    transaction must roll back — no ``users`` row, no orphan ``role_events`` row,
    and the global owner count unchanged. This needs no scheduling luck.

The cap is enforced against the *global* ``COUNT(*) WHERE role = 'owner'``, so the
setup reads the current real owner count and tops it up to the desired total with
sentinel owner rows (large-negative ``tg_uid`` values that cannot collide with a
genuine account). All cleanup is keyed strictly on those sentinel uids, so no
real row is ever touched. If the database already holds more owners than the test
needs to control, the test skips rather than mutating real state.

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
from config import MAX_OWNERS

# Sentinel Telegram uids for the two brand-new targets, the two racing admins,
# and a small pool of seeded "filler" owners used to position the global owner
# count. Real Telegram uids are positive, so large negative values cannot
# collide with a genuine account in a shared dev database. A per-process random
# run id offsets them so two CI workers sharing the same database don't collide
# on each other's sentinels. All cleanup is keyed strictly on these values.
_RUN = random.randint(1, 9_000_000)
_BASE = -(880_000_000 + _RUN * 100)
TARGET_A_TG_UID = _BASE - 1
TARGET_B_TG_UID = _BASE - 2
ACTOR_A_TG_UID = _BASE - 3
ACTOR_B_TG_UID = _BASE - 4
# Enough filler-owner slots to top the global count up to MAX_OWNERS if needed.
SEED_OWNER_UIDS = tuple(_BASE - 10 - i for i in range(MAX_OWNERS))

ALL_SENTINELS = (
    TARGET_A_TG_UID,
    TARGET_B_TG_UID,
    ACTOR_A_TG_UID,
    ACTOR_B_TG_UID,
    *SEED_OWNER_UIDS,
)


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
    strictly on the sentinel uids so no real data is ever touched. Access keys are
    deleted first in case the database enforces an FK from
    ``access_keys.redeemed_by_tg_uid`` to ``users.tg_uid``."""
    for uid in ALL_SENTINELS:
        await db.execute("DELETE FROM access_keys WHERE created_by_tg_uid = %s", (uid,))
        await db.execute("DELETE FROM role_events WHERE target_tg_uid = %s", (uid,))
        await db.execute("DELETE FROM users WHERE tg_uid = %s", (uid,))


async def _count(sql: str, params: tuple) -> int:
    row = await db.fetchone(sql, params)
    return int(row["n"]) if row else 0


async def _global_owner_count() -> int:
    """The live, global owner count the cap is enforced against."""
    return await _count("SELECT COUNT(*)::int AS n FROM users WHERE role = 'owner'", ())


async def _user_role(uid: int) -> str | None:
    row = await db.fetchone("SELECT role FROM users WHERE tg_uid = %s", (uid,))
    return row["role"] if row else None


async def _events_for(uid: int) -> int:
    return await _count(
        "SELECT COUNT(*)::int AS n FROM role_events WHERE target_tg_uid = %s", (uid,)
    )


async def _seed_owner(uid: int) -> None:
    """Insert one sentinel owner row to occupy a slot in the global owner count."""
    await db.execute(
        "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, name, "
        "role, access_granted) VALUES (%s, %s, %s, %s, %s, 'owner', true)",
        (uid, f"itest_seed_{uuid.uuid4().hex[:10]}", "h", "s",
         bot3.placeholder_name(uid)),
    )


async def _seed_user(uid: int, role: str = "user") -> None:
    """Insert one sentinel *non-owner* user row — an already-existing member who
    holds a ``users`` row and can later be promoted to owner. This is the common
    real-world promote target (vs. the brand-new pre-create path)."""
    await db.execute(
        "INSERT INTO users (tg_uid, user_id, password_hash, password_salt, name, "
        "role, access_granted) VALUES (%s, %s, %s, %s, %s, %s, true)",
        (uid, f"itest_member_{uuid.uuid4().hex[:10]}", "h", "s",
         bot3.placeholder_name(uid), role),
    )


async def _seed_owner_key(creator_uid: int) -> str:
    """Insert one unredeemed sentinel access key minting the ``owner`` role and
    return its code. ``created_by_tg_uid`` is a sentinel so ``_cleanup`` removes
    it. A genuine member redeeming this key would become an owner — exactly the
    redeem branch that contends for the cap's free slot."""
    code = bot3.gen_key_code()
    await db.execute(
        "INSERT INTO access_keys (id, code, role, expires_at, created_by_tg_uid, "
        "created_by_role) VALUES (%s, %s, 'owner', NULL, %s, 'management')",
        (str(uuid.uuid4()), code, creator_uid),
    )
    return code


async def _seed_user_key(creator_uid: int) -> str:
    """Insert one unredeemed sentinel access key minting the plain ``user`` role
    and return its code. ``created_by_tg_uid`` is a sentinel so ``_cleanup``
    removes it. The ``user`` role deliberately sidesteps the owner-cap branch in
    ``handle_key_submission`` (which only runs for ``owner`` keys), so a race on
    this key contends purely on the single-use conditional claim — never the
    owner cap."""
    code = bot3.gen_key_code()
    await db.execute(
        "INSERT INTO access_keys (id, code, role, expires_at, created_by_tg_uid, "
        "created_by_role) VALUES (%s, %s, 'user', NULL, %s, 'management')",
        (str(uuid.uuid4()), code, creator_uid),
    )
    return code


async def _key_redeemed_by(code: str) -> int | None:
    """Who (if anyone) the access key was committed as redeemed by."""
    row = await db.fetchone(
        "SELECT redeemed_by_tg_uid FROM access_keys WHERE code = %s", (code,)
    )
    return row["redeemed_by_tg_uid"] if row else None


async def _seed_owners_to(total: int) -> bool:
    """Top the *global* owner count up to exactly ``total`` using sentinel owner
    rows. Returns False (→ skip) if the database already holds more owners than
    ``total`` — we must never delete real owners to make room."""
    existing = await _global_owner_count()
    needed = total - existing
    if needed < 0:
        return False
    for i in range(needed):
        await _seed_owner(SEED_OWNER_UIDS[i])
    return True


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


# ─── the race: two parallel owner promotions, one winner, cap never exceeded ──
def test_concurrent_owner_promotions_one_winner_one_capped(monkeypatch, real_db):
    # Position the global owner count one slot below the cap: exactly one owner
    # promotion can succeed, the other must be refused.
    if not _run(_seed_owners_to(MAX_OWNERS - 1)):
        pytest.skip(
            "database already holds >= MAX_OWNERS-1 owners; cannot set up exactly "
            "one free slot without mutating real data"
        )
    assert _run(_global_owner_count()) == MAX_OWNERS - 1

    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)

    # Each promotion mints its own candidate user_id; the two targets differ, so
    # the collision is forced purely by the owner-cap count-then-insert window
    # (not any tg_uid/user_id uniqueness) — the exact guarantee the advisory lock
    # closes.
    _ids = iter(f"itest_own_{uuid.uuid4().hex[:10]}_{i}" for i in range(2))
    _ids_lock = threading.Lock()

    async def _uid():
        with _ids_lock:
            return next(_ids)

    monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

    # Hold both promotions until EACH has finished its "is there an existing
    # row?" pre-check and seen None, so neither can commit before the other even
    # begins — they truly contend for the cap's free slot.
    barrier = threading.Barrier(2)
    real_get = db.get_user_by_tg_uid
    targets = {TARGET_A_TG_UID, TARGET_B_TG_UID}

    async def _gated_get(uid: int):
        result = await real_get(uid)
        if uid in targets:
            barrier.wait(timeout=15)  # cross-thread rendezvous
        return result

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

    def _promote(actor_uid: int, target_uid: int) -> None:
        actor = {"tg_uid": actor_uid, "role": "management"}  # management -> owner
        _run(bot3.apply_promotion(object(), actor, target_uid, "owner"))

    t_a = threading.Thread(target=_promote, args=(ACTOR_A_TG_UID, TARGET_A_TG_UID))
    t_b = threading.Thread(target=_promote, args=(ACTOR_B_TG_UID, TARGET_B_TG_UID))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)
    assert not t_a.is_alive() and not t_b.is_alive(), "a promotion thread hung"

    # Exactly one target became an owner; the other has no users row at all.
    role_a = _run(_user_role(TARGET_A_TG_UID))
    role_b = _run(_user_role(TARGET_B_TG_UID))
    winners = [u for u, r in ((TARGET_A_TG_UID, role_a), (TARGET_B_TG_UID, role_b))
               if r == "owner"]
    losers = [u for u, r in ((TARGET_A_TG_UID, role_a), (TARGET_B_TG_UID, role_b))
              if r is None]
    assert winners == [w for w in winners if w], "expected exactly one owner"
    assert len(winners) == 1, f"expected one winner, got roles a={role_a} b={role_b}"
    assert len(losers) == 1, f"expected one fully-rolled-back loser, got {losers}"

    winner, loser = winners[0], losers[0]

    # The winner committed atomically: its owner row has a matching audit row.
    assert _run(_events_for(winner)) == 1
    # The loser's transaction rolled back whole — no orphan audit row from the
    # role_events insert that follows the users insert in the same tx.
    assert _run(_events_for(loser)) == 0

    # The core safety guarantee: the cap was never breached. With one slot free
    # and two racers, the global owner count lands at exactly MAX_OWNERS — never
    # MAX_OWNERS + 1 (which is what a lost count-then-write race would produce).
    total = _run(_global_owner_count())
    assert total == MAX_OWNERS
    assert total <= MAX_OWNERS

    # Both admins were answered: one a success, the other the cap refusal.
    assert any("Promoted" in t for t in recorder.texts)
    assert any("Owner cap reached" in t for t in recorder.texts)


# ─── the race on the EXISTING-user promote path: one winner, one capped ───────
# The brand-new race above flows through ``apply_promotion``'s pre-create branch.
# The far more common real-world case is two admins promoting two *already-
# existing* members to owner at the same instant: each target already holds a
# ``users`` row, so the promotion runs through ``_apply_role_change`` — a
# separate count-then-UPDATE window guarded by the *same* advisory lock. Unlike
# the brand-new path (whose ``users.tg_uid`` PRIMARY KEY would also reject a
# duplicate), nothing here collides except the owner-cap count itself, so this
# isolates the exact guarantee the lock buys on the existing-user branch:
# ``pg_advisory_xact_lock`` serializing the count-then-UPDATE across two live
# connections. The loser's role must be left *unchanged* (still its original
# non-owner role — not deleted, since the row pre-existed) with no orphan audit
# row, and the global owner count must never exceed ``MAX_OWNERS``.
def test_concurrent_existing_user_owner_promotions_one_winner_one_capped(monkeypatch, real_db):
    # Position the global owner count one slot below the cap: exactly one of the
    # two existing-member promotions can succeed, the other must be refused.
    if not _run(_seed_owners_to(MAX_OWNERS - 1)):
        pytest.skip(
            "database already holds >= MAX_OWNERS-1 owners; cannot set up exactly "
            "one free slot without mutating real data"
        )
    # Seed the two targets as already-existing, non-owner members (the real-world
    # shape: each has a users row before either promotion runs).
    _run(_seed_user(TARGET_A_TG_UID, "user"))
    _run(_seed_user(TARGET_B_TG_UID, "base_admin"))
    assert _run(_global_owner_count()) == MAX_OWNERS - 1

    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)

    # Hold both promotions until EACH has finished ``apply_promotion``'s
    # "is there an existing row?" pre-check (which here returns the seeded
    # member), so neither can commit before the other even enters
    # ``_apply_role_change`` — they truly contend for the cap's single free slot.
    # The per-target ``SELECT ... FOR UPDATE`` inside ``_apply_role_change`` locks
    # each target's own (distinct) row, so it never blocks the other thread; the
    # only real contention is the owner-cap advisory lock.
    barrier = threading.Barrier(2)
    real_get = db.get_user_by_tg_uid
    targets = {TARGET_A_TG_UID, TARGET_B_TG_UID}

    async def _gated_get(uid: int):
        result = await real_get(uid)
        if uid in targets:
            barrier.wait(timeout=15)  # cross-thread rendezvous
        return result

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

    def _promote(actor_uid: int, target_uid: int) -> None:
        actor = {"tg_uid": actor_uid, "role": "management"}  # management -> owner
        _run(bot3.apply_promotion(object(), actor, target_uid, "owner"))

    t_a = threading.Thread(target=_promote, args=(ACTOR_A_TG_UID, TARGET_A_TG_UID))
    t_b = threading.Thread(target=_promote, args=(ACTOR_B_TG_UID, TARGET_B_TG_UID))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)
    assert not t_a.is_alive() and not t_b.is_alive(), "a promotion thread hung"

    # Exactly one target became an owner; the other keeps its *original* role —
    # the loser's row pre-existed, so a refused promotion must leave it intact,
    # never drop or delete it.
    role_a = _run(_user_role(TARGET_A_TG_UID))
    role_b = _run(_user_role(TARGET_B_TG_UID))
    by_uid = {TARGET_A_TG_UID: role_a, TARGET_B_TG_UID: role_b}
    original = {TARGET_A_TG_UID: "user", TARGET_B_TG_UID: "base_admin"}

    winners = [u for u, r in by_uid.items() if r == "owner"]
    losers = [u for u, r in by_uid.items() if r == original[u]]
    assert len(winners) == 1, f"expected one winner, got roles a={role_a} b={role_b}"
    assert len(losers) == 1, (
        f"expected one loser left at its original role, got roles a={role_a} b={role_b}"
    )

    winner, loser = winners[0], losers[0]

    # The winner committed atomically: its owner role change has a matching audit
    # row.
    assert _run(_events_for(winner)) == 1
    # The loser's transaction rolled back whole — the ``__owner_cap__`` refusal
    # returns before the UPDATE/INSERT, so no orphan ``role_events`` row leaks.
    assert _run(_events_for(loser)) == 0

    # The core safety guarantee: the cap was never breached. With one slot free
    # and two racers, the global owner count lands at exactly MAX_OWNERS — never
    # MAX_OWNERS + 1 (which is what a lost count-then-UPDATE race would produce).
    total = _run(_global_owner_count())
    assert total == MAX_OWNERS
    assert total <= MAX_OWNERS

    # Both admins were answered: one a success ("is now Owner"), the other the
    # cap refusal.
    assert any("is now" in t and "Owner" in t for t in recorder.texts)
    assert any("Owner cap reached" in t for t in recorder.texts)


class _Msg:
    """Minimal stand-in for the Pyrogram message ``handle_key_submission`` reads:
    only ``msg.chat.id`` (the redeemer's Telegram uid) is touched."""

    def __init__(self, chat_id: int) -> None:
        self.chat = type("C", (), {"id": chat_id})()


# ─── the MIXED race: a key-redeem owner vs. a promote owner, one free slot ────
# The two tests above race two writers of the *same* kind (both promotes). The
# remaining, untested gap is the mixed collision: one writer becomes an owner by
# *redeeming an owner access key* (``handle_key_submission``'s owner branch — a
# count-then-INSERT inside its own transaction, guarded by the advisory lock only
# *after* it claims the single-use key), while a second writer promotes a
# different brand-new target to owner (``apply_promotion``'s pre-create branch).
# Both contend for the same single free owner slot at the same instant. This live
# cross-connection guarantee for the redeem branch is otherwise only covered by
# in-memory fakes (``test_bot3_owner_cap.py``), which can prove the lock→count→
# insert ordering but not that the lock actually serializes two real connections.
#
# A ``threading.Barrier`` holds both writers past their "is there already a
# users row for this target?" pre-check (each sees None) so neither can commit
# before the other even opens its transaction — they truly collide on the cap's
# count-then-write window. Exactly one wins; the other is refused (redeem path:
# "owner slot is full"; promote path: "Owner cap reached"). The loser's whole
# transaction rolls back: no ``users``/``role_events`` orphan, and if the redeem
# path is the loser its single-use key claim is rolled back too (the key stays
# unredeemed, reusable). The global owner count never exceeds ``MAX_OWNERS``.
def test_concurrent_key_redeem_vs_promote_one_winner_one_capped(monkeypatch, real_db):
    # Position the global owner count one slot below the cap: exactly one of the
    # two writers can succeed, the other must be refused.
    if not _run(_seed_owners_to(MAX_OWNERS - 1)):
        pytest.skip(
            "database already holds >= MAX_OWNERS-1 owners; cannot set up exactly "
            "one free slot without mutating real data"
        )
    # Mint an unredeemed owner key for the redeem writer to consume.
    redeem_code = _run(_seed_owner_key(ACTOR_A_TG_UID))
    assert _run(_global_owner_count()) == MAX_OWNERS - 1

    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)

    # Neutralize the redeem path's side requirements so the only contention left
    # is the owner cap: channel gate always satisfied, no saturation alert side
    # effects, and each writer mints a distinct candidate user_id (so no
    # user_id/tg_uid uniqueness — only the cap's count-then-write — can decide
    # the race).
    async def _members_ok(_uid):
        return {"all": [], "missing": []}

    monkeypatch.setattr(bot3, "check_channel_membership", _members_ok)

    _ids = iter(f"itest_mix_{uuid.uuid4().hex[:10]}_{i}" for i in range(2))
    _ids_lock = threading.Lock()

    async def _uid():
        with _ids_lock:
            return next(_ids)

    monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

    # Hold both writers until EACH has finished its "is there an existing row for
    # this target?" pre-check and seen None — the redeem path checks its redeemer
    # (chat_id), the promote path checks its target — so neither can commit before
    # the other opens its transaction. They then truly contend for the cap's free
    # slot on the advisory lock.
    barrier = threading.Barrier(2)
    real_get = db.get_user_by_tg_uid
    targets = {TARGET_A_TG_UID, TARGET_B_TG_UID}

    async def _gated_get(uid: int):
        result = await real_get(uid)
        if uid in targets:
            barrier.wait(timeout=15)  # cross-thread rendezvous
        return result

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

    # Writer 1: redeem an owner key as brand-new target A.
    def _redeem() -> None:
        state = {"name": "Redeemer", "tg_username": "redeemer"}
        _run(bot3.handle_key_submission(object(), _Msg(TARGET_A_TG_UID), redeem_code, state))

    # Writer 2: promote brand-new target B to owner.
    def _promote() -> None:
        actor = {"tg_uid": ACTOR_B_TG_UID, "role": "management"}  # management -> owner
        _run(bot3.apply_promotion(object(), actor, TARGET_B_TG_UID, "owner"))

    t_redeem = threading.Thread(target=_redeem)
    t_promote = threading.Thread(target=_promote)
    t_redeem.start()
    t_promote.start()
    t_redeem.join(timeout=30)
    t_promote.join(timeout=30)
    assert not t_redeem.is_alive() and not t_promote.is_alive(), "a writer thread hung"

    # Exactly one target became an owner; the other has no users row at all (both
    # are brand-new, so a refused writer rolls fully back to None).
    role_redeem = _run(_user_role(TARGET_A_TG_UID))
    role_promote = _run(_user_role(TARGET_B_TG_UID))
    by_uid = {TARGET_A_TG_UID: role_redeem, TARGET_B_TG_UID: role_promote}
    winners = [u for u, r in by_uid.items() if r == "owner"]
    losers = [u for u, r in by_uid.items() if r is None]
    assert len(winners) == 1, (
        f"expected one owner, got redeem={role_redeem} promote={role_promote}"
    )
    assert len(losers) == 1, (
        f"expected one fully-rolled-back loser, got redeem={role_redeem} "
        f"promote={role_promote}"
    )
    winner, loser = winners[0], losers[0]

    # The winner committed atomically: its owner row has a matching audit row …
    assert _run(_events_for(winner)) == 1
    # … and the loser's transaction rolled back whole — no orphan audit row.
    assert _run(_events_for(loser)) == 0

    # The core safety guarantee: the cap was never breached. With one slot free
    # and two racers, the global owner count lands at exactly MAX_OWNERS — never
    # MAX_OWNERS + 1 (which is what a lost count-then-write race would produce).
    total = _run(_global_owner_count())
    assert total == MAX_OWNERS
    assert total <= MAX_OWNERS

    # Both writers were answered, and the key's single-use claim is consistent
    # with who won: if the redeem path won, the key committed as redeemed by its
    # target; if it lost, its claim rolled back and the key stays unredeemed.
    redeemed_by = _run(_key_redeemed_by(redeem_code))
    if winner == TARGET_A_TG_UID:
        # Redeem won, promote refused.
        assert any("Access granted" in t for t in recorder.texts)
        assert any("Owner cap reached" in t for t in recorder.texts)
        assert redeemed_by == TARGET_A_TG_UID
    else:
        # Promote won, redeem refused.
        assert any("Promoted" in t for t in recorder.texts)
        assert any("owner slot is full" in t for t in recorder.texts)
        assert redeemed_by is None


# ─── the single-use claim: two simultaneous redeemers of the SAME key ─────────
# All three tests above race writers for the owner cap's free slot. The remaining,
# separately money/security-sensitive concurrency guarantee on the very same
# redeem path is the *single-use key* claim: ``handle_key_submission`` claims a
# key with a conditional ``UPDATE access_keys SET redeemed_by_tg_uid = ...
# WHERE redeemed_by_tg_uid IS NULL RETURNING id`` and raises ``__lost_race__`` if
# no row comes back. Today that is only exercised by in-memory fakes
# (``test_bot3_owner_cap.py``); the live cross-connection behaviour — that two
# genuinely simultaneous redeemers of the SAME key produce exactly one account,
# with the other told "Another user just redeemed that key first" — is unproven
# against a real database.
#
# This seeds ONE unredeemed non-owner (``user`` role) key to isolate the claim
# from the owner cap entirely (the owner-cap branch only runs for ``owner`` keys),
# then runs two genuine, parallel ``handle_key_submission`` calls for two brand-
# new redeemers (each in its own OS thread, event loop and DB connection)
# submitting the SAME code. A ``threading.Barrier`` holds both past their
# "do you already have an account?" pre-check (each sees None) so neither can
# open its transaction before the other — they truly collide on the conditional
# claim. Exactly one redeemer wins (its ``users`` + ``role_events`` rows commit
# and the key ends up ``redeemed_by_tg_uid`` = that redeemer); the other is
# refused with "Another user just redeemed that key first" and leaves no orphan
# ``users``/``role_events`` rows. The key is single-use: it is redeemed by the
# winner, never both.
def test_concurrent_single_use_key_redeem_one_winner_one_lost_race(monkeypatch, real_db):
    # One unredeemed non-owner key, the sole contended resource.
    redeem_code = _run(_seed_user_key(ACTOR_A_TG_UID))
    assert _run(_key_redeemed_by(redeem_code)) is None

    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)

    # Neutralize the redeem path's side requirements so the only contention left
    # is the single-use claim: channel gate always satisfied, no saturation alert
    # side effects, and each redeemer mints a distinct candidate user_id (so no
    # user_id uniqueness — only the conditional claim — can decide the race).
    async def _members_ok(_uid):
        return {"all": [], "missing": []}

    monkeypatch.setattr(bot3, "check_channel_membership", _members_ok)

    _ids = iter(f"itest_su_{uuid.uuid4().hex[:10]}_{i}" for i in range(2))
    _ids_lock = threading.Lock()

    async def _uid():
        with _ids_lock:
            return next(_ids)

    monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

    # Hold both redeemers until EACH has finished its "do you already have an
    # account?" pre-check and seen None, so neither can open its transaction
    # before the other — they then truly contend on the conditional claim.
    barrier = threading.Barrier(2)
    real_get = db.get_user_by_tg_uid
    targets = {TARGET_A_TG_UID, TARGET_B_TG_UID}

    async def _gated_get(uid: int):
        result = await real_get(uid)
        if uid in targets:
            barrier.wait(timeout=15)  # cross-thread rendezvous
        return result

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

    def _redeem(redeemer_uid: int) -> None:
        state = {"name": bot3.placeholder_name(redeemer_uid), "tg_username": "redeemer"}
        _run(bot3.handle_key_submission(object(), _Msg(redeemer_uid), redeem_code, state))

    t_a = threading.Thread(target=_redeem, args=(TARGET_A_TG_UID,))
    t_b = threading.Thread(target=_redeem, args=(TARGET_B_TG_UID,))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)
    assert not t_a.is_alive() and not t_b.is_alive(), "a redeemer thread hung"

    # Exactly one redeemer got an account; the other has no users row at all
    # (both are brand-new, so a refused redeemer rolls fully back to None).
    role_a = _run(_user_role(TARGET_A_TG_UID))
    role_b = _run(_user_role(TARGET_B_TG_UID))
    by_uid = {TARGET_A_TG_UID: role_a, TARGET_B_TG_UID: role_b}
    winners = [u for u, r in by_uid.items() if r == "user"]
    losers = [u for u, r in by_uid.items() if r is None]
    assert len(winners) == 1, f"expected one account, got roles a={role_a} b={role_b}"
    assert len(losers) == 1, (
        f"expected one fully-rolled-back loser, got roles a={role_a} b={role_b}"
    )
    winner, loser = winners[0], losers[0]

    # The winner committed atomically: its users row has a matching audit row …
    assert _run(_events_for(winner)) == 1
    # … and the loser's transaction rolled back whole — the ``__lost_race__``
    # raise happens before the users/role_events inserts, so no orphan leaks.
    assert _run(_events_for(loser)) == 0

    # The core single-use guarantee: the key is claimed by exactly the winner,
    # never both. A lost conditional-claim race would let the second redeemer also
    # commit (double redemption); instead the key ends up redeemed by the winner.
    assert _run(_key_redeemed_by(redeem_code)) == winner

    # Both redeemers were answered: one granted access, the other told the key was
    # already taken at that instant.
    assert any("Access granted" in t for t in recorder.texts)
    assert any("Another user just redeemed that key first" in t for t in recorder.texts)


# ─── the redeem-vs-revoke race: one row, exactly one consistent outcome ────────
# All prior tests race two writers for the *same resource* (owner cap or single-
# use claim) through the redeem / promote path. A separate, live-unproven race is
# a *redeem* colliding directly with an *admin revoke* of the same key at the same
# instant: one path issues the conditional claim
#
#   UPDATE access_keys SET redeemed_by_tg_uid = %s
#   WHERE id = %s AND revoked = false AND redeemed_by_tg_uid IS NULL
#   RETURNING id
#
# the other issues a plain revoke
#
#   UPDATE access_keys SET revoked = true WHERE code = %s
#
# Both UPDATEs target the same row. Postgres row-level locking ensures exactly
# one of them wins the row lock; the winner commits first and the loser sees the
# committed state.
#
# Two and only two consistent outcomes are possible:
#
#   • Redeem wins: redeemed_by_tg_uid is set to the redeemer's uid. The users +
#     role_events rows commit atomically inside the same transaction. The revoke
#     UPDATE (which runs afterward, outside any redeem transaction guard) may
#     still set revoked = true on the already-redeemed key — but the account was
#     created atomically and is intact.
#
#   • Revoke wins: revoked = true is committed first. The redeem's conditional
#     UPDATE finds no row matching ``revoked = false``, so RETURNING returns
#     nothing → ``__lost_race__`` → the whole redeem transaction rolls back.
#     No users row, no role_events orphan, redeemed_by stays NULL.
#
# No partial / inconsistent state is possible: there is no world where an account
# is created but the key stays unclaimed, or the key is claimed but no account
# commits. Today this is only covered by in-memory fakes; this test proves the
# live cross-connection guarantee against a real Postgres.
#
# A ``user`` role key is used deliberately: it sidesteps the owner-cap branch
# entirely so the only contention is the conditional claim row lock.
def test_concurrent_redeem_vs_revoke_exactly_one_consistent_outcome(monkeypatch, real_db):
    # Seed one unredeemed non-owner key — the sole contested resource.
    redeem_code = _run(_seed_user_key(ACTOR_A_TG_UID))
    assert _run(_key_redeemed_by(redeem_code)) is None

    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)

    # Neutralise the redeem path's side requirements so the only contention
    # left is the conditional claim: channel gate always satisfied, no
    # saturation-alert side effects, and a fixed candidate user_id (no
    # user_id uniqueness can decide the race — only the row lock can).
    async def _members_ok(_uid):
        return {"all": [], "missing": []}

    monkeypatch.setattr(bot3, "check_channel_membership", _members_ok)

    _new_user_id = f"itest_rv_{uuid.uuid4().hex[:10]}"

    async def _uid():
        return _new_user_id

    monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

    # Hold both threads until the redeem path has finished its
    # ``get_user_by_tg_uid`` pre-check (sees None) AND the revoke thread is
    # ready. Neither opens its transaction / issues its UPDATE until both have
    # cleared the barrier, so they truly collide on the row lock.
    barrier = threading.Barrier(2)
    real_get = db.get_user_by_tg_uid

    async def _gated_get(uid: int):
        result = await real_get(uid)
        if uid == TARGET_A_TG_UID:
            barrier.wait(timeout=15)  # cross-thread rendezvous
        return result

    monkeypatch.setattr(bot3.db, "get_user_by_tg_uid", _gated_get)

    # Writer 1: redeem the key as brand-new target A.
    def _redeem() -> None:
        state = {"name": bot3.placeholder_name(TARGET_A_TG_UID), "tg_username": "redeemer"}
        _run(bot3.handle_key_submission(object(), _Msg(TARGET_A_TG_UID), redeem_code, state))

    # Writer 2: revoke the same key via the same SQL the bot3 revoke command
    # uses — a plain unconditional UPDATE with no transaction guard.
    async def _do_revoke() -> None:
        barrier.wait(timeout=15)  # cross-thread rendezvous
        await db.execute(
            "UPDATE access_keys SET revoked = true WHERE code = %s", (redeem_code,)
        )

    def _revoke() -> None:
        _run(_do_revoke())

    t_redeem = threading.Thread(target=_redeem)
    t_revoke = threading.Thread(target=_revoke)
    t_redeem.start()
    t_revoke.start()
    t_redeem.join(timeout=30)
    t_revoke.join(timeout=30)
    assert not t_redeem.is_alive() and not t_revoke.is_alive(), "a writer thread hung"

    # Inspect the committed state of the key and the redeemer's account.
    key_row = _run(db.get_access_key_by_code(redeem_code))
    assert key_row is not None, "the seeded key disappeared unexpectedly"
    redeemed_by = key_row.get("redeemed_by_tg_uid")
    revoked = bool(key_row.get("revoked"))

    user_role = _run(_user_role(TARGET_A_TG_UID))
    events = _run(_events_for(TARGET_A_TG_UID))

    if redeemed_by == TARGET_A_TG_UID:
        # ── Redeem won ────────────────────────────────────────────────────────
        # The account was created atomically with the key claim.
        assert user_role == "user", (
            f"redeem won (key redeemed_by={redeemed_by}) but users row has "
            f"role={user_role!r} instead of 'user'"
        )
        assert events == 1, (
            f"redeem won but expected 1 role_events row, got {events}"
        )
        # The redeemer received an access-granted confirmation.
        assert any("Access granted" in t for t in recorder.texts), (
            f"redeem won but no 'Access granted' message; got: {recorder.texts}"
        )
    else:
        # ── Revoke won ────────────────────────────────────────────────────────
        # The conditional claim found revoked = true (or a concurrent revoke
        # beat it) so RETURNING returned nothing → __lost_race__ → full rollback.
        assert redeemed_by is None, (
            f"expected redeemed_by=NULL after revoke win, got {redeemed_by}"
        )
        assert revoked, (
            "revoke-wins branch: key should have revoked=true committed"
        )
        # No orphan rows from the rolled-back redeem transaction.
        assert user_role is None, (
            f"revoke won but a users row (role={user_role!r}) was left as orphan"
        )
        assert events == 0, (
            f"revoke won but {events} orphan role_events row(s) survived rollback"
        )
        # The redeemer was turned away with the revoked message: either the
        # early-exit at the top of handle_key_submission caught it (stale read
        # still saw revoked=false but post-rollback SELECT confirms revoked=true),
        # or the post-__lost_race__ SELECT found revoked=true and sent the precise
        # "was revoked" message.  "Another user" is NOT acceptable here.
        assert any("revoked" in t.lower() for t in recorder.texts), (
            f"revoke won but no 'revoked' refusal message sent to redeemer; got: {recorder.texts}"
        )

    # Cross-check: the two outcomes are mutually exclusive — there must never be
    # a world where an account was created but the key is left unclaimed, or
    # the key was claimed but no account committed (the transaction is atomic).
    account_without_claim = (redeemed_by is None) and (user_role is not None)
    claim_without_account = (redeemed_by == TARGET_A_TG_UID) and (user_role is None)
    assert not account_without_claim, (
        "partial state: users row committed but key redeemed_by stayed NULL"
    )
    assert not claim_without_account, (
        "partial state: key claimed but users row not committed (torn transaction)"
    )


# ─── stale-read window: revoke injected between the read and the tx ───────────
# ``handle_key_submission`` reads the key's ``revoked`` flag with
# ``get_access_key_by_code`` *outside* any transaction (a plain SELECT, no lock).
# If an admin revokes the key between that stale read and the conditional UPDATE
# inside the transaction, the stale read still sees ``revoked = false`` — the
# early-exit at the top of the function is bypassed — but the conditional UPDATE:
#
#   UPDATE access_keys SET redeemed_by_tg_uid = %s
#   WHERE id = %s AND revoked = false AND redeemed_by_tg_uid IS NULL
#   RETURNING id
#
# has ``AND revoked = false`` in its WHERE clause, so it matches no row,
# RETURNING returns nothing, ``__lost_race__`` is raised, and the whole
# transaction rolls back.  No account is created, no orphan rows leak, and
# after the rollback a post-rollback SELECT finds ``revoked = true`` and sends
# the precise "❌ This access key was revoked." message to the redeemer.
#
# This test pins that guarantee *deterministically* by injecting the revoke
# synchronously inside a wrapper around ``get_access_key_by_code``: after the
# real read returns (still sees ``revoked = false``), the wrapper immediately
# revokes the key in the database, then returns the stale dict to
# ``handle_key_submission``.  The caller sees a pristine-looking key; the
# database has already committed ``revoked = true`` before the transaction even
# opens.  No scheduling luck or thread synchronisation is needed.
#
# A ``user`` role key is used deliberately — it sidesteps the owner-cap branch
# entirely so the only thing under test is the conditional claim's
# ``revoked = false`` guard.
def test_revoke_in_stale_read_window_blocks_redemption(monkeypatch, real_db):
    # Seed one unredeemed non-owner key — the sole resource under test.
    redeem_code = _run(_seed_user_key(ACTOR_A_TG_UID))
    assert _run(_key_redeemed_by(redeem_code)) is None

    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)

    # Neutralise the redeem path's side requirements so the only thing under
    # test is the conditional claim's revoked guard: channel gate always
    # satisfied and no saturation-alert side effects.
    async def _members_ok(_uid):
        return {"all": [], "missing": []}

    monkeypatch.setattr(bot3, "check_channel_membership", _members_ok)

    _new_user_id = f"itest_sw_{uuid.uuid4().hex[:10]}"

    async def _uid():
        return _new_user_id

    monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

    # Wrap ``get_access_key_by_code`` to simulate the stale-read window: let
    # the real SELECT complete (it returns ``revoked = false``), then
    # immediately commit a revoke to the database before returning the stale
    # dict to ``handle_key_submission``.  The caller sees a pristine key; the
    # database already has ``revoked = true`` before the transaction opens.
    real_get_key = db.get_access_key_by_code

    async def _get_key_then_revoke(code: str):
        stale = await real_get_key(code)
        if stale is not None:
            await db.execute(
                "UPDATE access_keys SET revoked = true WHERE code = %s", (code,)
            )
        return stale

    monkeypatch.setattr(bot3.db, "get_access_key_by_code", _get_key_then_revoke)

    state = {"name": bot3.placeholder_name(TARGET_A_TG_UID), "tg_username": "redeemer"}
    _run(bot3.handle_key_submission(object(), _Msg(TARGET_A_TG_UID), redeem_code, state))

    # No account must have been created: the conditional UPDATE's
    # ``AND revoked = false`` clause matched nothing, so ``__lost_race__`` was
    # raised and the whole transaction rolled back.
    assert _run(_user_role(TARGET_A_TG_UID)) is None, (
        "a users row was created despite the key being revoked in the stale-read window"
    )
    assert _run(_events_for(TARGET_A_TG_UID)) == 0, (
        "an orphan role_events row survived rollback"
    )

    # The key must be revoked and unclaimed: the conditional claim found no
    # matching row, so ``redeemed_by_tg_uid`` stays NULL.
    key_row = _run(db.get_access_key_by_code(redeem_code))
    assert key_row is not None, "the seeded key disappeared unexpectedly"
    assert key_row.get("revoked"), "key should be revoked after the injected revoke"
    assert key_row.get("redeemed_by_tg_uid") is None, (
        "key was claimed despite the revoke being injected in the stale-read window"
    )

    # The redeemer must have been turned away with the precise "revoked" message.
    # After the __lost_race__ rollback, handle_key_submission does a post-rollback
    # SELECT; it finds revoked=true and sends "❌ This access key was revoked."
    # rather than the generic "Another user just redeemed that key first."
    assert any("revoked" in t.lower() for t in recorder.texts), (
        f"redeemer received no 'revoked' refusal message after stale-read-window revoke; "
        f"got: {recorder.texts}"
    )


# ─── the loser branch, pinned deterministically (no scheduling luck) ──────────
# The global owner count is seeded right up to the cap, then a single owner
# promotion of a brand-new target runs. The in-transaction count must see the
# cap is full and roll the whole unit of work back: no users row, no orphan
# role_events row, and the global owner count unchanged.
async def test_owner_cap_refusal_rolls_back_without_orphan(monkeypatch):
    if not await _db_reachable():
        pytest.skip("no real Postgres with the dashboard schema is available")
    await _cleanup()
    recorder = _Recorder()
    monkeypatch.setattr(bot3, "send", recorder.send)
    try:
        if not await _seed_owners_to(MAX_OWNERS):
            pytest.skip(
                "database already holds more than MAX_OWNERS owners; cannot pin "
                "the at-cap state without mutating real data"
            )
        assert await _global_owner_count() == MAX_OWNERS

        new_user_id = f"itest_own_{uuid.uuid4().hex[:10]}"

        async def _uid():
            return new_user_id

        monkeypatch.setattr(bot3, "_generate_unique_user_id", _uid)

        actor = {"tg_uid": ACTOR_A_TG_UID, "role": "management"}
        await bot3.apply_promotion(object(), actor, TARGET_A_TG_UID, "owner")

        # The promotion was refused: no users row was created for the target …
        assert await _user_role(TARGET_A_TG_UID) is None
        # … and no orphan audit row leaked from the same rolled-back transaction.
        assert await _events_for(TARGET_A_TG_UID) == 0
        # The global owner count is untouched — still exactly at the cap.
        assert await _global_owner_count() == MAX_OWNERS

        # The requester is told the cap is full, naming the cap.
        refusal = recorder.texts[-1]
        assert "Owner cap reached" in refusal
        assert str(MAX_OWNERS) in refusal
    finally:
        await _cleanup()
