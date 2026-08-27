"""
Second-opinion review (2026-08-27): has_event_within_days() returned False on
ANY lookup failure -- including total unavailability (Redis down AND the
JSON fallback missing/corrupt) -- silently letting new credit_spread_v1/
iron_condor_v1 entries through with zero event-risk check. Opposite of the
fail-closed convention used everywhere else in this codebase (MTF, RS, entry
price). Fixed: a genuinely empty/non-matching calendar successfully LOADED
from either source still returns False (a real "no events" result), but
total unavailability (neither source yields anything) now raises
CalendarUnavailable internally, which has_event_within_days() catches and
turns into a blocking True.
"""
import json
from datetime import date, timedelta

import pytest

from src.market_data import event_calendar
from src.market_data.event_calendar import has_event_within_days


class _FakeRedis:
    def __init__(self, value=None, raise_exc=None):
        self._value = value
        self._raise_exc = raise_exc

    async def get(self, key):
        if self._raise_exc:
            raise self._raise_exc
        return self._value


@pytest.mark.asyncio
async def test_real_event_within_window_blocks():
    redis = _FakeRedis(value=json.dumps({"INFY": [date.today().isoformat()]}))
    assert await has_event_within_days("INFY", redis, days=5) is True


@pytest.mark.asyncio
async def test_real_calendar_with_no_matching_dates_does_not_block():
    far_future = (date.today() + timedelta(days=365)).isoformat()
    redis = _FakeRedis(value=json.dumps({"INFY": [far_future]}))
    assert await has_event_within_days("INFY", redis, days=5) is False


@pytest.mark.asyncio
async def test_total_unavailability_fails_closed(monkeypatch, tmp_path):
    """Redis down AND no JSON fallback file at all -- genuinely can't tell
    if an event is coming, must block rather than silently proceed."""
    missing_path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(event_calendar, "_CALENDAR_JSON_PATH", str(missing_path))
    redis = _FakeRedis(raise_exc=ConnectionError("redis down"))
    assert await has_event_within_days("INFY", redis, days=5) is True


@pytest.mark.asyncio
async def test_corrupt_json_fallback_fails_closed(monkeypatch, tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(event_calendar, "_CALENDAR_JSON_PATH", str(bad_path))
    redis = _FakeRedis(raise_exc=ConnectionError("redis down"))
    assert await has_event_within_days("INFY", redis, days=5) is True


@pytest.mark.asyncio
async def test_redis_unavailable_but_json_fallback_present_still_works(monkeypatch, tmp_path):
    """A real (if empty) calendar successfully loaded from the JSON fallback
    is a legitimate 'no events' result, not an unavailability -- must NOT
    fail closed just because Redis itself was down."""
    real_path = tmp_path / "real.json"
    real_path.write_text(json.dumps({"_comment": "x", "*": []}), encoding="utf-8")
    monkeypatch.setattr(event_calendar, "_CALENDAR_JSON_PATH", str(real_path))
    redis = _FakeRedis(raise_exc=ConnectionError("redis down"))
    assert await has_event_within_days("INFY", redis, days=5) is False


@pytest.mark.asyncio
async def test_no_redis_configured_uses_json_fallback_normally(monkeypatch, tmp_path):
    real_path = tmp_path / "real.json"
    real_path.write_text(json.dumps({"*": [date.today().isoformat()]}), encoding="utf-8")
    monkeypatch.setattr(event_calendar, "_CALENDAR_JSON_PATH", str(real_path))
    assert await has_event_within_days("ANY_SYMBOL", None, days=5) is True


@pytest.mark.asyncio
async def test_shipped_json_calendar_loads_without_raising():
    """Sanity check against the real, committed config/event_calendar.json --
    guards against a future hand-edit breaking parsing entirely."""
    calendar = await event_calendar._load_calendar(None)
    assert isinstance(calendar, dict)
