"""
NIFTY/BANKNIFTY added as EXTRA ema_crossover_v1/momentum_v1 candidates
(2026-11-11, explicit user instruction) -- scoped to those two strategies
only, credit_spread_v1/iron_condor_v1 must never see them.

Key facts this feature rests on, verified via WebSearch before implementing
(not guessed): SEBI's November 2024 one-weekly-index-per-exchange rule left
BANKNIFTY monthly-only (reuses the existing monthly expiry path, same as
every stock); NIFTY kept its weekly expiry but the day moved Thursday ->
Tuesday effective 2026-09-01 (same weekday its monthly expiry already uses).
Lot sizes (NIFTY 65, BANKNIFTY 30) verified against NSE's January 2026
revision circular.
"""
from datetime import datetime, timedelta

import pytest

from src.core.constants import (
    FNO_LOT_SIZES,
    FNO_SECTORS,
    FNO_STRIKE_INTERVALS,
    INDEX_FNO_SYMBOLS,
    INDEX_SPOT_TRADINGSYMBOLS,
    WEEKLY_EXPIRY_INDEX_SYMBOLS,
)
from src.core.utils import (
    build_weekly_option_symbol,
    get_near_weekly_expiry,
    get_weekly_entry_expiry,
)


# ── Constants ────────────────────────────────────────────────────────────────

def test_only_nifty_and_banknifty_are_index_symbols():
    assert set(INDEX_FNO_SYMBOLS) == {"NIFTY", "BANKNIFTY"}


def test_only_nifty_has_weekly_expiry():
    # BANKNIFTY lost its weekly expiry in SEBI's November 2024 rule.
    assert WEEKLY_EXPIRY_INDEX_SYMBOLS == frozenset({"NIFTY"})
    assert "BANKNIFTY" not in WEEKLY_EXPIRY_INDEX_SYMBOLS


def test_index_spot_tradingsymbols_match_zerodha_convention():
    assert INDEX_SPOT_TRADINGSYMBOLS["NIFTY"] == "NIFTY 50"
    assert INDEX_SPOT_TRADINGSYMBOLS["BANKNIFTY"] == "NIFTY BANK"


def test_index_lot_sizes_and_strike_intervals_and_sectors_present():
    for sym in INDEX_FNO_SYMBOLS:
        assert FNO_LOT_SIZES[sym] > 0
        assert FNO_STRIKE_INTERVALS[sym] > 0
        assert FNO_SECTORS[sym] == "Index"
    assert FNO_LOT_SIZES["NIFTY"] == 65
    assert FNO_LOT_SIZES["BANKNIFTY"] == 30


# ── get_near_weekly_expiry() / get_weekly_entry_expiry() ───────────────────────

def test_near_weekly_expiry_is_always_a_tuesday():
    expiry = get_near_weekly_expiry()
    assert expiry.weekday() == 1  # Tuesday


def test_near_weekly_expiry_is_within_the_next_7_days():
    expiry = get_near_weekly_expiry()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    delta = (expiry.replace(hour=0, minute=0, second=0, microsecond=0) - today).days
    assert 0 <= delta <= 6


def test_weekly_entry_expiry_rolls_forward_by_whole_weeks_until_min_dte_met():
    near = get_near_weekly_expiry()
    near_dte = (near - datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)).days
    # Ask for more runway than the nearest week can offer -- must roll
    # forward in exact 7-day increments, landing on a later Tuesday.
    entry = get_weekly_entry_expiry(min_dte=near_dte + 3)
    assert entry.weekday() == 1
    assert entry > near
    assert (entry - near).days % 7 == 0


def test_weekly_entry_expiry_matches_near_expiry_when_runway_is_sufficient():
    entry = get_weekly_entry_expiry(min_dte=0)
    assert entry == get_near_weekly_expiry()


# ── build_weekly_option_symbol() ───────────────────────────────────────────────

def test_build_weekly_option_symbol_matches_kite_connect_documented_format():
    # Verified via WebSearch against Kite Connect's own forum examples
    # (2026-11-11): a Dec 17 weekly 13600 CE is "NIFTY20D1713600CE".
    expiry = datetime(2020, 12, 17)
    assert build_weekly_option_symbol("NIFTY", 13600, "CE", expiry) == "NIFTY20D1713600CE"


def test_build_weekly_option_symbol_month_codes_1_through_9_and_ond():
    cases = {
        1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9",
        10: "O", 11: "N", 12: "D",
    }
    for month, code in cases.items():
        expiry = datetime(2026, month, 7)
        sym = build_weekly_option_symbol("NIFTY", 25000, "PE", expiry)
        assert sym == f"NIFTY26{code}0725000PE", f"month {month}: expected code {code}"


def test_build_weekly_option_symbol_keeps_fractional_strike():
    expiry = datetime(2026, 11, 17)
    sym = build_weekly_option_symbol("NIFTY", 24987.5, "CE", expiry)
    assert sym == "NIFTY26N1724987.5CE"


# ── LTPPoller scoping: index symbols eligible for ema/momentum ONLY ─────────
# (2026-11-11, explicit user instruction: "add NIFTY/BankNifty as extra
# symbols these [ema_crossover_v1/momentum_v1] strategies can also trade")

import json

import pandas as pd
import pytest as _pytest

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


@_pytest.mark.asyncio
async def test_index_symbols_are_always_polled_regardless_of_liquidity_universe(monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["SOME_STOCK"])})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={"SOME_STOCK": 1, "NIFTY": 2, "BANKNIFTY": 3},
    )
    await poller._refresh_active_symbols()

    assert "NIFTY" in poller.symbols
    assert "BANKNIFTY" in poller.symbols
    # Not liquidity-ranked -- indices must never appear in _active_set itself,
    # only in the broader self.symbols union (see _index_set's docstring).
    assert "NIFTY" not in poller._active_set
    assert "BANKNIFTY" not in poller._active_set


@_pytest.mark.asyncio
async def test_index_symbols_compete_for_ema_and_momentum_pools_only(monkeypatch):
    """The core scoping guarantee: a real score for ALL four strategies'
    theses must still only land an index symbol in ema/momentum, never
    credit_spread/iron_condor -- credit_spread_v1/iron_condor_v1 were
    explicitly excluded from this feature. A real stock (SOME_STOCK) shares
    the exact same score profile, so its presence in every pool confirms
    the scoring/publishing pipeline genuinely ran, not that the pools were
    empty for an unrelated reason."""
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    redis = _FakePollRedis({REDIS_ACTIVE_FNO_SYMBOLS: json.dumps(["SOME_STOCK"])})
    poller = LTPPoller(
        redis_client=redis, kite=object(),
        instrument_tokens={"SOME_STOCK": 1, "NIFTY": 2, "BANKNIFTY": 3},
    )
    hist = _valid_history()

    async def _fake_get_history(symbol, loop):
        return hist.copy()

    async def _fake_get_history_15m(symbol, loop):
        return None

    async def _fake_read_day_range(symbol):
        return None

    # A strong positive score on EVERY strategy's thesis simultaneously --
    # if the exclusion guard weren't there, this would land NIFTY/BANKNIFTY
    # in all 4 pools, same as SOME_STOCK.
    def _fake_score_all(tick):
        return 10.0, 10.0, 10.0, 10.0

    monkeypatch.setattr(poller, "_get_history", _fake_get_history)
    monkeypatch.setattr(poller, "_get_history_15m", _fake_get_history_15m)
    monkeypatch.setattr(poller, "_read_day_range", _fake_read_day_range)
    monkeypatch.setattr(poller, "_score_all", _fake_score_all)

    await poller.poll()

    ema_pool = json.loads(redis.store[REDIS_TOP_SYMBOLS_KEY])
    momentum_pool = json.loads(redis.store[REDIS_TOP_SYMBOLS_MOMENTUM])
    assert "NIFTY" in ema_pool and "BANKNIFTY" in ema_pool
    assert "NIFTY" in momentum_pool and "BANKNIFTY" in momentum_pool

    # credit_spread/iron_condor pools must contain the real stock (proving
    # the pipeline ran and scored everyone) but NEVER the two index symbols.
    spread_pool = json.loads(redis.store[REDIS_TOP_SYMBOLS_CREDIT_SPREAD])
    condor_pool = json.loads(redis.store[REDIS_TOP_SYMBOLS_IRON_CONDOR])
    assert "SOME_STOCK" in spread_pool
    assert "SOME_STOCK" in condor_pool
    assert "NIFTY" not in spread_pool and "BANKNIFTY" not in spread_pool
    assert "NIFTY" not in condor_pool and "BANKNIFTY" not in condor_pool


# ── _process_signal(): weekly expiry for NIFTY, monthly (unchanged) for ────
# BANKNIFTY, RS-ranker bypass for both ───────────────────────────────────────

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.core.enums import SignalType


class _FakeIndexEngine:
    _audit_gate = LiveTradingEngine._audit_gate
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _ELEVATED_VIX_ENTRY_CEILING = LiveTradingEngine._ELEVATED_VIX_ENTRY_CEILING

    def __init__(self, market_data, rs_ranker=None):
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {}
        self._exited_today = set()
        self._max_daily_orders = 0
        self._max_concurrent_intraday = 999
        self._last_signal_date = {}
        self._signal_gate_stats = {}
        self._last_gate_rejection = None
        self._close_option_positions = AsyncMock()
        self._has_open_option = AsyncMock(return_value=False)
        self._get_market_data = AsyncMock(return_value=market_data)
        # None -> _process_signal's "no verified real lot size" fail-closed
        # path, stopping the pipeline right after lot_passed/rs_passed --
        # these tests are only about the DTE/RS gates earlier in the
        # function, not the full entry (strike selection, order placement).
        self._get_lot_size = AsyncMock(return_value=None)
        self._redis = None
        self.rs_ranker = rs_ranker


def _index_strategy(name, **overrides):
    return SimpleNamespace(
        name=name, is_active=True, min_dte=10, max_dte=999,
        generate_signal=lambda market_data: SignalType.BUY,
        **overrides,
    )


@pytest.mark.asyncio
async def test_nifty_entry_resolves_a_weekly_tuesday_expiry_and_passes_dte_gate():
    fake = _FakeIndexEngine({
        "close": 25000.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    })
    strategy = _index_strategy("ema_crossover_v1")

    await LiveTradingEngine._process_signal(fake, strategy, "NIFTY", vix=15.0, regime="TRENDING")

    # Reaching dte_passed proves the weekly path resolved a real expiry
    # within [weekly_min_dte, max_dte] without ever touching
    # get_near_month_expiry()'s monthly logic.
    assert fake._signal_gate_stats.get("ema_crossover_v1", {}).get("dte_passed", 0) >= 1


@pytest.mark.asyncio
async def test_banknifty_entry_uses_the_unchanged_monthly_path():
    # BANKNIFTY lost its weekly expiry (SEBI, Nov 2024) -- reuses the exact
    # same monthly min_dte=10 default every stock uses, not a weekly one.
    fake = _FakeIndexEngine({
        "close": 55000.0, "ltp_source": "live_tick",
        "rvol": 2.0, "rvol_valid": True,
        "adx14": 30.0, "adx_valid": True,
    })
    strategy = _index_strategy("ema_crossover_v1")

    await LiveTradingEngine._process_signal(fake, strategy, "BANKNIFTY", vix=15.0, regime="TRENDING")

    assert fake._signal_gate_stats.get("ema_crossover_v1", {}).get("dte_passed", 0) >= 1


@pytest.mark.asyncio
async def test_rs_ranker_gate_is_bypassed_for_nifty_even_when_ranks_would_reject_it():
    # An RS-rank list that would normally fail-closed on BUY (symbol not in
    # top-10) -- must be irrelevant for NIFTY, which skips this gate entirely.
    _ranks = [{"symbol": f"STOCK{i}", "rank": i} for i in range(20)]
    fake_rs_ranker = SimpleNamespace(get_ranks=AsyncMock(return_value=_ranks))
    fake = _FakeIndexEngine(
        {
            "close": 25000.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        },
        rs_ranker=fake_rs_ranker,
    )
    strategy = _index_strategy("ema_crossover_v1", require_rs=True)

    await LiveTradingEngine._process_signal(fake, strategy, "NIFTY", vix=15.0, regime="TRENDING")

    assert fake._signal_gate_stats.get("ema_crossover_v1", {}).get("rs_passed", 0) >= 1
    fake_rs_ranker.get_ranks.assert_not_awaited()


@pytest.mark.asyncio
async def test_rs_ranker_gate_still_applies_to_ordinary_stocks(monkeypatch):
    # Guard against over-fixing: a real stock not in the RS top-10 must
    # still be blocked -- the bypass is index-symbol-specific.
    import datetime as _dt
    import src.live_trading.live_trading_engine as lte_module
    monkeypatch.setattr(lte_module, "now_ist", lambda: _dt.datetime(2026, 10, 15, 10, 0, 0))

    _ranks = [{"symbol": f"STOCK{i}", "rank": i} for i in range(20)]
    fake_rs_ranker = SimpleNamespace(get_ranks=AsyncMock(return_value=_ranks))
    fake = _FakeIndexEngine(
        {
            "close": 100.0, "ltp_source": "live_tick",
            "rvol": 2.0, "rvol_valid": True,
            "adx14": 30.0, "adx_valid": True,
        },
        rs_ranker=fake_rs_ranker,
    )
    strategy = _index_strategy("ema_crossover_v1", require_rs=True)

    await LiveTradingEngine._process_signal(fake, strategy, "NOT_IN_RANKS", vix=15.0, regime="TRENDING")

    assert fake._last_gate_rejection is not None
    assert fake._last_gate_rejection["reason"] == "NOT_IN_RS_TOP_10"
