"""Tests for the Panel Bot (bot5) recurring 'connect your section channel' reminder.

The one-time live announcement (covered separately) only fires once per
deployment, so a section-owning admin who ignores it is never nudged again. This
recurring loop closes that gap, and it carries the same subtle rules that are
otherwise only verifiable in production:

  * recipients are the same eligible set as the announcement (management / owner
    / dev_admin + the hardcoded management UID), but only those who still have NO
    panel_sections row are DMed — already-connected admins are skipped;
  * a per-admin dedup window persisted in app_settings means a given admin is
    pinged at most once per period, even across passes / restarts;
  * a DB hiccup reading get_panel_section must NOT cause a double-ping — the prior
    marker is carried forward and the admin is retried next pass;
  * markers for admins who have since lost their section role are pruned so the
    persisted map stays bounded;
  * a single failing DM never aborts the pass, and the attempt is still marked so
    an unreachable admin is retried at most once per period, not every scan.

These tests exercise the real functions in ``bot5`` with the external surface
(database access + Telegram send + the wall clock) replaced by in-memory fakes.
"""
from __future__ import annotations

import json

from unittest.mock import AsyncMock

import pytest

import bot5


class _StopLoop(Exception):
    """Sentinel raised from the patched ``asyncio.sleep`` to break out of
    ``_reminder_loop``'s otherwise-infinite ``while True``. It is raised from a
    sleep (which the loop never wraps in try/except), so it propagates cleanly
    out of the loop instead of being mistaken for a failed pass."""


class FakeSleep:
    """Records every ``asyncio.sleep`` delay the loop requests and short-circuits
    the wall clock. After ``stop_after`` sleeps it raises ``_StopLoop`` so the
    loop terminates deterministically instead of running forever."""

    def __init__(self, stop_after: int) -> None:
        self.stop_after = stop_after
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.stop_after:
            raise _StopLoop


# ─── fakes ──────────────────────────────────────────────────────────────────
class FakeSettings:
    """In-memory stand-in for db.get_setting / db.set_setting.

    The reminder dedup state lives here as a JSON string under
    ``_REMINDER_SETTING_KEY``.
    """

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(initial or {})
        self.set_calls: list[tuple[str, str]] = []

    async def get_setting(self, key: str):
        return self.store.get(key)

    async def set_setting(self, key: str, value: str) -> None:
        self.set_calls.append((key, value))
        self.store[key] = value


class FakeSender:
    """Stand-in for sender.send. Records every attempt; returns None (a delivery
    failure, exactly as the real send() does when a user never opened the bot or
    blocked it) for any uid in ``fail_uids``."""

    def __init__(self, fail_uids: set[int] | None = None) -> None:
        self.fail_uids = set(fail_uids or ())
        self.calls: list[tuple[int, str]] = []
        self.sent: list[int] = []

    async def send(self, app, uid, text):
        self.calls.append((uid, text))
        if uid in self.fail_uids:
            return None
        self.sent.append(uid)
        return object()  # a truthy "Message"

    @property
    def recipients(self) -> list[int]:
        return [uid for uid, _ in self.calls]


class FakeSections:
    """Stand-in for db.get_panel_section.

    ``connected`` maps tg_uid -> a truthy section row; any uid in ``fail_uids``
    raises, exactly like a transient DB error on the real query.
    """

    def __init__(
        self,
        connected: dict[int, object] | None = None,
        fail_uids: set[int] | None = None,
    ) -> None:
        self.connected = dict(connected or {})
        self.fail_uids = set(fail_uids or ())
        self.calls: list[int] = []

    async def get_panel_section(self, uid: int):
        self.calls.append(uid)
        if uid in self.fail_uids:
            raise RuntimeError("db hiccup")
        return self.connected.get(uid)


class FakeClock:
    """Controllable stand-in for the ``time`` module used by bot5."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


def _saved_state(settings: FakeSettings) -> dict[str, float]:
    """Decode the dedup map bot5 last persisted (empty if it never saved)."""
    raw = settings.store.get(bot5._REMINDER_SETTING_KEY)
    return json.loads(raw) if raw else {}


# ─── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def settings(monkeypatch):
    s = FakeSettings()
    monkeypatch.setattr(bot5.db, "get_setting", s.get_setting)
    monkeypatch.setattr(bot5.db, "set_setting", s.set_setting)
    return s


@pytest.fixture
def sender(monkeypatch):
    snd = FakeSender()
    monkeypatch.setattr(bot5, "send", snd.send)
    return snd


@pytest.fixture
def clock(monkeypatch):
    clk = FakeClock()
    monkeypatch.setattr(bot5, "time", clk)
    return clk


def _patch_sections(monkeypatch, sections: FakeSections) -> FakeSections:
    monkeypatch.setattr(bot5.db, "get_panel_section", sections.get_panel_section)
    return sections


def _patch_recipients(monkeypatch, uids: list[int]) -> None:
    monkeypatch.setattr(bot5, "_announce_recipients", AsyncMock(return_value=uids))


# ─── eligibility: only un-connected admins are reminded ──────────────────────
async def test_only_unconnected_eligible_admins_are_dmed(
    monkeypatch, settings, sender, clock
):
    _patch_recipients(monkeypatch, [1, 2, 3])
    # uid 2 already linked a channel → never reminded.
    _patch_sections(monkeypatch, FakeSections(connected={2: {"chat_id": -100}}))

    await bot5._send_section_reminders(object())

    assert sender.recipients == [1, 3]
    assert 2 not in sender.recipients
    assert _saved_state(settings) == {"1": clock.now, "3": clock.now}


async def test_connected_admin_marker_is_dropped(monkeypatch, settings, sender, clock):
    # uid 5 has a stale marker from a past pass but has since connected a channel.
    settings.store[bot5._REMINDER_SETTING_KEY] = json.dumps({"5": clock.now - 10})
    _patch_recipients(monkeypatch, [5])
    _patch_sections(monkeypatch, FakeSections(connected={5: {"chat_id": -100}}))

    await bot5._send_section_reminders(object())

    assert sender.calls == []  # connected → not pinged
    assert _saved_state(settings) == {}  # stale marker pruned


# ─── per-admin once-per-period dedup, across passes ──────────────────────────
async def test_dedup_window_honoured_across_passes(
    monkeypatch, settings, sender, clock
):
    _patch_recipients(monkeypatch, [7])
    _patch_sections(monkeypatch, FakeSections())  # never connects

    # Pass 1 at t0: first contact → ping, marker written.
    clock.now = 1_000.0
    await bot5._send_section_reminders(object())
    assert sender.recipients == [7]
    assert _saved_state(settings) == {"7": 1_000.0}

    # Pass 2, still inside the period → marker kept, no second ping.
    clock.now = 1_000.0 + bot5._REMINDER_PERIOD_SECONDS - 1
    await bot5._send_section_reminders(object())
    assert sender.recipients == [7]  # unchanged
    assert _saved_state(settings) == {"7": 1_000.0}  # original marker preserved

    # Pass 3, period has elapsed → ping again, marker refreshed to new now.
    clock.now = 1_000.0 + bot5._REMINDER_PERIOD_SECONDS + 1
    await bot5._send_section_reminders(object())
    assert sender.recipients == [7, 7]
    assert _saved_state(settings) == {"7": clock.now}


# ─── DB hiccup on get_panel_section must not double-ping ──────────────────────
async def test_db_hiccup_carries_prior_marker_forward(
    monkeypatch, settings, sender, clock
):
    # uid 8 was pinged recently; this pass the section lookup errors out.
    settings.store[bot5._REMINDER_SETTING_KEY] = json.dumps({"8": 500.0})
    _patch_recipients(monkeypatch, [8])
    _patch_sections(monkeypatch, FakeSections(fail_uids={8}))

    await bot5._send_section_reminders(object())

    assert sender.calls == []  # never guess on a failed lookup
    assert _saved_state(settings) == {"8": 500.0}  # prior marker carried forward


async def test_db_hiccup_without_prior_marker_leaves_admin_untracked(
    monkeypatch, settings, sender, clock
):
    _patch_recipients(monkeypatch, [9])
    _patch_sections(monkeypatch, FakeSections(fail_uids={9}))

    await bot5._send_section_reminders(object())

    assert sender.calls == []
    # No bogus marker invented → admin is reconsidered on the next pass.
    assert _saved_state(settings) == {}


# ─── markers for admins who lost their section role are pruned ────────────────
async def test_markers_for_departed_admins_are_pruned(
    monkeypatch, settings, sender, clock
):
    # uid 100 is no longer in the eligible recipient set; uid 7 still is.
    settings.store[bot5._REMINDER_SETTING_KEY] = json.dumps(
        {"100": clock.now - 5, "7": clock.now - 5}
    )
    _patch_recipients(monkeypatch, [7])
    _patch_sections(monkeypatch, FakeSections())

    await bot5._send_section_reminders(object())

    saved = _saved_state(settings)
    assert "100" not in saved  # departed admin's marker dropped
    assert "7" in saved  # still-eligible admin retained


# ─── one failing DM never aborts the pass ────────────────────────────────────
async def test_failing_dm_does_not_abort_pass_and_marks_attempt(
    monkeypatch, settings, clock
):
    failing = FakeSender(fail_uids={2})
    monkeypatch.setattr(bot5, "send", failing.send)
    _patch_recipients(monkeypatch, [1, 2, 3])
    _patch_sections(monkeypatch, FakeSections())

    await bot5._send_section_reminders(object())

    assert failing.recipients == [1, 2, 3]  # all attempted, even after #2 failed
    assert failing.sent == [1, 3]  # #2's delivery failed
    # The failed attempt is still marked so #2 is retried at most once per period.
    assert _saved_state(settings) == {
        "1": clock.now,
        "2": clock.now,
        "3": clock.now,
    }


# ─── recipient resolution failure skips the whole pass ───────────────────────
async def test_recipient_resolution_failure_skips_pass(
    monkeypatch, settings, sender, clock
):
    settings.store[bot5._REMINDER_SETTING_KEY] = json.dumps({"7": 500.0})
    monkeypatch.setattr(
        bot5, "_announce_recipients", AsyncMock(side_effect=RuntimeError("db down"))
    )
    # If the pass were not skipped this would record a call.
    _patch_sections(monkeypatch, FakeSections())

    await bot5._send_section_reminders(object())

    assert sender.calls == []
    assert settings.set_calls == []  # state left exactly as it was


# ─── load/save dedup-state resilience ────────────────────────────────────────
async def test_load_state_returns_empty_on_db_error(monkeypatch):
    async def _boom(_key):
        raise RuntimeError("db down")

    monkeypatch.setattr(bot5.db, "get_setting", _boom)

    assert await bot5._load_reminder_state() == {}


async def test_load_state_returns_empty_on_invalid_json(monkeypatch):
    async def _bad(_key):
        return "not json{"

    monkeypatch.setattr(bot5.db, "get_setting", _bad)

    assert await bot5._load_reminder_state() == {}


async def test_load_state_coerces_values_and_skips_bad_entries(monkeypatch):
    async def _mixed(_key):
        return json.dumps({"1": 100, "2": "200.5", "3": "oops", "4": None})

    monkeypatch.setattr(bot5.db, "get_setting", _mixed)

    state = await bot5._load_reminder_state()

    assert state == {"1": 100.0, "2": 200.5}  # bad/uncoercible entries dropped


async def test_load_state_ignores_non_dict_payload(monkeypatch):
    async def _list(_key):
        return json.dumps([1, 2, 3])

    monkeypatch.setattr(bot5.db, "get_setting", _list)

    assert await bot5._load_reminder_state() == {}


async def test_save_state_swallows_db_error(monkeypatch):
    async def _boom(_key, _value):
        raise RuntimeError("db down")

    monkeypatch.setattr(bot5.db, "set_setting", _boom)

    # Must not raise — a failed persist is logged, never propagated.
    await bot5._save_reminder_state({"1": 123.0})


# ─── the scheduler loop: startup delay + interval timing ─────────────────────
async def test_reminder_loop_waits_startup_then_interval_between_passes(monkeypatch):
    """The loop must sleep ``_REMINDER_STARTUP_DELAY`` BEFORE the first pass, run
    a pass, then sleep ``_REMINDER_CHECK_INTERVAL`` between each subsequent pass."""
    events: list = []

    async def _pass(_app):
        events.append("pass")

    sleeper = FakeSleep(stop_after=3)

    async def _record_sleep(delay):
        events.append(("sleep", delay))
        await sleeper(delay)

    monkeypatch.setattr(bot5, "_send_section_reminders", _pass)
    monkeypatch.setattr(bot5.asyncio, "sleep", _record_sleep)

    with pytest.raises(_StopLoop):
        await bot5._reminder_loop(object())

    # The startup delay is the very first thing the loop does — before any pass.
    assert events[0] == ("sleep", bot5._REMINDER_STARTUP_DELAY)
    # Two full passes ran, each separated by the fixed re-scan interval.
    assert events == [
        ("sleep", bot5._REMINDER_STARTUP_DELAY),
        "pass",
        ("sleep", bot5._REMINDER_CHECK_INTERVAL),
        "pass",
        ("sleep", bot5._REMINDER_CHECK_INTERVAL),
    ]


async def test_reminder_loop_runs_first_pass_only_after_startup_delay(monkeypatch):
    """A more surgical guard on ordering: the first pass never runs before the
    startup delay has elapsed."""
    passes = AsyncMock()
    sleeper = FakeSleep(stop_after=1)  # raise on the very first (startup) sleep

    monkeypatch.setattr(bot5, "_send_section_reminders", passes)
    monkeypatch.setattr(bot5.asyncio, "sleep", sleeper)

    with pytest.raises(_StopLoop):
        await bot5._reminder_loop(object())

    assert sleeper.delays == [bot5._REMINDER_STARTUP_DELAY]
    passes.assert_not_called()  # loop stopped during startup wait, before pass 1


# ─── the scheduler loop: a failing pass must not kill the loop ───────────────
async def test_reminder_loop_survives_a_raising_pass(monkeypatch):
    """One pass raising must NOT abort the loop — the next scheduled pass still
    runs. A regression here would silently stop all future reminders."""
    passes = AsyncMock(side_effect=[RuntimeError("pass blew up"), None])
    sleeper = FakeSleep(stop_after=3)

    monkeypatch.setattr(bot5, "_send_section_reminders", passes)
    monkeypatch.setattr(bot5.asyncio, "sleep", sleeper)

    with pytest.raises(_StopLoop):
        await bot5._reminder_loop(object())

    # Pass 1 raised (and was swallowed); the loop kept going and ran pass 2.
    assert passes.call_count == 2
    # The interval sleep still happened after the failing pass, so the schedule
    # is preserved rather than the loop tightening into a hot retry.
    assert sleeper.delays == [
        bot5._REMINDER_STARTUP_DELAY,
        bot5._REMINDER_CHECK_INTERVAL,
        bot5._REMINDER_CHECK_INTERVAL,
    ]


async def test_reminder_loop_keeps_running_after_consecutive_failures(monkeypatch):
    """Even back-to-back failing passes don't break the loop; it just keeps
    re-scheduling on the same interval."""
    passes = AsyncMock(side_effect=RuntimeError("always fails"))
    sleeper = FakeSleep(stop_after=4)

    monkeypatch.setattr(bot5, "_send_section_reminders", passes)
    monkeypatch.setattr(bot5.asyncio, "sleep", sleeper)

    with pytest.raises(_StopLoop):
        await bot5._reminder_loop(object())

    # startup + 3 interval sleeps → 3 passes attempted, all raised, none aborted.
    assert passes.call_count == 3
    assert sleeper.delays == [
        bot5._REMINDER_STARTUP_DELAY,
        bot5._REMINDER_CHECK_INTERVAL,
        bot5._REMINDER_CHECK_INTERVAL,
        bot5._REMINDER_CHECK_INTERVAL,
    ]


# ─── on_started wiring: deployment-gated background scheduling ────────────────
async def test_on_started_skips_everything_outside_deployment(monkeypatch):
    """In dev (not a deployment) neither the reminder loop nor the live
    announcement run — a restart must never spam real admins."""
    monkeypatch.setattr(bot5, "IS_DEPLOYMENT", False)
    created = []
    monkeypatch.setattr(bot5.asyncio, "create_task", lambda coro: created.append(coro))
    announce = AsyncMock()
    monkeypatch.setattr(bot5, "_run_live_announcement", announce)

    await bot5.on_started(object())

    assert created == []  # no reminder loop scheduled
    announce.assert_not_called()


async def test_on_started_schedules_reminder_loop_in_deployment(monkeypatch):
    """In a deployment the recurring reminder loop is scheduled as a background
    task and the one-time live announcement is awaited."""
    monkeypatch.setattr(bot5, "IS_DEPLOYMENT", True)

    scheduled = []

    def _fake_create_task(coro):
        scheduled.append(coro)
        coro.close()  # we only assert it was scheduled; don't actually run it
        return object()

    monkeypatch.setattr(bot5.asyncio, "create_task", _fake_create_task)
    announce = AsyncMock()
    monkeypatch.setattr(bot5, "_run_live_announcement", announce)

    await bot5.on_started(object())

    assert len(scheduled) == 1  # the reminder loop was scheduled
    announce.assert_awaited_once()
