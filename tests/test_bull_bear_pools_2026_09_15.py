"""
Bull/bear candidate pool split for EMA and Momentum (2026-09-15, external
review). Candidates used to be ranked by a single absolute-value score with
no regard for direction -- on a day dominated by one direction, unrelated
noise on the OTHER side could still occupy top-N slots, crowding out
genuine same-direction candidates. Each side now gets its own full top-N;
the engine reads the union of both.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.market_data.ltp_poller import LTPPoller
from src.core.constants import (
    REDIS_ACTIVE_FNO_SYMBOLS,
    REDIS_TOP_SYMBOLS_EMA_BULL, REDIS_TOP_SYMBOLS_EMA_BEAR,
    REDIS_TOP_SYMBOLS_MOMENTUM_BULL, REDIS_TOP_SYMBOLS_MOMENTUM_BEAR,
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


@pytest.mark.asyncio
async def test_a_strongly_one_sided_day_fills_both_dedicated_pools_not_just_one(monkeypatch):
    # 4 bearish stocks (established downtrend, high ADX) + 1 mediocre
    # bullish stock. Under the old single top-5 ranking, if the bullish one
    # happened to score higher it could still take a slot the bearish side
    # "deserved" more on a day this one-sided. With split pools, momentum
    # bear pool gets its own full top-N regardless.
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    symbols = ["BEAR1", "BEAR2", "BEAR3", "BEAR4", "BULL1"]
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
        return None

    # ema20 < ema50 for BEAR*, ema20 > ema50 for BULL1
    def _fake_enrich(symbol, df, ltp, live_range=None):
        bearish = symbol.startswith("BEAR")
        return {
            "close": ltp,
            "ema20": 99.0 if bearish else 101.0,
            "ema50": 101.0 if bearish else 99.0,
            "ema_spread_pct": 2.0,
        }

    def _fake_score_all(tick):
        # everyone qualifies for momentum (m > 0); BULL1 scores highest
        is_bull = tick["ema20"] > tick["ema50"]
        m = 5.0 if is_bull else 4.0
        return 0.0, 0.0, 0.0, m

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)
    monkeypatch.setattr(poller, "_enrich", staticmethod(_fake_enrich))
    monkeypatch.setattr(poller, "_score_all", staticmethod(_fake_score_all))

    await poller.poll()

    bull_pool = json.loads(redis.store[REDIS_TOP_SYMBOLS_MOMENTUM_BULL])
    bear_pool = json.loads(redis.store[REDIS_TOP_SYMBOLS_MOMENTUM_BEAR])
    assert bull_pool == ["BULL1"]
    assert sorted(bear_pool) == ["BEAR1", "BEAR2", "BEAR3", "BEAR4"], (
        "all 4 bearish candidates must get their own pool, not compete "
        "against BULL1 for a single shared top-N"
    )


@pytest.mark.asyncio
async def test_ema_pool_also_splits_by_current_ema_relationship(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    symbols = ["UP", "DOWN"]
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(symbols)})
    poller = LTPPoller(redis_client=redis, kite=object(), instrument_tokens={"UP": 1, "DOWN": 2})
    hist = _valid_history()

    async def _fake_get_history(symbol, loop):
        return hist.copy()

    async def _fake_get_history_15m(symbol, loop):
        return None

    async def _fake_read_day_range(symbol):
        return None

    def _fake_enrich(symbol, df, ltp, live_range=None):
        return {
            "close": ltp,
            "ema20": 101.0 if symbol == "UP" else 99.0,
            "ema50": 99.0 if symbol == "UP" else 101.0,
            "ema_spread_pct": 0.3,
        }

    def _fake_score_all(tick):
        return 5.0, 0.0, 0.0, 0.0

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)
    monkeypatch.setattr(poller, "_enrich", staticmethod(_fake_enrich))
    monkeypatch.setattr(poller, "_score_all", staticmethod(_fake_score_all))

    await poller.poll()

    assert json.loads(redis.store[REDIS_TOP_SYMBOLS_EMA_BULL]) == ["UP"]
    assert json.loads(redis.store[REDIS_TOP_SYMBOLS_EMA_BEAR]) == ["DOWN"]


# ── LiveTradingEngine._get_active_symbols() reads the union of both sides ──

from src.live_trading.live_trading_engine import LiveTradingEngine


class _FakeEngineRedis:
    def __init__(self, store):
        self.store = store

    async def get(self, key):
        return self.store.get(key)


class _FakeEngine:
    # Real class (not SimpleNamespace) so self._get_pool_union(...) inside
    # _get_active_symbols auto-binds via the normal descriptor protocol --
    # an instance-attribute function reference would need self passed
    # explicitly and _get_active_symbols' own try/except would silently
    # swallow the resulting TypeError, masking the whole test.
    _get_active_symbols = LiveTradingEngine._get_active_symbols
    _get_pool_union = LiveTradingEngine._get_pool_union

    def __init__(self, store):
        self._redis = _FakeEngineRedis(store)
        self._symbols = ["FALLBACK"]


def _fake_engine(store):
    return _FakeEngine(store)


class _FakeMomentumStrategy:
    pass


class _FakeEmaStrategy:
    pass


@pytest.mark.asyncio
async def test_get_active_symbols_unions_momentum_bull_and_bear():
    _FakeMomentumStrategy.__name__ = "MomentumStrategy"
    engine = _fake_engine({
        REDIS_TOP_SYMBOLS_MOMENTUM_BULL: json.dumps(["A", "B"]),
        REDIS_TOP_SYMBOLS_MOMENTUM_BEAR: json.dumps(["C", "D"]),
    })
    result = await engine._get_active_symbols(_FakeMomentumStrategy())
    assert result == ["A", "B", "C", "D"]


@pytest.mark.asyncio
async def test_get_active_symbols_unions_ema_bull_and_bear():
    engine = _fake_engine({
        REDIS_TOP_SYMBOLS_EMA_BULL: json.dumps(["A"]),
        REDIS_TOP_SYMBOLS_EMA_BEAR: json.dumps(["B"]),
    })
    result = await engine._get_active_symbols(_FakeEmaStrategy())
    assert result == ["A", "B"]


@pytest.mark.asyncio
async def test_get_active_symbols_dedupes_a_symbol_appearing_in_both_sides():
    # Shouldn't happen in practice (a symbol is either bull or bear per
    # cycle), but the union must be defensive regardless.
    engine = _fake_engine({
        REDIS_TOP_SYMBOLS_EMA_BULL: json.dumps(["A", "B"]),
        REDIS_TOP_SYMBOLS_EMA_BEAR: json.dumps(["B", "C"]),
    })
    result = await engine._get_active_symbols(_FakeEmaStrategy())
    assert result == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_get_active_symbols_falls_back_to_full_universe_when_both_sides_empty():
    engine = _fake_engine({})
    result = await engine._get_active_symbols(_FakeEmaStrategy())
    assert result == ["FALLBACK"]
