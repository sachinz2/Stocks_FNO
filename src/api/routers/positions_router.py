from fastapi import APIRouter, HTTPException, Request, status
from src.database.connection import AsyncSessionLocal
from src.database.models.position import Position
from src.database.repositories.base import BaseRepository
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/positions", tags=["Positions"])


async def _get_ltp(contract: str, redis) -> float:
    """
    Look up the current LTP for an option contract from Redis.
    Tries the 5-second live cache first (optltp:), then the 30-second
    on-demand cache (optq:). Returns 0.0 if neither is available.
    """
    if not redis:
        return 0.0
    try:
        v = await redis.get(f"optltp:{contract}")
        if v:
            return round(float(v), 2)
        v = await redis.get(f"optq:{contract}")
        if v:
            return round(float(v), 2)
    except Exception:
        pass
    return 0.0


async def _positions_from_engine(engine, redis) -> list:
    """
    Build per-contract position rows from the engine's active spreads and condors.

    Returns one row per contract leg with signed quantity (negative = SELL, positive = BUY)
    so the dashboard's multi-leg grouping logic correctly identifies spreads and condors.

    Market prices are fetched from Redis (optltp: written every 5 s by the LTP poller,
    or optq: written on-demand by get_option_quote). Unrealized PnL uses the formula
    (market_price − avg_price) × signed_quantity, which is correct for both sides:
      SELL leg (qty < 0): profit when market price falls below entry
      BUY  leg (qty > 0): profit when market price rises above entry
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
        for contract, qty, entry in legs:
            if not contract:
                continue
            entry    = round(float(entry), 2)
            mkt      = await _get_ltp(contract, redis)
            unreal   = round((mkt - entry) * qty, 2) if mkt else 0.0
            rows.append({
                "symbol":         contract,
                "quantity":       qty,
                "avg_price":      entry,
                "market_price":   mkt,
                "unrealized_pnl": unreal,
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
        for contract, qty, entry in legs:
            if not contract:
                continue
            entry  = round(float(entry), 2)
            mkt    = await _get_ltp(contract, redis)
            unreal = round((mkt - entry) * qty, 2) if mkt else 0.0
            rows.append({
                "symbol":         contract,
                "quantity":       qty,
                "avg_price":      entry,
                "market_price":   mkt,
                "unrealized_pnl": unreal,
                "realized_pnl":   0.0,
            })

    return rows


@router.get("")
async def get_positions(request: Request):
    """
    Get all open positions from the live trading engine's in-memory state,
    enriched with live market prices and unrealized PnL from Redis.

    Engine state is persisted to Redis and restored on every restart, so this
    endpoint is accurate even immediately after docker compose restart.
    Falls back to the MySQL positions table if the engine is not running.
    """
    engine = getattr(request.app.state, "trading_engine", None)
    if engine is not None:
        redis = getattr(request.app.state, "redis", None)
        return await _positions_from_engine(engine, redis)

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
