"""
positions_router._positions_from_engine()'s short-TTL cache (2026-08-13).

/positions, /analytics/pnl-summary, and /analytics/capital-periods each
independently called this function (which walks engine state and fetches
live market prices) on every dashboard render -- the dashboard fetches
all three in the same page load, every 60s auto-refresh, so this was
doing the identical work 3x per render. Uses the REAL module (not the
isolated importlib-loaded instances test_positions_router.py/
test_analytics_pnl_summary.py use) since the cache lives at module scope
-- conftest.py's autouse fixture resets it before/after every test.
"""
import pytest
from unittest.mock import AsyncMock

from src.api.routers import positions_router


@pytest.mark.asyncio
async def test_second_call_within_ttl_reuses_cached_result(monkeypatch):
    fetch = AsyncMock(return_value=[{"symbol": "TITAN26SEP4650PE"}])
    monkeypatch.setattr(positions_router, "_positions_from_engine_uncached", fetch)

    engine = object()
    first  = await positions_router._positions_from_engine(engine, kite=None, redis=None)
    second = await positions_router._positions_from_engine(engine, kite=None, redis=None)

    assert first == second == [{"symbol": "TITAN26SEP4650PE"}]
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_after_ttl_expires_refetches(monkeypatch):
    fetch = AsyncMock(side_effect=[[{"symbol": "A"}], [{"symbol": "B"}]])
    monkeypatch.setattr(positions_router, "_positions_from_engine_uncached", fetch)
    monkeypatch.setattr(positions_router, "_POSITIONS_CACHE_TTL_SECONDS", 0.0)

    engine = object()
    first  = await positions_router._positions_from_engine(engine, kite=None, redis=None)
    second = await positions_router._positions_from_engine(engine, kite=None, redis=None)

    assert first == [{"symbol": "A"}]
    assert second == [{"symbol": "B"}]
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_different_engines_do_not_share_a_cache_entry():
    # Fixed while writing this test: an earlier version keyed the cache
    # globally (not per-engine) -- calling with a different engine within
    # the TTL window would have returned the FIRST engine's stale result.
    calls = {"n": 0}

    async def _fake_uncached(engine, kite, redis):
        calls["n"] += 1
        return [{"symbol": f"ENGINE-{id(engine)}"}]

    import unittest.mock as mock
    with mock.patch.object(positions_router, "_positions_from_engine_uncached", _fake_uncached):
        engine_a, engine_b = object(), object()
        result_a = await positions_router._positions_from_engine(engine_a, kite=None, redis=None)
        result_b = await positions_router._positions_from_engine(engine_b, kite=None, redis=None)

    assert result_a != result_b
    assert calls["n"] == 2, "each distinct engine must get its own real fetch, not a shared cache hit"
