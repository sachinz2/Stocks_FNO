"""
Candidate-pool READY/EMPTY/ERROR status (2026-09-15, external review).

Before this fix, a pool key was either SET (candidates found) or DELETED
(none found) -- both a genuinely empty pool and a poll() that crashed before
reaching this symbol's score looked identical downstream: key absent. Each
pool now also publishes a companion "<key>:status" JSON payload so a
consumer can tell READY / EMPTY / ERROR apart.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.market_data.ltp_poller import LTPPoller
from src.core.constants import (
    REDIS_ACTIVE_FNO_SYMBOLS,
    REDIS_TOP_SYMBOLS_KEY,
    REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
    REDIS_TOP_SYMBOLS_IRON_CONDOR,
    REDIS_TOP_SYMBOLS_MOMENTUM,
)


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


def _wire_happy_path(poller, monkeypatch, score_fn):
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
    monkeypatch.setattr(poller, "_score_all", score_fn)


@pytest.mark.asyncio
async def test_pool_status_is_ready_when_candidates_found(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["SYM_A"])})
    poller = LTPPoller(redis_client=redis, kite=object(), instrument_tokens={"SYM_A": 1})
    _wire_happy_path(poller, monkeypatch, lambda tick: (10.0, 5.0, 5.0, 30.0))

    await poller.poll()

    for key in (REDIS_TOP_SYMBOLS_KEY, REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
                REDIS_TOP_SYMBOLS_IRON_CONDOR, REDIS_TOP_SYMBOLS_MOMENTUM):
        status = json.loads(redis.store[f"{key}:status"])
        assert status["status"] == "READY"
        assert status["symbols_count"] == 1


@pytest.mark.asyncio
async def test_pool_status_is_empty_not_absent_when_no_candidates_qualify(monkeypatch):
    # Credit spread / iron condor / momentum all score 0 for every symbol --
    # a genuine "nothing qualified today", not a poller failure.
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["SYM_A"])})
    poller = LTPPoller(redis_client=redis, kite=object(), instrument_tokens={"SYM_A": 1})
    _wire_happy_path(poller, monkeypatch, lambda tick: (10.0, 0.0, 0.0, 0.0))

    await poller.poll()

    assert REDIS_TOP_SYMBOLS_CREDIT_SPREAD not in redis.store  # base key still absent (back-compat)
    status = json.loads(redis.store[f"{REDIS_TOP_SYMBOLS_CREDIT_SPREAD}:status"])
    assert status["status"] == "EMPTY"
    assert status["symbols_count"] == 0
    assert "reason" in status and status["reason"]

    status_momentum = json.loads(redis.store[f"{REDIS_TOP_SYMBOLS_MOMENTUM}:status"])
    assert status_momentum["status"] == "EMPTY"

    # EMA pool always scores (no floor gate) -- confirms this test's "0 score
    # everywhere" setup only produces EMPTY for the gated pools, not EMA.
    status_ema = json.loads(redis.store[f"{REDIS_TOP_SYMBOLS_KEY}:status"])
    assert status_ema["status"] == "READY"


@pytest.mark.asyncio
async def test_pool_status_is_error_when_poll_setup_fails_before_scoring(monkeypatch):
    # A failure in _refresh_active_symbols()/_prefetch_stale_histories() --
    # before any symbol is even scored -- must mark every pool ERROR with
    # the real reason, not leave pools looking like "zero candidates today".
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({
        REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["SYM_A"]),
        # Pre-seed a base key with a stale-but-real prior result, to confirm
        # the ERROR path deliberately leaves it alone rather than wiping it.
        REDIS_TOP_SYMBOLS_KEY: json.dumps(["STALE_SYM"]),
    })
    poller = LTPPoller(redis_client=redis, kite=object(), instrument_tokens={"SYM_A": 1})

    async def _boom(*a, **kw):
        raise ConnectionError("kite historical_data timeout")

    monkeypatch.setattr(poller, "_prefetch_stale_histories", _boom)

    with pytest.raises(ConnectionError):
        await poller.poll()

    for key in (REDIS_TOP_SYMBOLS_KEY, REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
                REDIS_TOP_SYMBOLS_IRON_CONDOR, REDIS_TOP_SYMBOLS_MOMENTUM):
        status = json.loads(redis.store[f"{key}:status"])
        assert status["status"] == "ERROR"
        assert "kite historical_data timeout" in status["reason"]

    # Stale-but-real prior data must survive an ERROR cycle untouched --
    # better than the engine suddenly seeing zero candidates from one bad poll.
    assert json.loads(redis.store[REDIS_TOP_SYMBOLS_KEY]) == ["STALE_SYM"]
