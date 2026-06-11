import pytest
import pandas as pd
from src.backtesting.engine import BacktestEngine, BacktestMetrics

@pytest.fixture
def sample_data():
    # Mock some basic OHLC + indicator data where an EMA crossover occurs
    data = []
    # Fast < Slow
    for i in range(10, 15):
        data.append({"timestamp": i, "close": 100, "high": 105, "low": 95, "ema20": 90, "ema50": 100})
    
    # Bullish Crossover (Fast > Slow)
    for i in range(15, 20):
        data.append({"timestamp": i, "close": 110, "high": 115, "low": 105, "ema20": 105, "ema50": 100})
        
    # Bearish Crossover (Fast < Slow)
    for i in range(20, 25):
        data.append({"timestamp": i, "close": 90, "high": 95, "low": 85, "ema20": 95, "ema50": 100})
        
    return pd.DataFrame(data)

def test_backtest_engine_ema_crossover(sample_data):
    engine = BacktestEngine(
        strategy_name="EMA_CROSSOVER", 
        parameters={"fast_period": 20, "slow_period": 50, "stop_loss_pct": 0.5} # High SL to prevent premature exit in test
    )
    
    metrics = engine.run(sample_data)
    
    assert metrics["total_trades"] == 2 # 1 Buy trade closed by Sell signal, 1 Sell trade forced closed at end
    assert "net_profit" in metrics
    assert "win_rate" in metrics
    
    # Verify the first trade was a BUY that exited for a loss when the bearish cross happened
    first_trade = engine.trades_history[0]
    assert first_trade["side"] == "BUY"
    assert first_trade["entry_price"] == 110  # Entered on the bullish cross tick
    assert first_trade["exit_price"] == 90    # Exited on the bearish cross tick
