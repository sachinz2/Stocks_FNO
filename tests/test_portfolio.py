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
    # Fixed 2026-08-07: PortfolioManager's real methods (calculate_pnl,
    # get_exposure, get_sector_exposure) all read positions via get_all() --
    # this fixture used to only put INFY there and stash TCS behind
    # filter(), which nothing in the current class calls (filter() is dead
    # here, likely left over from a sync_positions() method that no longer
    # exists on PortfolioManager -- see the removed test_sync_positions
    # below). get_all() now returns both, matching what's actually read.
    repo.get_all.return_value = [
        Position(id=1, symbol="INFY", quantity=5, avg_price=1500.0, realized_pnl=0.0),
        Position(id=2, symbol="TCS", quantity=10, avg_price=3000.0, realized_pnl=0.0),
    ]
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


# Fixed 2026-08-07: test_sync_positions removed -- PortfolioManager has no
# sync_positions() method (confirmed via grep of the class: only
# update_position_market_price, calculate_pnl, get_exposure, and
# get_sector_exposure exist). Whatever this tested was removed from the
# class at some point without the test being removed alongside it.


@pytest.mark.asyncio
async def test_calculate_pnl(portfolio_manager, mock_pos_repo):
    # INFY qty=5, avg=1500; TCS has no price supplied below so it's skipped
    # for unrealized PnL (both have realized_pnl=0, so total_realized stays 0)
    market_prices = {"INFY": 1600.0}

    pnl = await portfolio_manager.calculate_pnl(market_prices)

    assert pnl["unrealized_pnl"] == 500.0  # (1600 - 1500) * 5
    mock_pos_repo.update.assert_called_once()  # only INFY has a price to update


@pytest.mark.asyncio
async def test_get_sector_exposure(portfolio_manager):
    exposure = await portfolio_manager.get_sector_exposure()

    # INFY (5 * 1500) + TCS (10 * 3000) = 7500 + 30000 = 37500
    assert exposure["IT"] == 37500.0
