"""
refresh_nifty_regime_inputs() (2026-09-15, external review): real NIFTY 50
ATR14%/EMA20-50-spread%, replacing the cross-sectional 40-stock average
proxy as MarketRegimeDetector's global-regime input. Blends a historical
5-min baseline (kite.historical_data(), reliable for anything not-today)
with the live, WebSocket-accumulated bars in market:nifty_tick (written by
ZerodhaTicker._handle_nifty_tick()).
"""
import json
from datetime import datetime, timedelta

import pytest

from src.market_data.regime_detector import (
    refresh_nifty_regime_inputs, REDIS_NIFTY_REGIME_INPUTS_KEY,
)


class _FakeRedis:
    def __init__(self, store=None):
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _historical_bars(n=60, base=24000.0, trend_per_bar=0.0):
    """Synthetic 5-min bars, all dated "yesterday" or earlier so none get
    dropped by the today-exclusion filter."""
    start = datetime.now() - timedelta(days=5)
    bars = []
    price = base
    for i in range(n):
        price += trend_per_bar
        bars.append({
            "date": start + timedelta(minutes=5 * i),
            "open": price, "high": price + 5, "low": price - 5, "close": price,
        })
    return bars


class _FakeKite:
    def __init__(self, bars):
        self._bars = bars

    def historical_data(self, token, from_date, to_date, interval):
        return self._bars


@pytest.mark.asyncio
async def test_publishes_real_atr_and_ema_spread_from_historical_baseline():
    kite = _FakeKite(_historical_bars(n=60, trend_per_bar=2.0))  # steadily rising -> real EMA spread
    redis = _FakeRedis({})

    ok = await refresh_nifty_regime_inputs(kite, redis, nifty_token=256265)

    assert ok is True
    published = json.loads(redis.store[REDIS_NIFTY_REGIME_INPUTS_KEY])
    assert published["atr_pct_daily"] > 0
    assert published["ema_spread_pct"] > 0
    assert published["close"] > 0
    assert "timestamp" in published


# ── market_direction (2026-09-16, external review round 2) ─────────────────
# atr_pct_daily/ema_spread_pct only ever measure magnitude -- a strongly
# bullish and a strongly bearish NIFTY produce the identical classification
# inputs. market_direction answers the separate question of which way.

@pytest.mark.asyncio
async def test_market_direction_is_bullish_when_price_above_both_emas_in_order():
    kite = _FakeKite(_historical_bars(n=60, trend_per_bar=3.0))  # steadily rising
    redis = _FakeRedis({})

    await refresh_nifty_regime_inputs(kite, redis, nifty_token=256265)

    published = json.loads(redis.store[REDIS_NIFTY_REGIME_INPUTS_KEY])
    assert published["market_direction"] == "BULLISH"


@pytest.mark.asyncio
async def test_market_direction_is_bearish_when_price_below_both_emas_in_order():
    kite = _FakeKite(_historical_bars(n=60, trend_per_bar=-3.0))  # steadily falling
    redis = _FakeRedis({})

    await refresh_nifty_regime_inputs(kite, redis, nifty_token=256265)

    published = json.loads(redis.store[REDIS_NIFTY_REGIME_INPUTS_KEY])
    assert published["market_direction"] == "BEARISH"


@pytest.mark.asyncio
async def test_market_direction_is_neutral_when_flat():
    kite = _FakeKite(_historical_bars(n=60, trend_per_bar=0.0))  # flat -- close ~= ema20 ~= ema50
    redis = _FakeRedis({})

    await refresh_nifty_regime_inputs(kite, redis, nifty_token=256265)

    published = json.loads(redis.store[REDIS_NIFTY_REGIME_INPUTS_KEY])
    assert published["market_direction"] == "NEUTRAL"


@pytest.mark.asyncio
async def test_blends_live_tick_accumulated_bars_not_ignored():
    # market:nifty_tick (written by ZerodhaTicker) carries real intraday
    # bars -- must be included, not just the historical baseline.
    kite = _FakeKite(_historical_bars(n=60))
    live_bars_today = [
        {"date": datetime.now().isoformat(), "open": 24500.0, "high": 24550.0, "low": 24480.0, "close": 24490.0},
    ]
    redis = _FakeRedis({"market:nifty_tick": json.dumps({
        "symbol": "NIFTY_50_INDEX", "close": 24490.0,
        "bars_today": live_bars_today, "cur_bar_open": None,
    })})

    ok = await refresh_nifty_regime_inputs(kite, redis, nifty_token=256265)

    assert ok is True
    published = json.loads(redis.store[REDIS_NIFTY_REGIME_INPUTS_KEY])
    # close should reflect the LAST bar in the blended series (today's live bar)
    assert published["close"] == pytest.approx(24490.0)


@pytest.mark.asyncio
async def test_returns_false_and_does_not_publish_when_no_token():
    redis = _FakeRedis({})
    ok = await refresh_nifty_regime_inputs(_FakeKite([]), redis, nifty_token=None)
    assert ok is False
    assert REDIS_NIFTY_REGIME_INPUTS_KEY not in redis.store


@pytest.mark.asyncio
async def test_returns_false_and_does_not_publish_when_no_kite():
    redis = _FakeRedis({})
    ok = await refresh_nifty_regime_inputs(None, redis, nifty_token=256265)
    assert ok is False
    assert REDIS_NIFTY_REGIME_INPUTS_KEY not in redis.store


@pytest.mark.asyncio
async def test_returns_false_when_insufficient_bars():
    kite = _FakeKite(_historical_bars(n=10))  # well under the 50-bar floor
    redis = _FakeRedis({})

    ok = await refresh_nifty_regime_inputs(kite, redis, nifty_token=256265)

    assert ok is False
    assert REDIS_NIFTY_REGIME_INPUTS_KEY not in redis.store


@pytest.mark.asyncio
async def test_drops_todays_own_bars_from_historical_data_before_blending():
    # Zerodha confirmed historical_data() lags same-day intraday candles by
    # 5+ hours -- a "today" bar sneaking in from historical_data() must be
    # excluded; only the live-tick feed should represent today.
    yesterday_bars = _historical_bars(n=55)
    today_bad_bar = {
        "date": datetime.now(), "open": 99999.0, "high": 99999.0, "low": 99999.0, "close": 99999.0,
    }
    kite = _FakeKite(yesterday_bars + [today_bad_bar])
    redis = _FakeRedis({})

    ok = await refresh_nifty_regime_inputs(kite, redis, nifty_token=256265)

    assert ok is True
    published = json.loads(redis.store[REDIS_NIFTY_REGIME_INPUTS_KEY])
    assert published["close"] != 99999.0


@pytest.mark.asyncio
async def test_returns_false_and_does_not_raise_on_kite_exception():
    class _BrokenKite:
        def historical_data(self, *a, **kw):
            raise ConnectionError("kite timeout")

    redis = _FakeRedis({})
    ok = await refresh_nifty_regime_inputs(_BrokenKite(), redis, nifty_token=256265)
    assert ok is False
    assert REDIS_NIFTY_REGIME_INPUTS_KEY not in redis.store


# ── LiveTradingEngine._maybe_refresh_nifty_regime_inputs() ─────────────────
# Lazy "check cache first, refresh only on miss" wiring -- same pattern
# _get_cached_vix() already uses for VIX, avoiding a kite.historical_data()
# call every single 1-minute signal cycle.

from types import SimpleNamespace
from src.live_trading.live_trading_engine import LiveTradingEngine


def _fake_engine(redis, kite, nifty_token):
    return SimpleNamespace(
        _maybe_refresh_nifty_regime_inputs=LiveTradingEngine._maybe_refresh_nifty_regime_inputs,
        _redis=redis, _kite=kite, _nifty_instrument_token=nifty_token,
    )


@pytest.mark.asyncio
async def test_maybe_refresh_skips_when_cache_still_warm():
    calls = []

    class _TrackedKite:
        def historical_data(self, *a, **kw):
            calls.append(1)
            return _historical_bars(60)

    redis = _FakeRedis({REDIS_NIFTY_REGIME_INPUTS_KEY: json.dumps({"atr_pct_daily": 1.0})})
    fake = _fake_engine(redis, _TrackedKite(), 256265)

    await fake._maybe_refresh_nifty_regime_inputs(fake)

    assert calls == [], "must not re-fetch while the cache is still warm"


@pytest.mark.asyncio
async def test_maybe_refresh_fetches_when_cache_missing():
    kite = _FakeKite(_historical_bars(60))
    redis = _FakeRedis({})
    fake = _fake_engine(redis, kite, 256265)

    await fake._maybe_refresh_nifty_regime_inputs(fake)

    assert REDIS_NIFTY_REGIME_INPUTS_KEY in redis.store


@pytest.mark.asyncio
async def test_maybe_refresh_is_a_noop_without_nifty_token():
    kite = _FakeKite(_historical_bars(60))
    redis = _FakeRedis({})
    fake = _fake_engine(redis, kite, None)

    await fake._maybe_refresh_nifty_regime_inputs(fake)  # must not raise

    assert REDIS_NIFTY_REGIME_INPUTS_KEY not in redis.store


def test_set_nifty_instrument_token_stores_it():
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.set_nifty_instrument_token(256265)
    assert engine._nifty_instrument_token == 256265
