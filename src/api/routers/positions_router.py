from fastapi import APIRouter, HTTPException, status
from src.database.connection import AsyncSessionLocal
from src.database.models.position import Position
from src.database.repositories.base import BaseRepository
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/positions", tags=["Positions"])


@router.get("")
async def get_positions():
    """Get all open positions — no auth required (read-only, internal network)."""
    try:
        pos_repo = BaseRepository(Position, AsyncSessionLocal)
        positions = await pos_repo.get_all()
        return [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_price": float(p.avg_price) if p.avg_price else 0,
                "market_price": float(p.market_price) if p.market_price else 0,
                "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else 0,
                "realized_pnl": float(p.realized_pnl) if p.realized_pnl else 0,
            }
            for p in positions
            if p.deleted_at is None and p.quantity != 0
        ]
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/{symbol}")
async def get_position(symbol: str):
    """Get specific position by symbol — no auth required."""
    try:
        pos_repo = BaseRepository(Position, AsyncSessionLocal)
        positions = await pos_repo.filter(symbol=symbol)

        if not positions or positions[0].deleted_at:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No position found for {symbol}")

        p = positions[0]
        return {
            "symbol": p.symbol,
            "quantity": p.quantity,
            "avg_price": float(p.avg_price) if p.avg_price else 0,
            "market_price": float(p.market_price) if p.market_price else 0,
            "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else 0,
            "realized_pnl": float(p.realized_pnl) if p.realized_pnl else 0,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching position: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/{symbol}/pnl")
async def get_position_pnl(symbol: str):
    """Get PnL for specific position — no auth required."""
    try:
        pos_repo = BaseRepository(Position, AsyncSessionLocal)
        positions = await pos_repo.filter(symbol=symbol)

        if not positions or positions[0].deleted_at:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No position found for {symbol}")

        p = positions[0]
        unrealized = float(p.unrealized_pnl) if p.unrealized_pnl else 0
        realized = float(p.realized_pnl) if p.realized_pnl else 0
        return {
            "symbol": p.symbol,
            "unrealized_pnl": unrealized,
            "realized_pnl": realized,
            "total_pnl": unrealized + realized,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching position PnL: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
