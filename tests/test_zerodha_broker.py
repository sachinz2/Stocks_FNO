import pytest
from unittest.mock import MagicMock, patch
import sys

# Mock kiteconnect module before importing ZerodhaBroker
mock_kiteconnect = MagicMock()
sys.modules['kiteconnect'] = mock_kiteconnect

from src.brokers.zerodha import ZerodhaBroker

@pytest.fixture
def broker():
    return ZerodhaBroker(api_key="test_key", api_secret="test_secret")

def test_authenticate(broker):
    broker.kite.generate_session.return_value = {"access_token": "test_token"}
    success = broker.authenticate("dummy_request_token")
    assert success is True
    assert broker.access_token == "test_token"
    broker.kite.set_access_token.assert_called_with("test_token")

@pytest.mark.asyncio
async def test_place_order(broker):
    broker.kite.place_order.return_value = "order123"
    
    order_id = await broker.place_order("SBIN", "BUY", 100, 500.0)
    
    assert order_id == "order123"
    broker.kite.place_order.assert_called_once()

@pytest.mark.asyncio
async def test_cancel_order(broker):
    broker.kite.cancel_order.return_value = "order123"
    
    result = await broker.cancel_order("order123")
    assert result is True
    broker.kite.cancel_order.assert_called_once()
