import pytest
from unittest.mock import AsyncMock
from src.portfolio.portfolio_manager import PortfolioManager
from src.database.models.position import Position
from src.database.models.stock import Stock

@pytest.fixture
def mock_broker():
    broker = AsyncMock()
    broker.get_positions.return_value = [
        {"tradingsymbol": "TCS", "quantity": 10, "average_price": 3000.0, "realized_pnl": 500.0}
    ]
    return broker

@pytest.fixture
def mock_pos_repo():
    repo = AsyncMock()
    db_pos = Position(id=1, symbol="INFY", quantity=5, avg_price=1500.0, realized_pnl=0.0)
    repo.get_all.return_value = [db_pos]
    repo.filter.return_value = [db_pos, Position(id=2, symbol="TCS", quantity=10, avg_price=3000.0)]
    return repo

@pytest.fixture
def mock_stock_repo():
    repo = AsyncMock()
    repo.get_all.return_value = [
        Stock(symbol="INFY", sector="IT"),
        Stock(symbol="TCS", sector="IT")
    ]
    return repo

@pytest.fixture
def portfolio_manager(mock_broker, mock_pos_repo, mock_stock_repo):
    return PortfolioManager(mock_broker, mock_pos_repo, mock_stock_repo)

@pytest.mark.asyncio
async def test_sync_positions(portfolio_manager, mock_pos_repo, mock_broker):
    await portfolio_manager.sync_positions()
    
    mock_broker.get_positions.assert_called_once()
    mock_pos_repo.get_all.assert_called_once()
    
    # It should create TCS and update (zero out) INFY since INFY is not in broker return
    mock_pos_repo.create.assert_called_once()
    mock_pos_repo.update.assert_called_once()

@pytest.mark.asyncio
async def test_calculate_pnl(portfolio_manager, mock_pos_repo):
    # Only INFY is in get_all() mock initially, qty=5, avg=1500
    market_prices = {"INFY": 1600.0}
    
    pnl = await portfolio_manager.calculate_pnl(market_prices)
    
    assert pnl["unrealized_pnl"] == 500.0  # (1600 - 1500) * 5
    mock_pos_repo.update.assert_called_once()

@pytest.mark.asyncio
async def test_get_sector_exposure(portfolio_manager):
    exposure = await portfolio_manager.get_sector_exposure()
    
    # INFY (5 * 1500) + TCS (10 * 3000) = 7500 + 30000 = 37500
    assert exposure["IT"] == 37500.0
