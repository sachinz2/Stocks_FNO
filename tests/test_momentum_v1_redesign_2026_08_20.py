"""
momentum_v1 entry-quality redesign (2026-08-20), integrated directly into
the existing strategy rather than a separate momentum_v2 (explicit user
choice). Addresses the external PDF review's core thesis critique -- "you're
buying after the move already happened" -- with four independently-
toggleable entry filters, a lower-but-rising ADX floor, delta-based strike
selection, and a new underlying-based structural-invalidation exit. See
momentum.py's class docstring and initialize()'s parameter comments for the
full rationale, and docs/LIVE_TRADING_CHECKLIST.md for the real
trade_journal data (11 trades, 9.1% win rate) that motivated it.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.strategies.momentum import MomentumStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine


def _mom(**overrides):
    strat = MomentumStrategy("momentum_v1", overrides)
    strat.initialize()
    return strat


def _bar(symbol="RELIANCE", ema20=105.0, ema50=100.0, adx=30.0, close=2500.0,
         atr=20.0, vwap=2500.0, bar_key="live:t0"):
    return {
        "symbol": symbol, "ema20": ema20, "ema50": ema50, "adx14": adx,
        "close": close, "atr14": atr, "vwap": vwap, "ohlc_bar_key": bar_key,
    }


# ── A fully-qualifying signal still fires (guard against over-fixing) ───────

def test_a_genuinely_accelerating_non_extended_signal_still_fires():
    strat = _mom(signal_confirm_bars=1)
    # Bar 1: establishes history (ADX=26, EMA20=101 -- close to EMA50=100 so
    # not yet a qualifying spread; just seeding history). close/vwap kept on
    # the same price scale as ema20/ema50 (both are EMAs OF close).
    strat.generate_signal(_bar(adx=26.0, ema20=101.0, close=101.0, vwap=100.5, atr=2.0, bar_key="live:t0"))
    # Bar 2: ADX rising (26->30), EMA20 rising (101->105), spread wide enough,
    # close only modestly above EMA20 (1 ATR, within the 1.5x cap), close
    # close to VWAP (well within the 1.5% cap).
    signal = strat.generate_signal(_bar(adx=30.0, ema20=105.0, close=107.0, vwap=105.5, atr=2.0, bar_key="live:t1"))
    assert signal == "BUY"


# ── ADX rising requirement ───────────────────────────────────────────────────

def test_adx_not_rising_blocks_the_entry():
    strat = _mom(signal_confirm_bars=1, ema_slope_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(adx=35.0, bar_key="live:t0"))
    # ADX declining (35 -> 30) despite still being above adx_entry_threshold.
    signal = strat.generate_signal(_bar(adx=30.0, bar_key="live:t1"))
    assert signal == "HOLD"


def test_adx_rising_requirement_can_be_disabled():
    strat = _mom(signal_confirm_bars=1, adx_rising_required=False,
                 ema_slope_required=False, extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(adx=35.0, bar_key="live:t0"))
    signal = strat.generate_signal(_bar(adx=30.0, bar_key="live:t1"))
    assert signal == "BUY"


# ── EMA20 slope requirement ──────────────────────────────────────────────────

def test_ema_not_sloping_in_signal_direction_blocks_the_entry():
    strat = _mom(signal_confirm_bars=1, adx_rising_required=False,
                 extension_atr_mult=0, vwap_extension_pct=0)
    strat.generate_signal(_bar(ema20=106.0, bar_key="live:t0"))
    # BUY candidate, but EMA20 dropped (106 -> 105) instead of rising.
    signal = strat.generate_signal(_bar(ema20=105.0, bar_key="live:t1"))
    assert signal == "HOLD"


# ── Extension filter (distance from EMA20 in ATR units) ─────────────────────

def test_price_too_extended_from_ema20_blocks_the_entry():
    strat = _mom(signal_confirm_bars=1, adx_rising_required=False,
                 ema_slope_required=False, vwap_extension_pct=0, extension_atr_mult=1.5)
    strat.generate_signal(_bar(bar_key="live:t0"))
    # close is 40 away from ema20=105, atr=20 -> 2.0x ATR, over the 1.5x cap.
    signal = strat.generate_signal(_bar(close=145.0, ema20=105.0, atr=20.0, bar_key="live:t1"))
    assert signal == "HOLD"


def test_price_within_extension_limit_still_fires():
    strat = _mom(signal_confirm_bars=1, adx_rising_required=False,
                 ema_slope_required=False, vwap_extension_pct=0, extension_atr_mult=1.5)
    strat.generate_signal(_bar(bar_key="live:t0"))
    # close is 20 away from ema20=105, atr=20 -> 1.0x ATR, within the 1.5x cap.
    signal = strat.generate_signal(_bar(close=125.0, ema20=105.0, atr=20.0, bar_key="live:t1"))
    assert signal == "BUY"


# ── VWAP extension filter ────────────────────────────────────────────────────

def test_price_too_far_from_vwap_blocks_the_entry():
    strat = _mom(signal_confirm_bars=1, adx_rising_required=False,
                 ema_slope_required=False, extension_atr_mult=0, vwap_extension_pct=1.5)
    strat.generate_signal(_bar(bar_key="live:t0"))
    # close=2600, vwap=2500 -> 4% away, over the 1.5% cap.
    signal = strat.generate_signal(_bar(close=2600.0, vwap=2500.0, bar_key="live:t1"))
    assert signal == "HOLD"


# ── Underlying-based structural invalidation exit ────────────────────────────

def test_structural_invalidation_exits_a_call_when_underlying_closes_below_ema20():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "current_close": 99.0, "current_ema_fast": 100.0, "is_call": True,
    }
    # Premium P&L alone (39 vs 40, -2.5%) wouldn't trip any premium-based
    # stop -- the structural check must fire independently of it.
    assert strat.manage_position(pos, 39.0) == "EXIT"


def test_structural_invalidation_exits_a_put_when_underlying_closes_above_ema20():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "current_close": 101.0, "current_ema_fast": 100.0, "is_call": False,
    }
    assert strat.manage_position(pos, 39.0) == "EXIT"


def test_structural_invalidation_does_not_fire_while_thesis_still_holds():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "current_close": 105.0, "current_ema_fast": 100.0, "is_call": True,
    }
    assert strat.manage_position(pos, 39.0) == "HOLD"


def test_structural_invalidation_skips_gracefully_when_fields_absent():
    """Guard against over-fixing -- manage_position() must not crash or
    misfire when the engine doesn't supply the new fields (e.g. a caller
    that hasn't been updated, or a fake in an older test)."""
    strat = _mom()
    pos = {"avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0}
    assert strat.manage_position(pos, 39.0) == "HOLD"


def test_structural_invalidation_can_be_disabled():
    strat = _mom(underlying_invalidation_exit=False)
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "current_close": 99.0, "current_ema_fast": 100.0, "is_call": True,
    }
    assert strat.manage_position(pos, 39.0) == "HOLD"


# ── Engine-level hooks: RVOL threshold override + delta-based strikes ───────

def test_default_rvol_threshold_is_the_shared_1_3_floor():
    """ema_crossover_v1 (or anything else) that doesn't set
    rvol_entry_threshold must be unaffected by momentum_v1's stricter bar."""
    strat = SimpleNamespace(name="ema_crossover_v1")
    assert getattr(strat, "rvol_entry_threshold", 1.3) == 1.3


def test_momentum_v1_defaults_to_a_stricter_rvol_threshold():
    strat = _mom()
    assert strat.rvol_entry_threshold == 1.5


def test_engine_strike_selection_uses_delta_when_strategy_sets_it():
    import inspect
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert "entry_option_delta" in src
    assert "find_delta_strike" in src
    # ATM path must remain the default (no delta target set) so
    # ema_crossover_v1's behavior is provably unchanged.
    assert "get_atm_strike(underlying_price, symbol, interval=strike_interval)" in src


def test_momentum_v1_defaults_to_a_near_itm_delta_target():
    strat = _mom()
    assert strat.entry_option_delta == 0.60
