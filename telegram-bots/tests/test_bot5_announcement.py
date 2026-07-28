"""Tests for the Panel Bot (bot5) one-time live announcement.

The live announcement has subtle correctness rules that are otherwise only
verifiable in production:

  * recipients are the section-owning roles (management / owner / dev_admin)
    plus the hardcoded management UID; base_admin / user are never notified, and
    the set is deduped;
  * it is sent once per deployment — a restart with the same DEPLOYMENT_ID does
    not re-send, a fresh deployment with a new DEPLOYMENT_ID does;
  * it only runs inside a deployment (the IS_DEPLOYMENT gate in on_started);
  * a single failing DM never aborts the pass or blocks the dedup marker write.

These tests exercise the real functions in ``bot5`` with the external surface
(database access + Telegram send) replaced by in-memory fakes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import bot5


# ─── fakes ──────────────────────────────────────────────────────────────────
class FakeSettings:
    """In-memory stand-in for db.get_setting / db.set_setting."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(initial or {})
        self.get_calls = 0
        self.set_calls: list[tuple[str, str]] = []

    async def get_setting(self, key: str):
        self.get_calls += 1
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


def _fake_list_users_by_roles(rows: list[dict]):
    """Build a fake db.list_users_by_roles that honours role filtering, exactly
    like the real SQL ``WHERE role IN (...)`` query does."""

    async def _impl(roles):
        return [r for r in rows if r.get("role") in roles]

    return _impl


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


# ─── recipient selection ─────────────────────────────────────────────────────
async def test_recipients_include_section_roles_and_hardcoded_management(monkeypatch):
    rows = [
        {"tg_uid": 10, "role": "management"},
        {"tg_uid": 20, "role": "owner"},
        {"tg_uid": 30, "role": "dev_admin"},
        {"tg_uid": 40, "role": "base_admin"},  # excluded
        {"tg_uid": 50, "role": "user"},  # excluded
    ]
    monkeypatch.setattr(bot5.db, "list_users_by_roles", _fake_list_users_by_roles(rows))
    monkeypatch.setattr(bot5, "HARDCODED_MANAGEMENT_ID", 99)

    result = await bot5._announce_recipients()

    assert result == [10, 20, 30, 99]  # sorted, hardcoded management included
    assert 40 not in result  # base_admin never notified
    assert 50 not in result  # user never notified


async def test_recipients_are_deduped(monkeypatch):
    rows = [
        {"tg_uid": 99, "role": "management"},  # collides with hardcoded id
        {"tg_uid": 20, "role": "owner"},
        {"tg_uid": 20, "role": "owner"},  # duplicate row
        {"tg_uid": None, "role": "dev_admin"},  # missing uid is skipped
    ]
    monkeypatch.setattr(bot5.db, "list_users_by_roles", _fake_list_users_by_roles(rows))
    monkeypatch.setattr(bot5, "HARDCODED_MANAGEMENT_ID", 99)

    result = await bot5._announce_recipients()

    assert result == [20, 99]


async def test_recipients_request_only_section_roles(monkeypatch):
    captured: dict[str, tuple] = {}

    async def _spy(roles):
        captured["roles"] = roles
        return []

    monkeypatch.setattr(bot5.db, "list_users_by_roles", _spy)
    monkeypatch.setattr(bot5, "HARDCODED_MANAGEMENT_ID", 1)

    await bot5._announce_recipients()

    assert set(captured["roles"]) == {"management", "owner", "dev_admin"}
    assert "base_admin" not in captured["roles"]
    assert "user" not in captured["roles"]


# ─── per-deployment dedup ────────────────────────────────────────────────────
async def test_announcement_sends_and_writes_marker(monkeypatch, settings, sender):
    monkeypatch.setattr(bot5, "DEPLOYMENT_ID", "deploy-A")
    monkeypatch.setattr(bot5, "_announce_recipients", AsyncMock(return_value=[1, 2, 3]))

    await bot5._run_live_announcement(object())

    assert sender.sent == [1, 2, 3]
    assert settings.store[bot5._ANNOUNCE_SETTING_KEY] == "deploy-A"


async def test_same_deployment_does_not_resend(monkeypatch, sender):
    settings = FakeSettings({bot5._ANNOUNCE_SETTING_KEY: "deploy-A"})
    monkeypatch.setattr(bot5.db, "get_setting", settings.get_setting)
    monkeypatch.setattr(bot5.db, "set_setting", settings.set_setting)
    monkeypatch.setattr(bot5, "DEPLOYMENT_ID", "deploy-A")
    monkeypatch.setattr(bot5, "_announce_recipients", AsyncMock(return_value=[1, 2, 3]))

    await bot5._run_live_announcement(object())

    assert sender.calls == []  # nobody was DMed
    assert settings.set_calls == []  # marker left untouched


async def test_new_deployment_resends(monkeypatch, settings, sender):
    recipients = AsyncMock(return_value=[1, 2])
    monkeypatch.setattr(bot5, "_announce_recipients", recipients)

    # First deployment: announces.
    monkeypatch.setattr(bot5, "DEPLOYMENT_ID", "deploy-A")
    await bot5._run_live_announcement(object())
    assert len(sender.calls) == 2
    assert settings.store[bot5._ANNOUNCE_SETTING_KEY] == "deploy-A"

    # Restart of the same deployment: skipped.
    await bot5._run_live_announcement(object())
    assert len(sender.calls) == 2

    # Fresh deployment with a new id: announces again.
    monkeypatch.setattr(bot5, "DEPLOYMENT_ID", "deploy-B")
    await bot5._run_live_announcement(object())
    assert len(sender.calls) == 4
    assert settings.store[bot5._ANNOUNCE_SETTING_KEY] == "deploy-B"


async def test_announcement_aborts_without_marker_when_marker_read_fails(
    monkeypatch, sender
):
    async def _boom(_key):
        raise RuntimeError("db down")

    set_calls: list = []

    async def _set(key, value):
        set_calls.append((key, value))

    monkeypatch.setattr(bot5.db, "get_setting", _boom)
    monkeypatch.setattr(bot5.db, "set_setting", _set)
    monkeypatch.setattr(bot5, "DEPLOYMENT_ID", "deploy-A")
    monkeypatch.setattr(bot5, "_announce_recipients", AsyncMock(return_value=[1]))

    await bot5._run_live_announcement(object())

    assert sender.calls == []  # never sent on a failed dedup read
    assert set_calls == []  # never wrote a bogus marker


async def test_marker_write_failure_is_swallowed(monkeypatch, sender):
    """A failure persisting the dedup marker must be swallowed (logged), never
    propagated — otherwise a transient DB write error after the DMs went out
    would crash startup."""
    settings = FakeSettings()  # marker read returns None → not yet announced

    async def _boom(_key, _value):
        raise RuntimeError("db down")

    monkeypatch.setattr(bot5.db, "get_setting", settings.get_setting)
    monkeypatch.setattr(bot5.db, "set_setting", _boom)
    monkeypatch.setattr(bot5, "DEPLOYMENT_ID", "deploy-A")
    monkeypatch.setattr(bot5, "_announce_recipients", AsyncMock(return_value=[1, 2]))

    # Must complete without raising even though persisting the marker failed.
    await bot5._run_live_announcement(object())

    assert sender.sent == [1, 2]  # DMs still went out before the failed write


# ─── per-recipient failure tolerance ─────────────────────────────────────────
async def test_failing_dm_does_not_abort_pass_or_block_marker(monkeypatch, settings):
    failing = FakeSender(fail_uids={2})
    monkeypatch.setattr(bot5, "send", failing.send)
    monkeypatch.setattr(bot5, "DEPLOYMENT_ID", "deploy-A")
    monkeypatch.setattr(bot5, "_announce_recipients", AsyncMock(return_value=[1, 2, 3]))

    await bot5._run_live_announcement(object())

    assert failing.recipients == [1, 2, 3]  # all attempted, even after #2 failed
    assert failing.sent == [1, 3]  # #2's delivery failed
    assert settings.store[bot5._ANNOUNCE_SETTING_KEY] == "deploy-A"  # marker written


# ─── deployment gate (on_started) ────────────────────────────────────────────
async def test_on_started_skips_when_not_deployment(monkeypatch):
    announce = AsyncMock()
    monkeypatch.setattr(bot5, "_run_live_announcement", announce)

    reminder_started = {"value": False}

    async def _fake_loop(_app):
        reminder_started["value"] = True

    monkeypatch.setattr(bot5, "_reminder_loop", _fake_loop)
    monkeypatch.setattr(bot5, "IS_DEPLOYMENT", False)

    await bot5.on_started(object())

    announce.assert_not_awaited()
    assert reminder_started["value"] is False  # reminder loop not scheduled


async def test_on_started_runs_when_deployment(monkeypatch):
    import asyncio

    announce = AsyncMock()
    monkeypatch.setattr(bot5, "_run_live_announcement", announce)

    reminder_started = {"value": False}

    async def _fake_loop(_app):
        reminder_started["value"] = True

    monkeypatch.setattr(bot5, "_reminder_loop", _fake_loop)
    monkeypatch.setattr(bot5, "IS_DEPLOYMENT", True)

    await bot5.on_started(object())

    announce.assert_awaited_once()
    await asyncio.sleep(0)  # let the scheduled reminder task run
    assert reminder_started["value"] is True
