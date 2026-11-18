"""
momentum_v1 exit-path attribution (2026-11-18, data-driven, part of the
pre-2027-01-02-go-live strategy fine-tuning pass).

momentum_v1's losers show very low favorable excursion (option_mfe_pct avg
+1.7%) and real adverse excursion (option_mae_pct avg -6.1%) -- "wrong
almost immediately" -- despite two prior redesign rounds already targeting
late-entry problems. But every one of manage_position()'s ~11 distinct EXIT
branches collapsed to the same generic exit_reason string built once in
live_trading_engine.py's _check_open_option_exits(), making it impossible to
tell from trade_journal which specific check actually fired (e.g. the
underlying stop firing before a pullback-confirmed move even develops, vs a
genuine hard stop). This is purely additive instrumentation -- zero behavior
change, no parameter tuned yet. See manage_position()'s matching comment.
"""
from src.strategies.momentum import MomentumStrategy
from src.live_trading.live_trading_engine import LiveTradingEngine


def _mom(**overrides):
    strat = MomentumStrategy("momentum_v1", overrides)
    strat.initialize()
    return strat


def test_initializes_to_none():
    strat = _mom()
    assert strat._last_exit_tag is None


def test_hold_does_not_set_a_tag():
    strat = _mom()
    pos = {"avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0}
    assert strat.manage_position(pos, 40.0) == "HOLD"
    assert strat._last_exit_tag is None


def test_underlying_stop_sets_its_own_tag():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": True, "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 97.9,
    }
    assert strat.manage_position(pos, 39.5) == "EXIT"
    assert strat._last_exit_tag == "UNDERLYING_STOP"


def test_underlying_target_sets_its_own_tag():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": False, "entry_underlying_price": 100.0, "entry_atr": 2.0,
        "current_close": 95.9,
    }
    assert strat.manage_position(pos, 40.5) == "EXIT"
    assert strat._last_exit_tag == "UNDERLYING_TARGET"


def test_hard_stop_loss_sets_its_own_tag():
    strat = _mom()
    pos = {"avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0}
    assert strat.manage_position(pos, 40.0 * (1 - strat.stop_loss_pct) - 0.5) == "EXIT"
    assert strat._last_exit_tag == "HARD_STOP_LOSS"


def test_weakening_trend_stop_sets_its_own_tag():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0,
        "current_adx": strat.adx_entry_threshold - 1, "current_adx_valid": True,
    }
    premium = 40.0 * (1 - strat.weakening_stop_loss_pct) - 0.1
    assert strat.manage_position(pos, premium) == "EXIT"
    assert strat._last_exit_tag == "WEAKENING_TREND_STOP"


def test_structural_invalidation_sets_its_own_tag():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "is_call": True, "current_close": 99.0, "current_ema_fast": 100.0,
    }
    assert strat.manage_position(pos, 40.0) == "EXIT"
    assert strat._last_exit_tag == "STRUCTURAL_INVALIDATION"


def test_profit_target_sets_its_own_tag():
    strat = _mom()
    pos = {"avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0}
    assert strat.manage_position(pos, 40.0 * (1 + strat.target_pct)) == "EXIT"
    assert strat._last_exit_tag == "PROFIT_TARGET"


def test_trailing_stop_sets_its_own_tag():
    strat = _mom()
    pos = {"avg_price": 40.0, "peak_premium": 60.0, "current_adx": 30.0}
    premium = 60.0 * (1 - strat.trailing_stop_pct) - 0.1
    assert strat.manage_position(pos, premium) == "EXIT"
    assert strat._last_exit_tag == "TRAILING_STOP"


def test_breakeven_stop_sets_its_own_tag():
    strat = _mom()
    peak = 40.0 * (1 + strat.breakeven_activation_pct) + 1.0
    pos = {"avg_price": 40.0, "peak_premium": peak, "current_adx": 30.0}
    assert strat.manage_position(pos, 40.0) == "EXIT"
    assert strat._last_exit_tag == "BREAKEVEN_STOP"


def test_trend_exhausted_sets_its_own_tag():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0,
        "current_adx": strat.adx_exit_threshold - 1, "current_adx_valid": True,
    }
    assert strat.manage_position(pos, 40.0) == "EXIT"
    assert strat._last_exit_tag == "TREND_EXHAUSTED"


def test_stall_exit_sets_its_own_tag():
    strat = _mom(stall_exit_minutes=15)
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "minutes_since_entry": 20.0, "mfe_option_pct": 0.0,
    }
    assert strat.manage_position(pos, 40.0) == "EXIT"
    assert strat._last_exit_tag == "STALL_EXIT"


def test_profit_booking_giveback_sets_its_own_tag():
    strat = _mom()
    pos = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0,
        "quantity": 100, "peak_profit_rs": 1000.0,
    }
    # current profit must fall to <= peak*(1-giveback_pct)
    floor_rs = 1000.0 * (1 - strat.profit_booking_giveback_pct) - 1.0
    premium = 40.0 + floor_rs / 100
    assert strat.manage_position(pos, premium) == "EXIT"
    assert strat._last_exit_tag == "PROFIT_BOOKING_GIVEBACK"


def test_engine_appends_tag_to_exit_reason():
    import inspect
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    assert '_last_exit_tag' in src
    assert 'exit_reason += f" [{_exit_tag}]"' in src


def test_tag_append_is_generic_not_momentum_specific():
    # Must use getattr() with a None default so ema_crossover_v1 (which
    # never sets _last_exit_tag) is unaffected -- a no-op append, not an
    # AttributeError.
    import inspect
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    idx = src.index("_last_exit_tag")
    assert 'getattr(owner_strategy, "_last_exit_tag", None)' in src[idx - 40: idx + 60]
