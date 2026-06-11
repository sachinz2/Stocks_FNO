import pytest
from unittest.mock import AsyncMock, MagicMock
from src.orders.order_manager import OrderManager
from src.database.models.order import Order

@pytest.fixture
def mock_broker():
    broker = AsyncMock()
    broker.place_order.return_value = "broker_123"
    broker.cancel_order.return_value = True
    broker.get_orders.return_value = [
        {"order_id": "broker_123", "status": "COMPLETED"}
    ]
    return broker

@pytest.fixture
def mock_risk_manager():
    rm = MagicMock()
    rm.validate_trade.return_value = True
    return rm

@pytest.fixture
def mock_order_repo():
    repo = AsyncMock()
    db_order = Order(id=1, symbol="RELIANCE", side="BUY", quantity=10, price=2000.0, order_status="PENDING", broker_order_id="broker_123")
    repo.create.return_value = db_order
    repo.get_by_id.return_value = db_order
    repo.filter.return_value = [db_order]
    return repo

@pytest.fixture
def mock_audit_repo():
    return AsyncMock()

@pytest.fixture
def order_manager(mock_broker, mock_risk_manager, mock_order_repo, mock_audit_repo):
    return OrderManager(mock_broker, mock_risk_manager, mock_order_repo, mock_audit_repo)

@pytest.mark.asyncio
async def test_place_order_success(order_manager, mock_risk_manager, mock_broker, mock_order_repo, mock_audit_repo):
    result = await order_manager.place_order("RELIANCE", "BUY", 10, 2000.0)
    
    # 1. DB Create called
    mock_order_repo.create.assert_called_once()
    # 2. Risk check passed
    mock_risk_manager.validate_trade.assert_called_once_with("RELIANCE", "BUY", 10, 2000.0)
    # 3. Broker called
    mock_broker.place_order.assert_called_once_with("RELIANCE", "BUY", 10, 2000.0)
    # 4. DB Update called to OPEN
    mock_order_repo.update.assert_called_once()
    assert result.order_status == "PENDING"  # Initially returned as mock, but update was called

@pytest.mark.asyncio
async def test_place_order_risk_rejected(order_manager, mock_risk_manager, mock_broker, mock_order_repo, mock_audit_repo):
    mock_risk_manager.validate_trade.return_value = False
    
    result = await order_manager.place_order("RELIANCE", "BUY", 10, 2000.0)
    
    mock_broker.place_order.assert_not_called()
    mock_order_repo.update.assert_called_once()

@pytest.mark.asyncio
async def test_sync_orders(order_manager, mock_order_repo, mock_broker):
    await order_manager.sync_orders()
    
    mock_order_repo.filter.assert_called_once_with(order_status="OPEN")
    mock_broker.get_orders.assert_called_once()
    mock_order_repo.update.assert_called_once()  # Should update to COMPLETED based on mock
