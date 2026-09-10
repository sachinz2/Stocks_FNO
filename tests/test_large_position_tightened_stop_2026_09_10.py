"""
New rule, 2026-09-10 (explicit user request): if a single-leg position's
total premium value at entry (entry_premium x quantity -- the actual Rs
capital committed) exceeds Rs25,000, the hard stop loss tightens from the
strategy's normal stop_loss_pct (default 50%) to large_position_stop_loss_pct
(default 25%). Only the HARD stop -- weakening-trend stop, trailing stop,
breakeven, and profit target are unaffected.
"""
import pytest

from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.momentum import MomentumStrategy


def _position(avg_price, quantity):
    return {
        "avg_price": avg_price, "peak_premium": avg_price, "quantity": quantity,
        "current_adx": 30.0, "is_call": True,
    }


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_small_position_keeps_the_normal_50pct_stop(strategy_cls, strategy_id):
    """Entry premium value = 40 * 100 = Rs4,000 -- well under the Rs25,000
    threshold. A 30% drawdown must NOT trigger the hard stop (needs 50%)."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    avg_price, quantity = 40.0, 100
    current_premium = avg_price * 0.70  # -30%
    result = strat.manage_position(_position(avg_price, quantity), current_premium)
    assert result == "HOLD"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_small_position_still_exits_at_the_normal_50pct_stop(strategy_cls, strategy_id):
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    avg_price, quantity = 40.0, 100
    current_premium = avg_price * 0.49  # -51%
    result = strat.manage_position(_position(avg_price, quantity), current_premium)
    assert result == "EXIT"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_large_position_exits_at_the_tightened_25pct_stop(strategy_cls, strategy_id):
    """Entry premium value = 0.70 * 71475 = Rs50,032.50 -- over the
    Rs25,000 threshold (the real IDEA26SEP15PE position size seen live).
    A 30% drawdown (past the tightened 25%, but well short of the normal
    50%) must now trigger the hard stop."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    avg_price, quantity = 0.70, 71475
    current_premium = avg_price * 0.70  # -30%, > tightened 25% floor
    result = strat.manage_position(_position(avg_price, quantity), current_premium)
    assert result == "EXIT"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_large_position_still_holds_above_the_tightened_stop(strategy_cls, strategy_id):
    """Same large position, but only a 20% drawdown -- still above the
    tightened 25% floor -- must NOT exit via the hard stop."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    avg_price, quantity = 0.70, 71475
    current_premium = avg_price * 0.80  # -20%, < tightened 25% floor
    result = strat.manage_position(_position(avg_price, quantity), current_premium)
    assert result == "HOLD"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_threshold_is_on_total_value_not_just_quantity_or_price_alone(strategy_cls, strategy_id):
    """Guard against a wrong implementation that checks quantity or price
    alone instead of their product: high quantity but tiny price, total
    value under Rs25,000 -- must use the normal 50% stop."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    avg_price, quantity = 0.10, 100000  # value = Rs10,000
    current_premium = avg_price * 0.70  # -30%, would trip the tightened stop if wrongly applied
    result = strat.manage_position(_position(avg_price, quantity), current_premium)
    assert result == "HOLD"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_missing_quantity_falls_back_to_the_normal_stop_not_fail_closed(strategy_cls, strategy_id):
    """An older restored position without quantity in current_position must
    not be strengthened OR weakened by this rule -- falls back to the
    strategy's normal stop_loss_pct, same as before this change existed."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    position = {
        "avg_price": 40.0, "peak_premium": 40.0, "current_adx": 30.0, "is_call": True,
    }  # no "quantity" key at all
    result = strat.manage_position(position, 40.0 * 0.70)  # -30%, under normal 50%
    assert result == "HOLD"
    result2 = strat.manage_position(position, 40.0 * 0.49)  # -51%, past normal 50%
    assert result2 == "EXIT"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_thresholds_are_configurable(strategy_cls, strategy_id):
    strat = strategy_cls(strategy_id, {
        "large_position_threshold_rs": 5_000.0,
        "large_position_stop_loss_pct": 0.10,
    })
    strat.initialize()
    avg_price, quantity = 10.0, 1000  # value = Rs10,000, over the lowered 5,000 threshold
    current_premium = avg_price * 0.85  # -15%, past the lowered 10% floor
    result = strat.manage_position(_position(avg_price, quantity), current_premium)
    assert result == "EXIT"
