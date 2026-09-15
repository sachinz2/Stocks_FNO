"""
LTPPoller universe-health visibility (2026-09-15, external review).

"The strategy is scanning 40 stocks" could previously silently mean far
fewer in practice -- a widespread history-fetch outage, or most of the
universe stuck on the historical-close bootstrap fallback -- with no
visible signal short of grepping per-symbol warning logs. poll() now
publishes market:universe_health every cycle with universe_size,
history_valid, live_data_valid, and candidate_count.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.market_data.ltp_poller import LTPPoller
from src.core.constants import REDIS_ACTIVE_FNO_SYMBOLS


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
async def test_universe_health_reports_full_coverage_when_everything_is_healthy(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    symbols = ["A", "B", "C"]
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(symbols)})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={s: i for i, s in enumerate(symbols)},
    )
    hist = _valid_history()

    async def _fake_get_history(symbol, loop):
        return hist.copy()

    async def _fake_get_history_15m(symbol, loop):
        return None

    async def _fake_read_day_range(symbol):
        return {"close": 100.0}  # real live data for every symbol

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)

    await poller.poll()

    health = json.loads(redis.store["market:universe_health"])
    assert health["universe_size"] == 3
    assert health["history_valid"] == 3
    assert health["live_data_valid"] == 3
    assert health["candidate_count"] == 3  # EMA scores every symbol with valid history


@pytest.mark.asyncio
async def test_universe_health_reflects_a_partial_history_fetch_outage(monkeypatch):
    # "Scanning 40 stocks" but only 1 of 3 actually has enough history --
    # must be visible in history_valid, not silently absorbed.
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    symbols = ["GOOD", "NOHIST1", "NOHIST2"]
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(symbols)})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={s: i for i, s in enumerate(symbols)},
    )
    hist = _valid_history()

    async def _fake_get_history(symbol, loop):
        return hist.copy() if symbol == "GOOD" else None  # insufficient history

    async def _fake_get_history_15m(symbol, loop):
        return None

    async def _fake_read_day_range(symbol):
        return {"close": 100.0}

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)

    await poller.poll()

    health = json.loads(redis.store["market:universe_health"])
    assert health["universe_size"] == 3
    assert health["history_valid"] == 1
    assert health["candidate_count"] == 1


@pytest.mark.asyncio
async def test_universe_health_reflects_symbols_stuck_on_bootstrap_fallback(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    symbols = ["LIVE", "STALE1", "STALE2"]
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(symbols)})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={s: i for i, s in enumerate(symbols)},
    )
    hist = _valid_history()

    async def _fake_get_history(symbol, loop):
        return hist.copy()

    async def _fake_get_history_15m(symbol, loop):
        return None

    async def _fake_read_day_range(symbol):
        # No live day_range (None) for STALE* -- falls back to historical
        # close, tracked in poller._no_live_data_warned.
        return {"close": 100.0} if symbol == "LIVE" else None

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)

    await poller.poll()

    health = json.loads(redis.store["market:universe_health"])
    assert health["universe_size"] == 3
    assert health["history_valid"] == 3       # history is fine for all 3
    assert health["live_data_valid"] == 1     # only LIVE has real tick data
