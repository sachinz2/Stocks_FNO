from fastapi import APIRouter, Depends, HTTPException, status
from src.api.dto.schemas import SignalGenerateRequest, SignalGenerateResponse
from src.api.dependencies import get_current_user
from src.database.connection import AsyncSessionLocal
from src.database.models.signal import Signal
from src.database.models.stock import Stock
from src.database.repositories.base import BaseRepository
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["Signals"])


@router.get("")
async def get_signals(symbol: str = None, status_filter: str = None):
    """Get all generated signals — no auth required (read-only, internal network)."""
    try:
        signal_repo = BaseRepository(Signal, AsyncSessionLocal)
        signals = await signal_repo.filter(symbol=symbol) if symbol else await signal_repo.get_all()

        result = []
        for s in signals:
            if s.deleted_at is not None:
                continue
            if status_filter and s.status != status_filter:
                continue
            result.append({
                "id": s.id,
                "symbol": s.symbol,
                "strategy_name": s.strategy_name,
                "signal_type": s.signal_type,
                "confidence": float(s.confidence) if s.confidence else 0,
                "generated_at": s.generated_at.isoformat() if s.generated_at else None,
                "status": s.status,
            })

        return result
    except Exception as e:
        logger.error(f"Error fetching signals: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/{signal_id}")
async def get_signal(signal_id: int):
    """Get specific signal by ID — no auth required."""
    try:
        signal_repo = BaseRepository(Signal, AsyncSessionLocal)
        signal = await signal_repo.get_by_id(signal_id)

        if not signal or signal.deleted_at:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")

        return {
            "id": signal.id,
            "symbol": signal.symbol,
            "strategy_name": signal.strategy_name,
            "signal_type": signal.signal_type,
            "confidence": float(signal.confidence) if signal.confidence else 0,
            "generated_at": signal.generated_at.isoformat() if signal.generated_at else None,
            "status": signal.status,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching signal: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post("/generate", response_model=SignalGenerateResponse)
async def generate_signals(
    request: SignalGenerateRequest,
    user: str = Depends(get_current_user),
):
    """Manually trigger signal generation — requires JWT auth."""
    try:
        stock_repo = BaseRepository(Stock, AsyncSessionLocal)
        signal_repo = BaseRepository(Signal, AsyncSessionLocal)

        stocks = await stock_repo.filter(fno_enabled=True, active=True)
        generated_count = 0

        for stock in stocks:
            if stock.deleted_at:
                continue
            await signal_repo.create({
                "strategy_name": request.strategy,
                "symbol": stock.symbol,
                "signal_type": "BUY",
                "confidence": 0.75,
                "generated_at": datetime.utcnow(),
                "status": "PENDING",
            })
            generated_count += 1

        logger.info(f"Generated {generated_count} signals for strategy {request.strategy}")
        return SignalGenerateResponse(generated=generated_count)
    except Exception as e:
        logger.error(f"Error generating signals: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
