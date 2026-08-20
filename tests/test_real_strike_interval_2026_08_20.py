"""
option_chain.get_real_strike_interval() / LiveTradingEngine._get_strike_interval()
/ utils.get_atm_strike(interval=...) (2026-08-20).

FNO_STRIKE_INTERVALS is a hand-maintained table that was already found wrong
for 27/39 symbols once this project (silently computing phantom strikes).
get_real_contract() already protects against ordering a strike that isn't
actually listed by snapping to the nearest real one -- but a wrong interval
still corrupts the *candidate* fed into it, especially find_delta_strike()'s
scan grid (candidates spaced by strike_interval across up to 30 strikes from
ATM). get_real_strike_interval() derives the interval instead from the same
daily-refreshed real-contract cache get_real_contract() already reads, so it
can never drift out of sync with what Zerodha actually lists, and a newly
added symbol needs zero manual verification to get this right.
"""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from src.core.utils import get_atm_strike
from src.market_data.option_chain import get_real_strike_interval
from src.core.constants import REDIS_CONTRACT_PREFIX


class _FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)


def _cache_payload(strikes: dict) -> dict:
    return {"2026-09-29": strikes}


@pytest.mark.asyncio
async def test_derives_standard_interval_from_real_listed_strikes():
    strikes = {str(k): {"CE": f"X{k}CE", "PE": f"X{k}PE"} for k in (1150, 1160, 1170, 1180, 1190)}
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload(strikes))})

    interval = await get_real_strike_interval("BAJFINANCE", date(2026, 9, 29), redis)

    assert interval == 10


@pytest.mark.asyncio
async def test_derives_fractional_interval_for_half_strike_symbols():
    # Real NSE grid for symbols like ITC/WIPRO/ONGC includes genuine half-strikes.
    strikes = {"245.0": {"CE": "X245CE"}, "247.5": {"PE": "X2475PE"}, "250.0": {"CE": "X250CE"}}
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}ITC": json.dumps(_cache_payload(strikes))})

    interval = await get_real_strike_interval("ITC", date(2026, 9, 29), redis)

    assert interval == 2.5


@pytest.mark.asyncio
async def test_uses_minimum_gap_not_first_gap():
    # A wide gap near the edge of the listed range must not corrupt the
    # derived interval -- the true grid spacing is the minimum consecutive
    # gap, found near ATM where the full grid is listed.
    strikes = {str(k): {"CE": f"X{k}CE"} for k in (1100, 1150, 1160, 1170)}
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload(strikes))})

    interval = await get_real_strike_interval("BAJFINANCE", date(2026, 9, 29), redis)

    assert interval == 10


@pytest.mark.asyncio
async def test_returns_none_on_cache_miss():
    redis = _FakeRedis({})
    assert await get_real_strike_interval("BAJFINANCE", date(2026, 9, 29), redis) is None


@pytest.mark.asyncio
async def test_returns_none_when_fewer_than_two_strikes_listed():
    strikes = {"1160": {"CE": "X1160CE"}}
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload(strikes))})

    assert await get_real_strike_interval("BAJFINANCE", date(2026, 9, 29), redis) is None


@pytest.mark.asyncio
async def test_returns_none_when_redis_is_none():
    assert await get_real_strike_interval("BAJFINANCE", date(2026, 9, 29), None) is None


@pytest.mark.asyncio
async def test_returns_none_on_redis_error_not_raise():
    class _BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

    assert await get_real_strike_interval("BAJFINANCE", date(2026, 9, 29), _BrokenRedis()) is None


# ── get_atm_strike(interval=...) ─────────────────────────────────────────────

def test_get_atm_strike_uses_explicit_interval_over_static_table():
    # KOTAKBANK's real static-table interval is 5 -- pass a deliberately
    # different value to prove the explicit interval wins, not the table.
    assert get_atm_strike(1103.0, "KOTAKBANK", interval=10) == 1100
    assert get_atm_strike(1103.0, "KOTAKBANK") != 1100  # static table (5) would round to 1105


def test_get_atm_strike_falls_back_to_static_table_when_interval_omitted():
    # Matches the pre-2026-08-20 behavior exactly when no real interval is available.
    from src.core.constants import FNO_STRIKE_INTERVALS
    assert get_atm_strike(1103.0, "KOTAKBANK") == round(1103.0 / FNO_STRIKE_INTERVALS["KOTAKBANK"]) * FNO_STRIKE_INTERVALS["KOTAKBANK"]


# ── LiveTradingEngine._get_strike_interval() ─────────────────────────────────

@pytest.mark.asyncio
async def test_engine_get_strike_interval_prefers_real_cache():
    from src.live_trading.live_trading_engine import LiveTradingEngine

    strikes = {str(k): {"CE": f"X{k}CE"} for k in (1150, 1160, 1170)}
    stub = SimpleNamespace(_redis=_FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload(strikes))}))

    interval = await LiveTradingEngine._get_strike_interval(stub, "BAJFINANCE", date(2026, 9, 29))

    assert interval == 10


@pytest.mark.asyncio
async def test_engine_get_strike_interval_falls_back_to_static_table_on_cache_miss():
    from src.live_trading.live_trading_engine import LiveTradingEngine
    from src.core.constants import FNO_STRIKE_INTERVALS

    stub = SimpleNamespace(_redis=_FakeRedis({}))  # nothing cached

    interval = await LiveTradingEngine._get_strike_interval(stub, "BAJFINANCE", date(2026, 9, 29))

    assert interval == FNO_STRIKE_INTERVALS.get("BAJFINANCE", 50)
