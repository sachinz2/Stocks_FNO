"""
EMA crossover event watchlist (2026-09-15, external review).

_get_active_symbols() reads whatever's in the top-N pool at the exact
instant it's called, with no memory of who was a candidate a cycle ago.
ema_score's proximity term drives a stock's score toward its ATR-only floor
within a cycle or two of actually crossing -- so a stock could cross, then
fall out of the top-N before the engine's next ~1-min signal cycle ever
evaluated it. A symbol within _EMA_WATCHLIST_ENTRY_THRESHOLD of a cross now
gets watchlisted and force-included in the published pool for
_EMA_WATCHLIST_BARS cycles regardless of what its score does next.
"""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.market_data.ltp_poller import LTPPoller, _EMA_WATCHLIST_BARS
from src.core.constants import REDIS_ACTIVE_FNO_SYMBOLS, REDIS_TOP_SYMBOLS_KEY


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


def _wire(poller, monkeypatch, ema_spread_by_symbol, score_by_symbol):
    hist = _valid_history()

    async def _fake_get_history(symbol, loop):
        return hist.copy()

    async def _fake_get_history_15m(symbol, loop):
        return None

    async def _fake_read_day_range(symbol):
        return None

    def _fake_enrich(symbol, df, ltp, live_range=None):
        return {"close": ltp, "ema_spread_pct": ema_spread_by_symbol.get(symbol, 999)}

    def _fake_score_all(tick):
        # ema_score only -- other three pools irrelevant to this test
        sym = None
        for s, v in ema_spread_by_symbol.items():
            if tick.get("ema_spread_pct") == v:
                sym = s
        return score_by_symbol.get(sym, 1.0), 0.0, 0.0, 0.0

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)
    monkeypatch.setattr(poller, "_enrich", staticmethod(_fake_enrich))
    monkeypatch.setattr(poller, "_score_all", staticmethod(_fake_score_all))


@pytest.mark.asyncio
async def test_a_stock_that_just_crossed_survives_in_the_pool_via_watchlist(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["PAYTM", "A", "B", "C", "D", "E"])})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={s: i for i, s in enumerate(["PAYTM", "A", "B", "C", "D", "E"])},
    )

    # Cycle 1: PAYTM approaching a cross (spread 0.05%, inside the 0.20%
    # entry band) and scores highly -- naturally in the top-5.
    _wire(poller, monkeypatch,
          ema_spread_by_symbol={"PAYTM": 0.05, "A": 1, "B": 1, "C": 1, "D": 1, "E": 1},
          score_by_symbol={"PAYTM": 10.0, "A": 9, "B": 8, "C": 7, "D": 6, "E": 5})
    await poller.poll()
    assert "PAYTM" in json.loads(redis.store[REDIS_TOP_SYMBOLS_KEY])
    assert poller._ema_watchlist.get("PAYTM") == _EMA_WATCHLIST_BARS

    # Cycle 2: PAYTM just crossed -- spread widened past the proximity cap,
    # score collapsed to near-zero, naturally falls out of top-5 (F now
    # outscores it). Without the watchlist this is exactly the bug: PAYTM
    # disappears from the pool the instant it does the thing the strategy
    # exists to detect.
    _wire(poller, monkeypatch,
          ema_spread_by_symbol={"PAYTM": 0.60, "A": 1, "B": 1, "C": 1, "D": 1, "E": 1, "F": 1},
          score_by_symbol={"PAYTM": 0.1, "A": 9, "B": 8, "C": 7, "D": 6, "E": 5, "F": 4})
    poller.symbols = ["PAYTM", "A", "B", "C", "D", "E", "F"]
    poller._active_set = set(poller.symbols)
    await poller.poll()

    top_ema = json.loads(redis.store[REDIS_TOP_SYMBOLS_KEY])
    assert "PAYTM" in top_ema, "watchlist must keep a just-crossed stock in the pool"
    assert poller._ema_watchlist.get("PAYTM") == _EMA_WATCHLIST_BARS - 1


@pytest.mark.asyncio
async def test_watchlist_entry_expires_after_its_bar_window(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["PAYTM"])})
    poller = LTPPoller(redis_client=redis, kite=object(), instrument_tokens={"PAYTM": 1})
    poller._ema_watchlist = {"PAYTM": 1}  # one cycle left

    _wire(poller, monkeypatch,
          ema_spread_by_symbol={"PAYTM": 5.0},  # far from a cross now, no re-entry
          score_by_symbol={"PAYTM": 1.0})
    await poller.poll()

    assert "PAYTM" not in poller._ema_watchlist, "entry must expire once its bar count reaches 0"


@pytest.mark.asyncio
async def test_watchlist_refreshes_full_window_while_symbol_stays_near_a_cross(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["PAYTM"])})
    poller = LTPPoller(redis_client=redis, kite=object(), instrument_tokens={"PAYTM": 1})
    poller._ema_watchlist = {"PAYTM": 1}  # about to expire

    _wire(poller, monkeypatch,
          ema_spread_by_symbol={"PAYTM": 0.05},  # still hovering near a cross
          score_by_symbol={"PAYTM": 5.0})
    await poller.poll()

    assert poller._ema_watchlist["PAYTM"] == _EMA_WATCHLIST_BARS, (
        "a symbol still within the entry band must get its full window "
        "back, not just tick down toward expiry"
    )


@pytest.mark.asyncio
async def test_force_include_never_crowds_out_a_stronger_natural_candidate(monkeypatch):
    # Regression guard on _publish_pool's own contract: watchlist entries
    # are appended AFTER the natural top-N, never competing for rank.
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    symbols = ["PAYTM", "A", "B", "C", "D", "E"]
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(symbols)})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={s: i for i, s in enumerate(symbols)},
    )
    poller._ema_watchlist = {"PAYTM": 2}

    _wire(poller, monkeypatch,
          ema_spread_by_symbol={"PAYTM": 5.0, "A": 1, "B": 1, "C": 1, "D": 1, "E": 1},
          score_by_symbol={"PAYTM": 0.1, "A": 9, "B": 8, "C": 7, "D": 6, "E": 5})
    await poller.poll()

    top_ema = json.loads(redis.store[REDIS_TOP_SYMBOLS_KEY])
    assert top_ema[:5] == ["A", "B", "C", "D", "E"], "natural top-5 ranking must be untouched"
    assert "PAYTM" in top_ema
