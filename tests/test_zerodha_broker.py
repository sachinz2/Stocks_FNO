import pytest
from unittest.mock import MagicMock, patch
from src.brokers.zerodha import ZerodhaBroker


@pytest.fixture
def broker():
    # Fixed 2026-08-07: patching sys.modules['kiteconnect'] at file-import
    # time only works if src.brokers.zerodha's own `from kiteconnect import
    # KiteConnect` (module-level, evaluated once) hasn't already run against
    # the REAL package -- which it had, because pytest collects (imports)
    # every test file before running any test bodies, and some other test
    # file transitively imports src.brokers.zerodha first. That left
    # broker.kite as a genuine KiteConnect instance with real bound methods
    # (hence "'method' object has no attribute 'return_value'" instead of a
    # working mock). Patching the already-bound KiteConnect name directly on
    # the zerodha module is robust regardless of import order.
    with patch("src.brokers.zerodha.KiteConnect") as mock_kite_cls:
        mock_kite_cls.return_value = MagicMock()
        yield ZerodhaBroker(api_key="test_key", api_secret="test_secret")


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
async def test_place_order_entry_uses_limit_order_with_price(broker):
    # Fixed 2026-08-07: entries stay LIMIT, unchanged.
    broker.kite.place_order.return_value = "order123"

    await broker.place_order("SBIN", "BUY", 100, 500.0, is_exit_order=False)

    _, kwargs = broker.kite.place_order.call_args
    assert kwargs["order_type"] == broker.kite.ORDER_TYPE_LIMIT
    assert kwargs["price"] == 500.0


@pytest.mark.asyncio
async def test_place_order_exit_uses_market_order_no_price(broker):
    # Fixed 2026-08-07: exits now use MARKET, not LIMIT -- a LIMIT exit
    # order can sit unfilled if the market moves away, but every exit path
    # treats "broker accepted the order" as "position closed" (journal
    # popped, capital released), leaving a real still-open position with
    # nothing watching it if it never actually filled. MARKET orders
    # guarantee an immediate, certain fill.
    broker.kite.place_order.return_value = "order456"

    order_id = await broker.place_order("SBIN", "SELL", 100, 500.0, is_exit_order=True)

    assert order_id == "order456"
    _, kwargs = broker.kite.place_order.call_args
    assert kwargs["order_type"] == broker.kite.ORDER_TYPE_MARKET
    assert "price" not in kwargs, "MARKET orders must not pass a price -- they execute at best available"


@pytest.mark.asyncio
async def test_cancel_order(broker):
    broker.kite.cancel_order.return_value = "order123"

    result = await broker.cancel_order("order123")
    assert result is True
    broker.kite.cancel_order.assert_called_once()
