from fastapi import APIRouter, Depends, HTTPException, status
from src.api.dto.schemas import SignalGenerateRequest
from src.api.services.auth import require_admin_token
from src.database.connection import AsyncSessionLocal
from src.database.models.signal import Signal
from src.database.repositories.base import BaseRepository
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


@router.post("/generate", dependencies=[Depends(require_admin_token)])
async def generate_signals(
    request: SignalGenerateRequest,
):
    """Manually trigger signal generation — requires the shared admin token (X-Admin-Token header)."""
    # Fixed 2026-09-15 (deep review): this endpoint never ran any real
    # strategy logic -- it inserted one hardcoded signal_type="BUY",
    # confidence=0.75 row per fno_enabled stock, unconditionally, with no
    # validation that request.strategy is even a real registered strategy.
    # LiveTradingEngine/the strategies never write to the `signals` table
    # (they only log signal text) -- this stub was the ONLY writer to it,
    # so every row GET /signals ever returned was fabricated, with no way
    # for a caller to tell it apart from a genuine signal-generation run.
    # Same fix pattern as risk_router/backtest_router/stocks_router (fake
    # 200 -> honest 501); GET /signals and GET /signals/{id} are untouched
    # since they honestly read whatever real rows exist.
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Manual signal generation via this endpoint is not implemented — "
            "it previously inserted fabricated signal rows, not real strategy output."
        ),
    )
