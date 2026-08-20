"""option_chain.get_active_fno_symbols() (2026-08-20)."""
import json

import pytest

from src.market_data.option_chain import get_active_fno_symbols
from src.core.constants import FNO_SYMBOLS, REDIS_ACTIVE_FNO_SYMBOLS


class _FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)


@pytest.mark.asyncio
async def test_returns_cached_active_list_when_present():
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["A", "B", "C"])})
    result = await get_active_fno_symbols(redis)
    assert result == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_falls_back_to_static_list_on_cache_miss():
    redis = _FakeRedis({})
    result = await get_active_fno_symbols(redis)
    assert result == list(FNO_SYMBOLS)


@pytest.mark.asyncio
async def test_falls_back_to_static_list_on_empty_cached_value():
    # A recompute that somehow produced an empty list must not blank out
    # the entire trading universe -- fall back to the known-safe static set.
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps([])})
    result = await get_active_fno_symbols(redis)
    assert result == list(FNO_SYMBOLS)


@pytest.mark.asyncio
async def test_falls_back_to_static_list_when_redis_is_none():
    result = await get_active_fno_symbols(None)
    assert result == list(FNO_SYMBOLS)


@pytest.mark.asyncio
async def test_falls_back_to_static_list_on_redis_error():
    class _BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis down")

    result = await get_active_fno_symbols(_BrokenRedis())
    assert result == list(FNO_SYMBOLS)


@pytest.mark.asyncio
async def test_falls_back_to_static_list_on_malformed_json():
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: "not-json{{"})
    result = await get_active_fno_symbols(redis)
    assert result == list(FNO_SYMBOLS)
