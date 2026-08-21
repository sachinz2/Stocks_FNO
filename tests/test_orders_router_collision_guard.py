"""
Cross-strategy contract-collision guard for manual order placement (2026-08-14).

Found in a follow-up review: LiveTradingEngine._has_active_multi_leg_structure()
is checked inline in the engine's own automated entry paths
(_process_signal/_process_credit_spread/_process_iron_condor) but manual
orders placed through orders_router.py's POST /orders called
OrderManager.place_order() directly, bypassing the guard entirely -- the same
class of live incident (ema_crossover_v1 trading BAJFINANCE while
credit_spread_v1 held it open as a spread leg) could happen again via a
manual order instead of a second automated strategy.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.dto.schemas import OrderRequest
from src.api.routers.orders_router import place_order


def _make_request(engine):
    app = SimpleNamespace(state=SimpleNamespace(trading_engine=engine))
    return SimpleNamespace(app=app)


class _FakeEngine:
    def __init__(self, underlying_map, active_multi_leg):
        self._underlying_map = underlying_map
        self._active_multi_leg = active_multi_leg
        self.order_manager = AsyncMock()

    def _get_underlying_from_contract(self, contract):
        return self._underlying_map.get(contract)

    def _has_active_multi_leg_structure(self, symbol):
        return symbol in self._active_multi_leg


@pytest.mark.asyncio
async def test_manual_order_blocked_when_underlying_has_active_spread():
    engine = _FakeEngine({"BAJFINANCE26SEP1190CE": "BAJFINANCE"}, {"BAJFINANCE"})
    order_request = OrderRequest(symbol="BAJFINANCE26SEP1190CE", side="BUY", quantity=1, price=100.0)

    with pytest.raises(HTTPException) as exc_info:
        await place_order(order_request, _make_request(engine))

    assert exc_info.value.status_code == 409
    engine.order_manager.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_manual_order_allowed_when_no_collision():
    engine = _FakeEngine({"RELIANCE26SEP2500CE": "RELIANCE"}, set())
    engine.order_manager.place_order.return_value = SimpleNamespace(id=1)
    order_request = OrderRequest(symbol="RELIANCE26SEP2500CE", side="BUY", quantity=1, price=100.0)

    result = await place_order(order_request, _make_request(engine))

    engine.order_manager.place_order.assert_called_once()
    assert result.order_id == "1"


@pytest.mark.asyncio
async def test_manual_order_allowed_when_underlying_unresolvable():
    # A symbol that doesn't match any known FNO underlying prefix -- guard
    # must not block on an underlying it can't identify.
    engine = _FakeEngine({}, {"BAJFINANCE"})
    engine.order_manager.place_order.return_value = SimpleNamespace(id=2)
    order_request = OrderRequest(symbol="SOMETHING_UNKNOWN", side="BUY", quantity=1, price=100.0)

    result = await place_order(order_request, _make_request(engine))

    engine.order_manager.place_order.assert_called_once()
    assert result.order_id == "2"


@pytest.mark.asyncio
async def test_manual_order_still_fails_closed_when_engine_not_ready():
    order_request = OrderRequest(symbol="RELIANCE26SEP2500CE", side="BUY", quantity=1, price=100.0)

    with pytest.raises(HTTPException) as exc_info:
        await place_order(order_request, _make_request(None))

    assert exc_info.value.status_code == 503
