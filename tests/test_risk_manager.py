import pytest
from src.risk.risk_manager import RiskManager

@pytest.fixture
def risk_manager():
    return RiskManager(initial_capital=100000.0)

def test_validate_trade_passes(risk_manager):
    # Setup clean state
    risk_manager.update_state([], 0.0, 0.0)
    
    # Attempt a safe trade (Value: 10,000, which is < 20% of 100k)
    passed = risk_manager.validate_trade("RELIANCE", "BUY", 10, 1000.0)
    assert passed is True

def test_validate_trade_max_exposure_violation(risk_manager):
    risk_manager.update_state([], 0.0, 0.0)
    
    # Attempt a trade value of 50,000 (50% of capital, exceeds 20% max)
    passed = risk_manager.validate_trade("RELIANCE", "BUY", 50, 1000.0)
    assert passed is False

def test_validate_trade_max_daily_loss_violation(risk_manager):
    # Max loss is 5% of 100k = -5000
    risk_manager.update_state([], -6000.0, 0.0) # We are down 6k today
    
    passed = risk_manager.validate_trade("RELIANCE", "BUY", 10, 100.0)
    assert passed is False
    assert risk_manager.rules["kill_switch_active"] is True

def test_validate_trade_max_positions_violation(risk_manager):
    # Fixed 2026-08-07: was hardcoded to 5 -- the real configured limit is
    # rules["max_open_positions"] = 25 (src/risk/risk_manager.py), so this
    # never actually exercised the real boundary; a 6th position legitimately
    # should (and does) pass under the real limit.
    max_positions = risk_manager.rules["max_open_positions"]
    open_positions = [
        {"symbol": f"SYM{i}", "quantity": 10} for i in range(max_positions)
    ]
    risk_manager.update_state(open_positions, 0.0, 0.0)

    # Attempting to open one more new position beyond the real limit
    passed = risk_manager.validate_trade("NEW_SYM", "BUY", 10, 100.0)
    assert passed is False

    # Attempting to add to an existing position should pass
    passed = risk_manager.validate_trade("SYM1", "BUY", 10, 100.0)
    assert passed is True

def test_kill_switch(risk_manager):
    risk_manager.activate_kill_switch("Manual Override")

    passed = risk_manager.validate_trade("RELIANCE", "BUY", 10, 100.0)
    assert passed is False


# ── capital_at_risk gating (credit_spread_v1 / iron_condor_v1) ─────────────
#
# SELL-anchored spread/condor legs are priced by max loss (capital_at_risk),
# not quantity*price like a plain BUY -- validate_trade must gate on that
# explicit figure when provided, and preserve the old fail-open default
# (trade_value=0) for any SELL caller that doesn't pass it.

from src.core.constants import STRATEGY_CAPITAL_ALLOCATION


def test_capital_at_risk_blocks_sell_entry_over_strategy_budget():
    rm = RiskManager(initial_capital=300_000.0)
    budget = 300_000.0 * STRATEGY_CAPITAL_ALLOCATION["credit_spread_v1"]
    rm._strategy_deployed["credit_spread_v1"] = budget - 1000.0  # simulate prior trades

    ok = rm.validate_trade(
        "TESTCE", "SELL", 25, 10.0,
        strategy_name="credit_spread_v1", capital_at_risk=5000.0,
    )
    assert ok is False


def test_capital_at_risk_allows_sell_entry_within_budget():
    rm = RiskManager(initial_capital=300_000.0)
    budget = 300_000.0 * STRATEGY_CAPITAL_ALLOCATION["credit_spread_v1"]
    rm._strategy_deployed["credit_spread_v1"] = budget - 1000.0

    ok = rm.validate_trade(
        "TESTCE2", "SELL", 25, 10.0,
        strategy_name="credit_spread_v1", capital_at_risk=500.0,
    )
    assert ok is True


def test_sell_without_capital_at_risk_keeps_old_fail_open_default():
    # No regression for any SELL caller that doesn't pass capital_at_risk --
    # trade_value defaults to 0, same as before this gate existed.
    rm = RiskManager(initial_capital=300_000.0)
    budget = 300_000.0 * STRATEGY_CAPITAL_ALLOCATION["credit_spread_v1"]
    rm._strategy_deployed["credit_spread_v1"] = budget * 10  # absurdly over budget

    ok = rm.validate_trade("TESTCE3", "SELL", 25, 10.0, strategy_name="credit_spread_v1")
    assert ok is True


def test_buy_path_unaffected_by_capital_at_risk_gate():
    # ema_crossover_v1 / momentum_v1 BUY entries still gate on quantity*price
    # as before -- capital_at_risk is specific to SELL-anchored spread/condor legs.
    rm = RiskManager(initial_capital=300_000.0)
    ema_budget = 300_000.0 * STRATEGY_CAPITAL_ALLOCATION["ema_crossover_v1"]
    rm._strategy_deployed["ema_crossover_v1"] = ema_budget - 100.0

    ok = rm.validate_trade("TESTPE", "BUY", 25, 50.0, strategy_name="ema_crossover_v1")  # 25*50=1250 > 100 headroom
    assert ok is False
