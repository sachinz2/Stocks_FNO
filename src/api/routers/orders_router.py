from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dto.schemas import OrderRequest, OrderResponse
from src.api.dependencies import get_current_user
from src.database.connection import get_db_session
from src.database.models.order import Order
from src.database.models.audit import AuditLog
from src.database.repositories.base import BaseRepository
from src.orders.order_manager import OrderManager
from src.risk.risk_manager import RiskManager
from src.paper_trading.paper_broker import PaperBroker
from src.core.logger import logger

router = APIRouter(prefix="/orders", tags=["Orders"])

# Initialize broker and risk manager (in production, these would be in a dependency container)
paper_broker = PaperBroker(initial_balance=300000.0)
risk_manager = RiskManager(initial_capital=300000.0)

@router.post("", response_model=OrderResponse)
async def place_order(
    request: OrderRequest,
    session: AsyncSession = Depends(get_db_session),
    user: str = Depends(get_current_user)
):
    """Place a new order (BUY or SELL)"""
    try:
        order_repo = BaseRepository(Order, session)
        audit_repo = BaseRepository(AuditLog, session)
        
        om = OrderManager(paper_broker, risk_manager, order_repo, audit_repo)
        db_order = await om.place_order(request.symbol, request.side, request.quantity, request.price)
        
        if not db_order:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to create order")
        
        return OrderResponse(order_id=str(db_order.id))
    except ValueError as e:
        logger.error(f"Order validation error: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error placing order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@router.get("")
async def get_orders(
    session: AsyncSession = Depends(get_db_session),
    user: str = Depends(get_current_user)
):
    """Get all orders"""
    try:
        order_repo = BaseRepository(Order, session)
        orders = await order_repo.get_all()
        return [
            {
                "id": o.id,
                "symbol": o.symbol,
                "side": o.side,
                "quantity": o.quantity,
                "price": float(o.price) if o.price else 0,
                "status": o.order_status,
                "created_at": o.created_at
            }
            for o in orders if o.deleted_at is None
        ]
    except Exception as e:
        logger.error(f"Error fetching orders: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@router.get("/{order_id}")
async def get_order(
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
    user: str = Depends(get_current_user)
):
    """Get specific order by ID"""
    try:
        order_repo = BaseRepository(Order, session)
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
            "created_at": order.created_at,
            "updated_at": order.updated_at
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@router.delete("/{order_id}")
async def cancel_order(
    order_id: int,
    session: AsyncSession = Depends(get_db_session),
    user: str = Depends(get_current_user)
):
    """Cancel an order"""
    try:
        order_repo = BaseRepository(Order, session)
        audit_repo = BaseRepository(AuditLog, session)
        
        om = OrderManager(paper_broker, risk_manager, order_repo, audit_repo)
        success = await om.cancel_order(order_id)
        
        if not success:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot cancel order")
        
        return {"status": "cancelled", "order_id": order_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling order: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
