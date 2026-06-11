import pytest
from unittest.mock import AsyncMock
from datetime import datetime, timedelta
from src.reporting.report_generator import ReportGenerator
from src.database.models.trade import Trade

@pytest.fixture
def mock_trade_repo():
    repo = AsyncMock()
    now = datetime.utcnow()
    # Mock some trades: 2 wins, 1 loss
    t1 = Trade(id=1, strategy_name="VWAP", symbol="SBIN", pnl=100.0, entry_time=now, exit_time=now)
    t2 = Trade(id=2, strategy_name="VWAP", symbol="TCS", pnl=200.0, entry_time=now, exit_time=now)
    t3 = Trade(id=3, strategy_name="EMA", symbol="INFY", pnl=-50.0, entry_time=now, exit_time=now)
    
    repo.get_all.return_value = [t1, t2, t3]
    return repo

@pytest.mark.asyncio
async def test_daily_report(mock_trade_repo):
    generator = ReportGenerator(mock_trade_repo)
    report = await generator.daily_report()
    
    assert report["total_trades"] == 3
    assert report["net_profit"] == 250.0 # 100 + 200 - 50
    assert report["win_rate"] == 66.67 # 2 / 3
    assert report["profit_factor"] == 6.0 # 300 / 50
    # Expectancy: (0.666 * 150) - (0.333 * 50) = 100 - 16.66 = 83.33
    assert report["expectancy"] == 83.33

@pytest.mark.asyncio
async def test_strategy_report(mock_trade_repo):
    generator = ReportGenerator(mock_trade_repo)
    start = datetime.utcnow() - timedelta(days=1)
    end = datetime.utcnow() + timedelta(days=1)
    
    report = await generator.strategy_report("VWAP", start, end)
    
    # Only VWAP trades: 100 and 200 PnL
    assert report["total_trades"] == 2
    assert report["net_profit"] == 300.0
    assert report["win_rate"] == 100.0
    assert report["profit_factor"] == float('inf') # No losses
