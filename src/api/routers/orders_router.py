from fastapi import APIRouter, Depends, HTTPException, Request, status
from src.api.dto.schemas import OrderRequest, OrderResponse
from src.api.dependencies import get_current_user
from src.database.connection import AsyncSessionLocal
from src.database.models.order import Order
from src.database.models.audit import AuditLog
from src.database.repositories.base import BaseRepository
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["Orders"])


def _get_order_manager(request: Request):
    """
    Reuse the live trading engine's real OrderManager (its real broker --
    paper or live depending on TradingMode -- and its real, capital-
    compounding-aware RiskManager instance), instead of this router
    building its own separate PaperBroker/RiskManager.

    Fixed 2026-08-13: this router used to construct a completely
    independent module-level `_risk_manager`/`_paper_broker` pair. That
    RiskManager never received expiry-to-expiry capital-period updates
    (only engine.risk_manager does, via the scheduled rollover job) and
    tracked its own separate deployed-capital/daily-loss counters --  a
    manual order placed here could pass its own exposure/daily-loss check
    while the combined real exposure across both paths exceeded the
    configured limit. The paper broker was similarly a disconnected
    simulated balance, unrelated to whatever the live engine itself was
    tracking.
    """
    engine = getattr(request.app.state, "trading_engine", None)
    if engine is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Trading engine not ready")
    return engine.order_manager


@router.get("")
async def get_orders():
    """Get all orders — no auth required (read-only, internal network)."""
    try:
        order_repo = BaseRepository(Order, AsyncSessionLocal)
        orders = await order_repo.get_all()
        return [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.quantity,
                "price": float(o.price) if o.price else 0,
                "status": o.order_status,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in orders
            if o.deleted_at is None
        ]
    except Exception as e:
        logger.error(f"Error fetching orders: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/{order_id}")
async def get_order(order_id: int):
    """Get specific order by ID — no auth required."""
    try:
        order_repo = BaseRepository(Order, AsyncSessionLocal)
        order = await order_repo.get_by_id(order_id)

        if not order or order.deleted_at:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

        return {
            "id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": float(order.price) if order.price else 0,
            "status": order.order_status,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post("", response_model=OrderResponse)
async def place_order(
    order_request: OrderRequest,
    http_request: Request,
    user: str = Depends(get_current_user),
):
    """Place a new order — requires JWT auth."""
    try:
        om = _get_order_manager(http_request)
        db_order = await om.place_order(
            order_request.symbol, order_request.side, order_request.quantity, order_request.price,
        )

        if not db_order:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to create order")

        return OrderResponse(order_id=str(db_order.id))
    except ValueError as e:
        logger.error(f"Order validation error: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error placing order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.delete("/{order_id}")
async def cancel_order(
    order_id: int,
    http_request: Request,
    user: str = Depends(get_current_user),
):
    """Cancel an order — requires JWT auth."""
    try:
        om = _get_order_manager(http_request)
        success = await om.cancel_order(order_id)

        if not success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot cancel order")

        return {"status": "cancelled", "order_id": order_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
