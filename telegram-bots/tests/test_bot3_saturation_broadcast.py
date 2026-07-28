"""Tests for bot3._broadcast_saturation_alert.

The broadcast function assembles a message that lets admins see the full band
list and know exactly which threshold fires next. These tests mock the two
internal helpers (_effective_thresholds, _saturation_alert_recipients) and the
Telegram send call so no real network or DB is touched.

Scenarios covered:
  * The rendered text always contains "Alert bands:" followed by every band.
  * The rendered text always contains "Next alert at:" with the first uncrossed
    band derived from the live ratio.
  * When the ratio is past every band, "Next alert at:" reads
    "none — all bands crossed".
  * Every recipient returned by _saturation_alert_recipients receives the same
    message text.
  * A single failing send never aborts the broadcast to other recipients.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import bot3


def _run(coro):
    return asyncio.run(coro)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _patch_broadcast_deps(monkeypatch, *, bands, recipients):
    """Patch _effective_thresholds and _saturation_alert_recipients on bot3.

    Returns a FakeSender whose .calls list records every (uid, text) pair that
    _broadcast_saturation_alert passes to send().
    """
    monkeypatch.setattr(bot3, "_effective_thresholds", lambda: bands)
    monkeypatch.setattr(
        bot3,
        "_saturation_alert_recipients",
        AsyncMock(return_value=recipients),
    )

    calls: list[tuple[int, str]] = []

    async def fake_send(_app, uid, text):
        calls.append((uid, text))
        return object()

    monkeypatch.setattr(bot3, "send", fake_send)
    return calls


# ─── message content: Alert bands line ───────────────────────────────────────

async def test_alert_bands_line_lists_all_bands(monkeypatch):
    """Every band in _effective_thresholds appears in the broadcast text."""
    bands = [50, 75, 90, 95, 99]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=[1001])

    await bot3._broadcast_saturation_alert(object(), ratio=0.60)

    assert calls, "send was never called"
    _uid, text = calls[0]
    assert "Alert bands:" in text
    for b in bands:
        assert f"{b}%" in text, f"band {b}% missing from broadcast text"


async def test_alert_bands_line_respects_custom_thresholds(monkeypatch):
    """The function uses whatever _effective_thresholds returns, not a hard-
    coded constant, so a caller can override the bands."""
    bands = [60, 80]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=[42])

    await bot3._broadcast_saturation_alert(object(), ratio=0.50)

    _uid, text = calls[0]
    assert "Alert bands:" in text
    assert "60%" in text
    assert "80%" in text
    assert "50%" not in text  # default bands must not bleed through


# ─── message content: Next alert at line ─────────────────────────────────────

async def test_next_alert_is_first_uncrossed_band(monkeypatch):
    """'Next alert at:' shows the lowest band still above the current ratio.

    The assertion matches the full 'Next alert at: 90%' substring so it cannot
    be accidentally satisfied by '90%' appearing on the Alert bands line.
    """
    bands = [50, 75, 90, 95, 99]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=[1])

    # ratio 0.82 → 82% → bands 50 and 75 are crossed; next is 90
    await bot3._broadcast_saturation_alert(object(), ratio=0.82)

    _uid, text = calls[0]
    assert "Next alert at: 90%" in text, f"expected 'Next alert at: 90%' in: {text!r}"
    assert "none" not in text


async def test_next_alert_is_correct_at_boundary(monkeypatch):
    """A ratio exactly at a band value means that band IS crossed; the next
    uncrossed band is the one immediately above it."""
    bands = [50, 75, 90]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=[1])

    # ratio 0.75 → 75.0% → 75 is not > 75, so next uncrossed is 90
    await bot3._broadcast_saturation_alert(object(), ratio=0.75)

    _uid, text = calls[0]
    assert "Next alert at: 90%" in text, f"expected 'Next alert at: 90%' in: {text!r}"


async def test_next_alert_low_ratio_is_first_band(monkeypatch):
    """When no band has been crossed yet the 'Next alert at:' is the first
    (lowest) band."""
    bands = [50, 75, 90, 95, 99]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=[1])

    await bot3._broadcast_saturation_alert(object(), ratio=0.10)

    _uid, text = calls[0]
    assert "Next alert at: 50%" in text, f"expected 'Next alert at: 50%' in: {text!r}"


# ─── edge case: all bands crossed ────────────────────────────────────────────

async def test_all_bands_crossed_shows_none_message(monkeypatch):
    """When the ratio exceeds every band, 'Next alert at:' must read
    'none — all bands crossed' rather than a percentage."""
    bands = [50, 75, 90, 95, 99]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=[1])

    # ratio 0.995 → 99.5% > 99 — all bands crossed
    await bot3._broadcast_saturation_alert(object(), ratio=0.995)

    _uid, text = calls[0]
    assert "Next alert at: none — all bands crossed" in text, (
        f"expected exact all-crossed phrase in: {text!r}"
    )


async def test_all_bands_crossed_single_band(monkeypatch):
    """Edge case with a single-band list: once crossed the message shows the
    all-crossed phrase on the Next alert at line."""
    bands = [80]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=[7])

    await bot3._broadcast_saturation_alert(object(), ratio=0.90)

    _uid, text = calls[0]
    assert "Next alert at: none — all bands crossed" in text, (
        f"expected exact all-crossed phrase in: {text!r}"
    )


# ─── recipient delivery ───────────────────────────────────────────────────────

async def test_all_recipients_receive_the_broadcast(monkeypatch):
    """Every UID returned by _saturation_alert_recipients gets one DM."""
    bands = [75]
    recipients = [101, 202, 303]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=recipients)

    await bot3._broadcast_saturation_alert(object(), ratio=0.50)

    assert [uid for uid, _ in calls] == recipients


async def test_all_recipients_get_identical_text(monkeypatch):
    """The same text is sent to every recipient — no per-user rendering."""
    bands = [50, 90]
    recipients = [1, 2, 3]
    calls = _patch_broadcast_deps(monkeypatch, bands=bands, recipients=recipients)

    await bot3._broadcast_saturation_alert(object(), ratio=0.60)

    texts = [t for _, t in calls]
    assert len(set(texts)) == 1, "recipients received different texts"


async def test_single_failing_send_does_not_abort_broadcast(monkeypatch):
    """A send() that raises for one recipient must not prevent the remaining
    recipients from being notified — the broadcast continues and logs the error."""
    bands = [50]
    recipients = [1, 2, 3]
    monkeypatch.setattr(bot3, "_effective_thresholds", lambda: bands)
    monkeypatch.setattr(
        bot3, "_saturation_alert_recipients", AsyncMock(return_value=recipients)
    )

    sent: list[int] = []

    async def flaky_send(_app, uid, text):
        if uid == 2:
            raise RuntimeError("Telegram delivery error")
        sent.append(uid)
        return object()

    monkeypatch.setattr(bot3, "send", flaky_send)

    # Must not raise even though recipient 2 failed.
    await bot3._broadcast_saturation_alert(object(), ratio=0.60)

    # All recipients must have been attempted; 1 and 3 succeeded.
    assert 1 in sent, "recipient 1 was not delivered"
    assert 3 in sent, "recipient 3 was skipped after recipient 2 failed"
    assert 2 not in sent, "recipient 2 should have failed"


# ─── _saturation_alert_recipients unit tests ─────────────────────────────────

async def test_recipients_include_management_and_owners(monkeypatch):
    """management and owner roles are both included; others are excluded."""
    rows = [
        {"tg_uid": 10, "role": "management"},
        {"tg_uid": 20, "role": "owner"},
        {"tg_uid": 30, "role": "dev_admin"},   # not in target roles
        {"tg_uid": 40, "role": "base_admin"},  # not in target roles
    ]

    async def fake_list_users(roles):
        return [r for r in rows if r["role"] in roles]

    monkeypatch.setattr(bot3.db, "list_users_by_roles", fake_list_users)
    monkeypatch.setattr(bot3, "HARDCODED_MANAGEMENT_ID", 99)

    result = await bot3._saturation_alert_recipients()

    assert 10 in result
    assert 20 in result
    assert 99 in result   # hardcoded id always present
    assert 30 not in result
    assert 40 not in result


async def test_recipients_deduped_and_sorted(monkeypatch):
    """Duplicate UIDs (e.g. hardcoded id also in the DB) appear only once;
    the list is in ascending order."""
    rows = [
        {"tg_uid": 99, "role": "management"},  # same as hardcoded id
        {"tg_uid": 10, "role": "owner"},
    ]

    async def fake_list_users(_roles):
        return rows

    monkeypatch.setattr(bot3.db, "list_users_by_roles", fake_list_users)
    monkeypatch.setattr(bot3, "HARDCODED_MANAGEMENT_ID", 99)

    result = await bot3._saturation_alert_recipients()

    assert result == sorted(set(result))
    assert result.count(99) == 1


async def test_recipients_skips_null_tg_uid(monkeypatch):
    """Rows with a NULL / None tg_uid are silently excluded."""
    rows = [
        {"tg_uid": None, "role": "management"},
        {"tg_uid": 55, "role": "owner"},
    ]

    async def fake_list_users(_roles):
        return rows

    monkeypatch.setattr(bot3.db, "list_users_by_roles", fake_list_users)
    monkeypatch.setattr(bot3, "HARDCODED_MANAGEMENT_ID", 1)

    result = await bot3._saturation_alert_recipients()

    assert None not in result
    assert 55 in result
