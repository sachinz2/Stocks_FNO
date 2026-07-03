from fastapi import APIRouter, HTTPException, Request, status
from src.database.connection import AsyncSessionLocal
from src.database.models.position import Position
from src.database.repositories.base import BaseRepository
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/positions", tags=["Positions"])


def _positions_from_engine(engine) -> list:
    """
    Build per-contract position rows from the engine's active spreads and condors.

    The dashboard groups rows by underlying (JSWSTEEL26JUL1210PE → JSWSTEEL) and
    identifies multi-leg structures by checking for both positive and negative
    quantities under the same underlying. So we return one row per contract leg
    with signed quantity: negative for SELL legs, positive for BUY legs.

    market_price and unrealized_pnl are 0 here; the dashboard falls back to
    avg_price when market_price is 0, so the display is correct without live quotes.
    """
    rows = []

    for sym, spread in engine._active_spreads.items():
        lot = spread.get("lot_size", 0)
        if not lot:
            continue
        legs = [
            (spread.get("short_contract", ""), -lot, spread.get("short_premium", 0.0)),
            (spread.get("long_contract",  ""),  lot, spread.get("long_premium",  0.0)),
        ]
        for contract, qty, price in legs:
            if contract:
                rows.append({
                    "symbol":         contract,
                    "quantity":       qty,
                    "avg_price":      round(float(price), 2),
                    "market_price":   0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl":   0.0,
                })

    for sym, cond in engine._active_condors.items():
        lot = cond.get("lot_size", 0)
        if not lot:
            continue
        legs = [
            (cond.get("put_short_contract",  ""), -lot, cond.get("put_short_premium",  0.0)),
            (cond.get("put_long_contract",   ""),  lot, cond.get("put_long_premium",   0.0)),
            (cond.get("call_short_contract", ""), -lot, cond.get("call_short_premium", 0.0)),
            (cond.get("call_long_contract",  ""),  lot, cond.get("call_long_premium",  0.0)),
        ]
        for contract, qty, price in legs:
            if contract:
                rows.append({
                    "symbol":         contract,
                    "quantity":       qty,
                    "avg_price":      round(float(price), 2),
                    "market_price":   0.0,
                    "unrealized_pnl": 0.0,
                    "realized_pnl":   0.0,
                })

    return rows


@router.get("")
async def get_positions(request: Request):
    """
    Get all open positions from the live trading engine's in-memory state.

    Engine state is persisted to Redis and restored on every restart, so this
    endpoint is accurate even immediately after docker compose restart.
    Falls back to the MySQL positions table if the engine is not running
    (it will be empty since the engine never writes to it).
    """
    engine = getattr(request.app.state, "trading_engine", None)
    if engine is not None:
        return _positions_from_engine(engine)

    # Fallback: MySQL (legacy — engine never writes here, will always be empty)
    try:
        pos_repo = BaseRepository(Position, AsyncSessionLocal)
        positions = await pos_repo.get_all()
        return [
            {
                "symbol":         p.symbol,
                "quantity":       p.quantity,
                "avg_price":      float(p.avg_price)      if p.avg_price      else 0,
                "market_price":   float(p.market_price)   if p.market_price   else 0,
                "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else 0,
                "realized_pnl":   float(p.realized_pnl)   if p.realized_pnl   else 0,
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
            "symbol":         p.symbol,
            "quantity":       p.quantity,
            "avg_price":      float(p.avg_price)      if p.avg_price      else 0,
            "market_price":   float(p.market_price)   if p.market_price   else 0,
            "unrealized_pnl": float(p.unrealized_pnl) if p.unrealized_pnl else 0,
            "realized_pnl":   float(p.realized_pnl)   if p.realized_pnl   else 0,
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
        realized   = float(p.realized_pnl)   if p.realized_pnl   else 0
        return {
            "symbol":         p.symbol,
            "unrealized_pnl": unrealized,
            "realized_pnl":   realized,
            "total_pnl":      unrealized + realized,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching position PnL: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")
