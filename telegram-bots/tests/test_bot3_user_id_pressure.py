"""Tests for the bot3 panel-user-id saturation logging.

``_generate_unique_user_id`` widens the digit count as collisions appear so a
growing user base keeps finding free ids. The only *reactive* signal that the id
space is filling up is the friendly "please try again" failure once every retry
collides. To give admins early warning, the generator now emits warnings:

  * when it has to *widen* the digit count (collision pressure forced a 10x
    larger address space), and
  * when it only lands a free id after burning more than a small number of
    retries (``_USER_ID_RETRY_LOG_THRESHOLD``).

These tests drive the real ``_generate_unique_user_id`` with a deterministic
``gen_user_id`` + ``db.get_user_by_user_id`` stand-in that marks the first N
candidates as taken, and assert the exact log lines fire (or stay silent) at
each pressure level. ``gen_user_id`` is patched to embed the requested digit
count in each candidate so widening is directly observable.
"""
from __future__ import annotations

import asyncio
import logging

import bot3
import db


def _run(coro):
    return asyncio.run(coro)


def _install_fake_id_space(monkeypatch, taken_count: int) -> list[int]:
    """Patch the id generator + lookup so the first ``taken_count`` candidates
    are 'already taken'. Returns the list of digit-widths requested, in order,
    so a test can confirm whether/when widening happened."""
    digits_seen: list[int] = []

    def fake_gen(digits: int = bot3._USER_ID_DIGITS) -> str:
        digits_seen.append(digits)
        # 1-based index of this candidate, encoded into the id.
        return f"{digits}:{len(digits_seen)}"

    async def fake_lookup(candidate: str):
        idx = int(candidate.split(":")[1])
        return {"user_id": candidate} if idx <= taken_count else None

    monkeypatch.setattr(bot3, "gen_user_id", fake_gen)
    monkeypatch.setattr(db, "get_user_by_user_id", fake_lookup)
    return digits_seen


def test_clean_generation_logs_nothing(monkeypatch, caplog):
    """A free id on the first try (the overwhelmingly common case) is silent."""
    _install_fake_id_space(monkeypatch, taken_count=0)
    with caplog.at_level(logging.WARNING, logger="zenin.bot3"):
        uid = _run(bot3._generate_unique_user_id())
    assert uid == f"{bot3._USER_ID_DIGITS}:1"
    assert caplog.records == []


def test_few_collisions_under_threshold_stay_silent(monkeypatch, caplog):
    """Burning a couple of retries is normal noise and must not warn."""
    taken = bot3._USER_ID_RETRY_LOG_THRESHOLD - 1
    _install_fake_id_space(monkeypatch, taken_count=taken)
    with caplog.at_level(logging.WARNING, logger="zenin.bot3"):
        _run(bot3._generate_unique_user_id())
    assert caplog.records == []


def test_retry_pressure_logs_warning(monkeypatch, caplog):
    """Once more than a small number of retries collide (but not enough to
    widen) the generator warns about the collision pressure."""
    taken = bot3._USER_ID_RETRY_LOG_THRESHOLD
    # Stay below the first widen point so only the retry-pressure path fires.
    assert taken < bot3._USER_ID_WIDEN_EVERY
    digits_seen = _install_fake_id_space(monkeypatch, taken_count=taken)
    with caplog.at_level(logging.WARNING, logger="zenin.bot3"):
        _run(bot3._generate_unique_user_id())
    # No widening: every candidate was generated at the starting width.
    assert set(digits_seen) == {bot3._USER_ID_DIGITS}
    msgs = [r.getMessage() for r in caplog.records]
    assert len(msgs) == 1
    assert "user id pressure" in msgs[0]
    assert f"{taken} collision" in msgs[0]


def test_enough_collisions_widen_and_log(monkeypatch, caplog):
    """Crossing a widen boundary logs the widening (with the new width) and,
    because that also burns past the retry threshold, the pressure line."""
    taken = bot3._USER_ID_WIDEN_EVERY  # forces exactly one widen, then a free id
    digits_seen = _install_fake_id_space(monkeypatch, taken_count=taken)
    with caplog.at_level(logging.WARNING, logger="zenin.bot3"):
        uid = _run(bot3._generate_unique_user_id())
    # The free id came from the widened space.
    widened = bot3._USER_ID_DIGITS + 1
    assert uid == f"{widened}:{taken + 1}"
    assert widened in digits_seen
    msgs = [r.getMessage() for r in caplog.records]
    widen_lines = [m for m in msgs if "saturating" in m]
    pressure_lines = [m for m in msgs if "user id pressure" in m]
    assert len(widen_lines) == 1
    assert f"widening to {widened} digits" in widen_lines[0]
    assert len(pressure_lines) == 1
