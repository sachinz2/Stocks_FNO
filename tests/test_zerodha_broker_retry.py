"""
ZerodhaBroker.cancel_order() retry behavior. Before this fix, the @retry
decorator was a complete no-op -- exceptions were swallowed internally
before tenacity ever saw them, so nothing was ever actually retried.
"""
import pytest
from unittest.mock import MagicMock
import kiteconnect.exceptions as kite_exc
from src.brokers.zerodha import ZerodhaBroker


def _bare_broker():
    broker = ZerodhaBroker.__new__(ZerodhaBroker)  # skip __init__, no real kite needed
    broker.kite = MagicMock()
    broker.kite.VARIETY_REGULAR = "regular"
    return broker


@pytest.mark.asyncio
async def test_cancel_order_retries_transient_network_failures():
    broker = _bare_broker()
    calls = {"n": 0}

    def _flaky_cancel(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise kite_exc.NetworkException("simulated transient network blip")
        return None  # kite.cancel_order returns None on success

    broker.kite.cancel_order.side_effect = _flaky_cancel

    result = await broker.cancel_order("dummy-order-id")

    assert result is True
    assert calls["n"] == 3, "expected 2 failures + 1 successful retry"


@pytest.mark.asyncio
async def test_cancel_order_returns_false_never_raises_after_exhausting_retries():
    broker = _bare_broker()
    broker.kite.cancel_order.side_effect = kite_exc.NetworkException("permanent failure")

    result = await broker.cancel_order("dummy-order-id-2")

    assert result is False
    assert broker.kite.cancel_order.call_count == 3, "stop_after_attempt(3)"


@pytest.mark.asyncio
async def test_cancel_order_non_transient_exception_fails_fast_no_retry():
    # A plain ValueError (not NetworkException/DataException) must fail on
    # the first attempt -- confirms the retry scope is correctly narrowed to
    # transient errors, not retrying everything indiscriminately.
    broker = _bare_broker()
    broker.kite.cancel_order.side_effect = ValueError("bad order id -- will never succeed")

    result = await broker.cancel_order("dummy-order-id-3")

    assert result is False
    assert broker.kite.cancel_order.call_count == 1
