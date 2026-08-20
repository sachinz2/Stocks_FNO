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


@pytest.mark.asyncio
async def test_refresh_tracks_active_set_separately_from_the_polled_union():
    # The bug this guards: self.symbols (polled) must not be used directly
    # to decide entry-candidate-pool eligibility, since it includes
    # force-tracked symbols that aren't actually liquidity-active.
    redis = _FakeRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["A", "B"])})
    poller = LTPPoller(redis_client=redis)
    poller.register_underlying("ZZZTHIN")

    await poller._refresh_active_symbols()

    assert set(poller.symbols) == {"A", "B", "ZZZTHIN"}
    assert poller._active_set == {"A", "B"}
    assert "ZZZTHIN" not in poller._active_set


# ── Force-tracked-but-inactive symbols must not enter entry-candidate pools
# (2026-08-20, code-review fix) ───────────────────────────────────────────────
#
# Confirmed live bug: LTPPoller polled ALL of self.symbols (active-liquid
# UNION force-tracked) and computed a score for every one of them, then
# published the top-N by score straight to the pools _process_signal reads
# for NEW entries -- so a symbol force-tracked only to keep an EXISTING
# position's exit data fresh could still rank into a pool and trigger a
# brand-new reversal entry on itself, defeating the whole point of demoting
# it. Fix: self._active_set gates pool eligibility; self.symbols (the union)
# still gates what gets polled/enriched, so market data stays fresh either way.

import pandas as pd
from datetime import datetime, timedelta

from src.core.constants import REDIS_TOP_SYMBOLS_KEY, REDIS_TOP_SYMBOLS_MOMENTUM


class _FakePollRedis:
    def __init__(self, store=None):
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


def _valid_history(n=60):
    dates = pd.date_range(datetime.now() - timedelta(days=n), periods=n, freq="5min")
    return pd.DataFrame({
        "date": dates, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000,
    })


@pytest.mark.asyncio
async def test_force_tracked_but_inactive_symbol_never_enters_a_published_pool(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)

    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["ACTIVE_SYM"])})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={"ACTIVE_SYM": 1, "THIN_SYM": 2},
    )
    poller.register_underlying("THIN_SYM")  # open position, but not in the active list

    hist = _valid_history()

    async def _fake_get_history(symbol, loop):
        return hist.copy()

    async def _fake_get_history_15m(symbol, loop):
        return None

    async def _fake_read_day_range(symbol):
        return None

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)
    # Isolate this test to the eligibility gate itself -- both symbols score
    # identically high, so if THIN_SYM shows up in a pool it's only because
    # the gate failed, not because of an incidental scoring difference.
    monkeypatch.setattr(poller, "_score_all", lambda tick: (10.0, 5.0, 5.0, 10.0))

    await poller.poll()

    assert poller._active_set == {"ACTIVE_SYM"}
    assert set(poller.symbols) == {"ACTIVE_SYM", "THIN_SYM"}, "THIN_SYM must still be polled"

    top_ema = json.loads(redis.store[REDIS_TOP_SYMBOLS_KEY])
    top_momentum = json.loads(redis.store[REDIS_TOP_SYMBOLS_MOMENTUM])
    assert "THIN_SYM" not in top_ema, "force-tracked-only symbol must not be entry-pool eligible"
    assert "THIN_SYM" not in top_momentum
    assert "ACTIVE_SYM" in top_ema
    assert "ACTIVE_SYM" in top_momentum
