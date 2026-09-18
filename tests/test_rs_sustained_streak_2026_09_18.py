"""
RSRanker's sustained-rank streak tracker (2026-09-18) -- how many
CONSECUTIVE CALENDAR DAYS a symbol has held a top-3/bottom-3 RS rank, not
just today's snapshot. This is what actually distinguished PAYTM (RS rank
#1 for 7 straight trading days, confirmed live) from an ordinary stock
sitting in today's top-3 by chance. Feeds the shadow-mode override for
regime-paused directional strategies.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.market_data.rs_ranker import RSRanker, REDIS_RS_SUSTAINED_STREAK_KEY


class _FakeRedis:
    def __init__(self, seed=None):
        self.store = dict(seed or {})

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _scores(top_symbols, bottom_symbols):
    """Build a minimal scores list with the given symbols at the extremes
    -- _update_sustained_streak only reads scores[:n]/scores[-n:]."""
    mid = [{"symbol": f"MID{i}", "rs_score": 50.0} for i in range(4)]
    top = [{"symbol": s, "rs_score": 90.0} for s in top_symbols]
    bottom = [{"symbol": s, "rs_score": 10.0} for s in bottom_symbols]
    return top + mid + bottom


@pytest.mark.asyncio
async def test_first_day_seeds_streak_at_one():
    redis = _FakeRedis()
    ranker = RSRanker(redis)

    await ranker._update_sustained_streak(_scores(["PAYTM", "A", "B"], ["Z", "Y", "X"]))

    raw = json.loads(redis.store[REDIS_RS_SUSTAINED_STREAK_KEY])
    assert raw["streaks"]["PAYTM"] == {"side": "top", "count": 1}
    assert raw["streaks"]["Z"] == {"side": "bottom", "count": 1}


@pytest.mark.asyncio
async def test_streak_extends_on_a_genuinely_new_day_if_symbol_still_qualifies():
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    seed = {
        REDIS_RS_SUSTAINED_STREAK_KEY: json.dumps({
            "date": yesterday,
            "streaks": {"PAYTM": {"side": "top", "count": 6}},
        })
    }
    redis = _FakeRedis(seed)
    ranker = RSRanker(redis)

    await ranker._update_sustained_streak(_scores(["PAYTM", "A", "B"], ["Z", "Y", "X"]))

    raw = json.loads(redis.store[REDIS_RS_SUSTAINED_STREAK_KEY])
    assert raw["streaks"]["PAYTM"] == {"side": "top", "count": 7}


@pytest.mark.asyncio
async def test_streak_resets_when_symbol_drops_out_for_a_day():
    two_days_ago = (datetime.now().date() - timedelta(days=2)).isoformat()
    seed = {
        REDIS_RS_SUSTAINED_STREAK_KEY: json.dumps({
            "date": two_days_ago,
            "streaks": {"PAYTM": {"side": "top", "count": 6}},
        })
    }
    redis = _FakeRedis(seed)
    ranker = RSRanker(redis)

    # PAYTM isn't in today's top-3/bottom-3 at all this call (dropped out
    # for at least the intervening day) -- when it returns later, it must
    # start fresh, not silently resume the old count.
    await ranker._update_sustained_streak(_scores(["A", "B", "C"], ["Z", "Y", "X"]))
    assert "PAYTM" not in json.loads(redis.store[REDIS_RS_SUSTAINED_STREAK_KEY])["streaks"]


@pytest.mark.asyncio
async def test_is_idempotent_within_the_same_calendar_day():
    redis = _FakeRedis()
    ranker = RSRanker(redis)

    await ranker._update_sustained_streak(_scores(["PAYTM", "A", "B"], ["Z", "Y", "X"]))
    # A second call the SAME day with a different snapshot (e.g. PAYTM
    # momentarily out of top-3 mid-day) must NOT overwrite the day's
    # already-recorded determination.
    await ranker._update_sustained_streak(_scores(["A", "B", "C"], ["Z", "Y", "X"]))

    raw = json.loads(redis.store[REDIS_RS_SUSTAINED_STREAK_KEY])
    assert raw["streaks"]["PAYTM"] == {"side": "top", "count": 1}


@pytest.mark.asyncio
async def test_switching_sides_resets_the_streak():
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    seed = {
        REDIS_RS_SUSTAINED_STREAK_KEY: json.dumps({
            "date": yesterday,
            "streaks": {"IDEA": {"side": "bottom", "count": 4}},
        })
    }
    redis = _FakeRedis(seed)
    ranker = RSRanker(redis)

    # IDEA flipped to top-3 today -- a real change in character, not a
    # continuation of its "weak" streak.
    await ranker._update_sustained_streak(_scores(["IDEA", "A", "B"], ["Z", "Y", "X"]))

    raw = json.loads(redis.store[REDIS_RS_SUSTAINED_STREAK_KEY])
    assert raw["streaks"]["IDEA"] == {"side": "top", "count": 1}


# ── get_sustained_streak() -- fail-closed read side ──────────────────────────

@pytest.mark.asyncio
async def test_get_sustained_streak_returns_empty_with_no_data():
    ranker = RSRanker(_FakeRedis())
    assert await ranker.get_sustained_streak() == {}


@pytest.mark.asyncio
async def test_get_sustained_streak_fails_closed_on_stale_date():
    stale_date = (datetime.now().date() - timedelta(days=3)).isoformat()
    seed = {
        REDIS_RS_SUSTAINED_STREAK_KEY: json.dumps({
            "date": stale_date,
            "streaks": {"PAYTM": {"side": "top", "count": 7}},
        })
    }
    ranker = RSRanker(_FakeRedis(seed))

    assert await ranker.get_sustained_streak() == {}


@pytest.mark.asyncio
async def test_get_sustained_streak_returns_todays_real_data():
    today = datetime.now().date().isoformat()
    seed = {
        REDIS_RS_SUSTAINED_STREAK_KEY: json.dumps({
            "date": today,
            "streaks": {"PAYTM": {"side": "top", "count": 7}},
        })
    }
    ranker = RSRanker(_FakeRedis(seed))

    result = await ranker.get_sustained_streak()

    assert result == {"PAYTM": {"side": "top", "count": 7}}
