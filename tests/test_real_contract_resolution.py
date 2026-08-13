"""
get_real_contract() / LiveTradingEngine._resolve_contract() (2026-08-13).

Every strike we select is computed via interval arithmetic (get_atm_strike()/
find_delta_strike()), and every symbol string is hand-formatted
(build_option_symbol()) -- neither has ever been cross-checked against what
Zerodha actually has listed. This is the same root-cause shape as
FNO_LOT_SIZES/FNO_STRIKE_INTERVALS, both of which WERE found wrong for
36/39 and 27/39 symbols earlier this project when finally audited against
live kite.instruments("NFO") data.

get_real_contract() validates/corrects a computed (symbol, expiry, strike,
option_type) against a daily-refreshed real-instrument cache (see
scripts/zerodha_auto_auth.py's fetch_and_cache_real_contracts()), returning
the REAL tradingsymbol -- snapping to the nearest actually-listed strike if
the computed one isn't listed for that expiry. Falls back to None (caller
uses build_option_symbol()'s formula, unchanged) on any cache miss.
"""
import json
import pytest
from datetime import date

from src.market_data.option_chain import get_real_contract
from src.core.constants import REDIS_CONTRACT_PREFIX


class _FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _cache_payload():
    # Matches fetch_and_cache_real_contracts()'s shape:
    # expiry_iso -> strike_str -> {"CE": tradingsymbol, "PE": tradingsymbol}
    return {
        "2026-09-29": {
            "1160": {"CE": "BAJFINANCE26SEP1160CE", "PE": "BAJFINANCE26SEP1160PE"},
            "1170": {"CE": "BAJFINANCE26SEP1170CE", "PE": "BAJFINANCE26SEP1170PE"},
            "1190": {"CE": "BAJFINANCE26SEP1190CE", "PE": "BAJFINANCE26SEP1190PE"},
        },
        "2026-08-25": {
            "1160": {"CE": "BAJFINANCE26AUG1160CE", "PE": "BAJFINANCE26AUG1160PE"},
        },
    }


@pytest.mark.asyncio
async def test_exact_strike_match_returns_real_tradingsymbol():
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload())})

    result = await get_real_contract("BAJFINANCE", date(2026, 9, 29), 1160, "CE", redis)

    assert result == ("BAJFINANCE26SEP1160CE", 1160)


@pytest.mark.asyncio
async def test_strike_not_listed_snaps_to_nearest_real_strike():
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload())})

    # 1165 isn't listed for this expiry (1160, 1170, 1190 are) -- nearest is 1160.
    result = await get_real_contract("BAJFINANCE", date(2026, 9, 29), 1165, "CE", redis)

    assert result == ("BAJFINANCE26SEP1160CE", 1160)


@pytest.mark.asyncio
async def test_snaps_to_correctly_nearest_strike_not_just_first():
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload())})

    # 1183 is closer to 1190 (7 away) than 1170 (13 away).
    result = await get_real_contract("BAJFINANCE", date(2026, 9, 29), 1183, "PE", redis)

    assert result == ("BAJFINANCE26SEP1190PE", 1190)


@pytest.mark.asyncio
async def test_symbol_not_cached_returns_none():
    redis = _FakeRedis({})  # no data cached for this symbol at all

    result = await get_real_contract("BAJFINANCE", date(2026, 9, 29), 1160, "CE", redis)

    assert result is None


@pytest.mark.asyncio
async def test_expiry_not_in_cached_window_returns_none():
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload())})

    # Cache only has 2026-08-25 and 2026-09-29 -- a far-month expiry outside
    # the cached 3-nearest window must fail closed to None, not guess.
    result = await get_real_contract("BAJFINANCE", date(2026, 12, 29), 1160, "CE", redis)

    assert result is None


@pytest.mark.asyncio
async def test_redis_none_returns_none():
    result = await get_real_contract("BAJFINANCE", date(2026, 9, 29), 1160, "CE", None)
    assert result is None


@pytest.mark.asyncio
async def test_redis_error_returns_none_not_raise():
    class _BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

    result = await get_real_contract("BAJFINANCE", date(2026, 9, 29), 1160, "CE", _BrokenRedis())
    assert result is None


@pytest.mark.asyncio
async def test_malformed_cache_json_returns_none_not_raise():
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": "not valid json{{{"})
    result = await get_real_contract("BAJFINANCE", date(2026, 9, 29), 1160, "CE", redis)
    assert result is None


# ── LiveTradingEngine._resolve_contract() ────────────────────────────────────

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeResolveEngine:
    def __init__(self, redis):
        self._redis = redis
        self._kite = None


@pytest.mark.asyncio
async def test_resolve_contract_uses_real_match_when_available():
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload())})
    fake = _FakeResolveEngine(redis)

    strike, contract = await LiveTradingEngine._resolve_contract(
        fake, "BAJFINANCE", date(2026, 9, 29), 1160, "CE",
    )

    assert strike == 1160
    assert contract == "BAJFINANCE26SEP1160CE"


@pytest.mark.asyncio
async def test_resolve_contract_falls_back_to_formula_on_cache_miss():
    fake = _FakeResolveEngine(redis=None)

    strike, contract = await LiveTradingEngine._resolve_contract(
        fake, "BAJFINANCE", date(2026, 9, 29), 1160, "CE",
    )

    # Falls back to build_option_symbol()'s formula unchanged.
    assert strike == 1160
    assert contract == "BAJFINANCE26SEP1160CE"  # formula and real data agree here


@pytest.mark.asyncio
async def test_resolve_contract_snapped_strike_flows_through_to_caller():
    redis = _FakeRedis({f"{REDIS_CONTRACT_PREFIX}BAJFINANCE": json.dumps(_cache_payload())})
    fake = _FakeResolveEngine(redis)

    # 1165 doesn't exist -- caller must receive the CORRECTED strike (1160),
    # not the original candidate, so downstream width/credit math stays
    # consistent with the contract actually being traded.
    strike, contract = await LiveTradingEngine._resolve_contract(
        fake, "BAJFINANCE", date(2026, 9, 29), 1165, "CE",
    )

    assert strike == 1160
    assert contract == "BAJFINANCE26SEP1160CE"
