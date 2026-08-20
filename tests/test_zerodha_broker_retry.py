"""
ZerodhaBroker.cancel_order()/place_order()/modify_order() retry behavior.
Before the cancel_order fix, its @retry decorator was a complete no-op --
exceptions were swallowed internally before tenacity ever saw them, so
nothing was ever actually retried. place_order/modify_order weren't
verified to be free of the same bug class -- added 2026-08-13.
"""
import pytest
from unittest.mock import MagicMock
import kiteconnect.exceptions as kite_exc
from src.brokers.zerodha import ZerodhaBroker


def _bare_broker():
    broker = ZerodhaBroker.__new__(ZerodhaBroker)  # skip __init__, no real kite needed
    broker.kite = MagicMock()
    broker.kite.VARIETY_REGULAR      = "regular"
    broker.kite.EXCHANGE_NFO         = "NFO"
    broker.kite.EXCHANGE_NSE         = "NSE"
    broker.kite.PRODUCT_MIS          = "MIS"
    broker.kite.PRODUCT_NRML         = "NRML"
    broker.kite.ORDER_TYPE_MARKET    = "MARKET"
    broker.kite.ORDER_TYPE_LIMIT     = "LIMIT"
    broker.kite.TRANSACTION_TYPE_BUY  = "BUY"
    broker.kite.TRANSACTION_TYPE_SELL = "SELL"
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


# ── place_order() retry (2026-08-13) ────────────────────────────────────────
#
# Structurally different from cancel_order's original bug: place_order's
# try/except re-raises after logging rather than swallowing, so the outer
# @retry should already see failures correctly -- but that was never
# actually verified with a test, and this exact bug class has bitten a
# sibling method in this file before.

@pytest.mark.asyncio
async def test_place_order_retries_transient_network_failures():
    broker = _bare_broker()
    calls = {"n": 0}

    def _flaky_place(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise kite_exc.NetworkException("simulated transient network blip")
        return "order-999"

    broker.kite.place_order.side_effect = _flaky_place

    order_id = await broker.place_order("SBIN", "BUY", 100, 500.0)

    assert order_id == "order-999"
    assert calls["n"] == 3, "expected 2 failures + 1 successful retry"


@pytest.mark.asyncio
async def test_place_order_raises_after_exhausting_retries():
    # Fixed 2026-08-20 (deep review): place_order() no longer uses a bare
    # @retry on the whole method -- a lost response after Zerodha had
    # already accepted the order caused a genuine duplicate live order (no
    # idempotency check). Replaced with a manual retry loop that checks for
    # an existing order by tag before resubmitting. It now re-raises the
    # real underlying exception directly (not wrapped in tenacity.RetryError)
    # after exhausting attempts -- OrderManager.place_order()'s caller-side
    # `except Exception` catches this the same way either way.
    broker = _bare_broker()
    broker.kite.orders.return_value = []  # no tag-matched order found on any retry check
    broker.kite.place_order.side_effect = kite_exc.NetworkException("permanent failure")

    with pytest.raises(kite_exc.NetworkException):
        await broker.place_order("SBIN", "BUY", 100, 500.0)

    assert broker.kite.place_order.call_count == 3, "stop_after_attempt(3)"


@pytest.mark.asyncio
async def test_place_order_non_transient_exception_fails_fast_no_retry():
    broker = _bare_broker()
    broker.kite.place_order.side_effect = ValueError("insufficient margin -- will never succeed")

    with pytest.raises(ValueError):
        await broker.place_order("SBIN", "BUY", 100, 500.0)

    assert broker.kite.place_order.call_count == 1


# ── modify_order() -- zero coverage until now (2026-08-13) ─────────────────

@pytest.mark.asyncio
async def test_modify_order_success():
    broker = _bare_broker()
    broker.kite.modify_order.return_value = None

    result = await broker.modify_order("order-1", new_price=105.5, new_quantity=50)

    assert result is True
    _, kwargs = broker.kite.modify_order.call_args
    assert kwargs["order_id"] == "order-1"
    assert kwargs["price"] == 105.5
    assert kwargs["quantity"] == 50


@pytest.mark.asyncio
async def test_modify_order_retries_transient_network_failures():
    broker = _bare_broker()
    calls = {"n": 0}

    def _flaky_modify(**kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise kite_exc.NetworkException("simulated transient network blip")
        return None

    broker.kite.modify_order.side_effect = _flaky_modify

    result = await broker.modify_order("order-2", new_price=100.0, new_quantity=25)

    assert result is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_modify_order_returns_false_never_raises_after_exhausting_retries():
    broker = _bare_broker()
    broker.kite.modify_order.side_effect = kite_exc.NetworkException("permanent failure")

    result = await broker.modify_order("order-3", new_price=100.0, new_quantity=25)

    assert result is False
    assert broker.kite.modify_order.call_count == 3
