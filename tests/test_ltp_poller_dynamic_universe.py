"""
LTPPoller dynamic active-universe wiring (2026-08-20).

The weekly recompute job can shrink the active universe if a symbol's
liquidity falls below the floor. That must never cost an open position its
market data -- register_underlying()/unregister_underlying() force-track any
symbol with a currently open position regardless of the dynamically-fetched
active list.
"""
import json

import pytest

from src.market_data.ltp_poller import LTPPoller
from src.core.constants import FNO_SYMBOLS, REDIS_ACTIVE_FNO_SYMBOLS


class _FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)


@pytest.mark.asyncio
async def test_refresh_pulls_the_dynamic_active_list_by_default():
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["A", "B"])})
    poller = LTPPoller(redis_client=redis)  # symbols=None -> dynamic

    await poller._refresh_active_symbols()

    assert poller.symbols == ["A", "B"]


@pytest.mark.asyncio
async def test_refresh_unions_with_force_tracked_underlyings():
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["A", "B"])})
    poller = LTPPoller(redis_client=redis)
    poller.register_underlying("ZZZTHIN")  # an open position on a now-illiquid symbol

    await poller._refresh_active_symbols()

    assert set(poller.symbols) == {"A", "B", "ZZZTHIN"}


@pytest.mark.asyncio
async def test_unregister_drops_a_previously_force_tracked_symbol():
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["A"])})
    poller = LTPPoller(redis_client=redis)
    poller.register_underlying("ZZZTHIN")
    await poller._refresh_active_symbols()
    assert "ZZZTHIN" in poller.symbols

    poller.unregister_underlying("ZZZTHIN")
    await poller._refresh_active_symbols()

    assert "ZZZTHIN" not in poller.symbols


@pytest.mark.asyncio
async def test_explicit_symbols_at_construction_are_never_overwritten():
    # A caller that pins symbols explicitly (e.g. a test harness) must keep
    # exactly that list -- not have it silently replaced by the dynamic
    # active universe (which, with no redis wired up in some callers, could
    # even silently fall back to the full static FNO_SYMBOLS instead).
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["A", "B", "C"])})
    poller = LTPPoller(redis_client=redis, symbols=["ONLY_THIS"])

    await poller._refresh_active_symbols()

    assert poller.symbols == ["ONLY_THIS"]


@pytest.mark.asyncio
async def test_falls_back_to_static_fno_symbols_when_redis_has_nothing_cached():
    poller = LTPPoller(redis_client=_FakeRedis({}))
    await poller._refresh_active_symbols()
    assert set(poller.symbols) == set(FNO_SYMBOLS)
