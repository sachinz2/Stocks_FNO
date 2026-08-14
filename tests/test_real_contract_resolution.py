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
the computed one isn't listed for that expiry. Returns None on a cache miss.

Fixed 2026-08-14: LiveTradingEngine._resolve_contract() (the caller) used
to fall back to build_option_symbol()'s computed formula whenever
get_real_contract() returned None -- fail-OPEN, silently trading a guessed
symbol/strike that might not even be listed (the exact bug class already
found wrong for 27/39 symbols once). Now fail-closed: _resolve_contract()
itself returns None on a cache miss, and every entry-path caller skips the
trade rather than substituting a computed guess.
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
async def test_resolve_contract_fails_closed_on_cache_miss():
    fake = _FakeResolveEngine(redis=None)

    result = await LiveTradingEngine._resolve_contract(
        fake, "BAJFINANCE", date(2026, 9, 29), 1160, "CE",
    )

    # Fail-closed (2026-08-14): no verified real contract -> None, not a
    # silent fallback to the computed build_option_symbol() guess.
    assert result is None


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


# ── _get_lot_size() fail-closed (2026-08-14) ────────────────────────────────
#
# Used to fall back to the static FNO_LOT_SIZES table on any Redis cache
# miss -- that table's own comment already documents the exact danger this
# caused live: 36/39 symbols were once found wrong there, masked under
# normal operation by the Redis cache, but on any day the daily auth job
# fails or is delayed, this would silently submit wildly wrong order
# quantities. Now returns None on a cache miss; callers must skip the entry.

import inspect
from src.core.constants import REDIS_LOT_SIZE_PREFIX


class _FakeLotSizeEngine:
    def __init__(self, redis):
        self._redis = redis


@pytest.mark.asyncio
async def test_get_lot_size_returns_real_cached_value():
    redis = _FakeRedis({f"{REDIS_LOT_SIZE_PREFIX}RELIANCE": "500"})
    fake = _FakeLotSizeEngine(redis)
    assert await LiveTradingEngine._get_lot_size(fake, "RELIANCE") == 500


@pytest.mark.asyncio
async def test_get_lot_size_fails_closed_on_cache_miss():
    fake = _FakeLotSizeEngine(redis=_FakeRedis({}))
    assert await LiveTradingEngine._get_lot_size(fake, "RELIANCE") is None


@pytest.mark.asyncio
async def test_get_lot_size_fails_closed_when_redis_unavailable():
    fake = _FakeLotSizeEngine(redis=None)
    assert await LiveTradingEngine._get_lot_size(fake, "RELIANCE") is None


@pytest.mark.asyncio
async def test_get_lot_size_fails_closed_on_redis_error():
    class _BrokenRedis:
        async def get(self, key):
            raise RuntimeError("connection lost")
    fake = _FakeLotSizeEngine(redis=_BrokenRedis())
    assert await LiveTradingEngine._get_lot_size(fake, "RELIANCE") is None


# ── Entry-path callers must skip on lot-size/contract fail-closed (2026-08-14) ──
#
# _process_signal/_process_credit_spread/_process_iron_condor all have deep
# precondition chains (IV rank/VIX/ADX/PCR/DTE/strike-selection checks)
# before reaching lot-size/contract resolution -- driving the full method
# end-to-end for this would need an extremely large mock (same rationale
# test_entry_failure_unwind.py documents for the sibling unwind-on-failure
# checks). Source-level regression guards instead.

def test_process_signal_skips_entry_when_lot_size_unavailable():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    idx = src.index("lot_size = await self._get_lot_size(symbol)")
    guard = src[idx:idx + 500]
    assert "if not lot_size:" in guard
    assert "return" in guard


def test_process_signal_skips_entry_when_contract_unresolved():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    idx = src.index("resolved = await self._resolve_contract(symbol, expiry, strike, option_type)")
    guard = src[idx:idx + 500]
    assert "if resolved is None:" in guard
    assert "return" in guard


def test_process_credit_spread_skips_entry_when_lot_size_unavailable():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    idx = src.index("lot_size   = await self._get_lot_size(symbol)")
    guard = src[idx:idx + 500]
    assert "if not lot_size:" in guard
    assert "return" in guard


def test_process_credit_spread_skips_entry_when_any_leg_unresolved():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    # All 4 resolve_contract call sites (initial short/long, OI-bump short/long)
    # must each be followed by a None-check within a short span.
    idx = 0
    found = 0
    while True:
        idx = src.find("await self._resolve_contract(symbol, expiry,", idx)
        if idx == -1:
            break
        guard = src[idx:idx + 250]
        assert "if resolved is None:" in guard, f"missing fail-closed guard near offset {idx}"
        found += 1
        idx += 1
    assert found == 4, f"expected 4 resolve_contract call sites, found {found}"


def test_process_iron_condor_skips_entry_when_lot_size_unavailable():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index("lot_size   = await self._get_lot_size(symbol)")
    guard = src[idx:idx + 500]
    assert "if not lot_size:" in guard
    assert "return" in guard


def test_process_iron_condor_skips_entry_when_any_leg_unresolved():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    idx = src.index('_resolved_legs = {}')
    guard = src[idx:idx + 500]
    assert "if _r is None:" in guard
    assert "return" in guard
