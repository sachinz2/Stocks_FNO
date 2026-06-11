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
    # Set 5 open positions
    open_positions = [
        {"symbol": f"SYM{i}", "quantity": 10} for i in range(5)
    ]
    risk_manager.update_state(open_positions, 0.0, 0.0)
    
    # Attempting to open a 6th new position
    passed = risk_manager.validate_trade("NEW_SYM", "BUY", 10, 100.0)
    assert passed is False
    
    # Attempting to add to an existing position should pass
    passed = risk_manager.validate_trade("SYM1", "BUY", 10, 100.0)
    assert passed is True

def test_kill_switch(risk_manager):
    risk_manager.activate_kill_switch("Manual Override")
    
    passed = risk_manager.validate_trade("RELIANCE", "BUY", 10, 100.0)
    assert passed is False
