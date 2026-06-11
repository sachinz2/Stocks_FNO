import pytest
from src.strategies.base import StrategyRegistry
from src.strategies.vwap import VWAPStrategy

def test_strategy_registration():
    strategy_class = StrategyRegistry.get_strategy_class("VWAP_REVERSION")
    assert strategy_class is not None
    assert strategy_class == VWAPStrategy

def test_vwap_buy_signal():
    strategy = StrategyRegistry.load_strategy("VWAP_REVERSION", "test_inst_1", {"atr_multiplier": 2.0})
    
    # VWAP = 100, ATR = 5. Lower band = 100 - (2.0 * 5) = 90
    data = {
        "close": 89,
        "vwap": 100,
        "atr14": 5
    }
    
    signal = strategy.generate_signal(data)
    assert signal == "BUY"

def test_vwap_sell_signal():
    strategy = StrategyRegistry.load_strategy("VWAP_REVERSION", "test_inst_2", {"atr_multiplier": 2.0})
    
    # VWAP = 100, ATR = 5. Upper band = 100 + (2.0 * 5) = 110
    data = {
        "close": 111,
        "vwap": 100,
        "atr14": 5
    }
    
    signal = strategy.generate_signal(data)
    assert signal == "SELL"

def test_vwap_hold_signal():
    strategy = StrategyRegistry.load_strategy("VWAP_REVERSION", "test_inst_3", {"atr_multiplier": 2.0})
    
    # Price is within bands
    data = {
        "close": 105,
        "vwap": 100,
        "atr14": 5
    }
    
    signal = strategy.generate_signal(data)
    assert signal == "HOLD"

def test_vwap_stop_loss():
    strategy = StrategyRegistry.load_strategy("VWAP_REVERSION", "test_inst_4", {"stop_loss_atr": 1.5})
    
    position = {
        "side": "BUY",
        "avg_price": 100,
        "atr_at_entry": 4
    }
    
    # Stop loss distance = 1.5 * 4 = 6. Stop loss price = 100 - 6 = 94.
    
    # Price above SL
    action = strategy.manage_position(position, 95)
    assert action == "HOLD"
    
    # Price hits SL
    action = strategy.manage_position(position, 94)
    assert action == "EXIT"
