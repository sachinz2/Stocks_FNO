from fastapi import APIRouter, HTTPException, Request, status
from src.database.connection import AsyncSessionLocal
from src.database.models.position import Position
from src.database.repositories.base import BaseRepository
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/positions", tags=["Positions"])


async def _fetch_market_prices(contracts: list, kite, redis) -> dict:
    """
    Fetch current LTPs for a list of option contracts.

    Priority:
      1. optltp:{contract} — 5-second Redis cache written by ZerodhaLTPPoller
      2. optq:{contract}   — 30-second on-demand Redis cache
      3. kite.ltp()        — single batched REST call for all cache-miss contracts;
                             result cached in optq: for 30 s

    Batching all misses into one kite.ltp() call avoids N serial REST round-trips
    for N contracts (important for 4-leg condors × multiple positions).
    """
    prices: dict = {}
    uncached: list = []

    for contract in contracts:
        hit = 0.0
        if redis:
            try:
                v = await redis.get(f"optltp:{contract}")
                if v:
                    hit = round(float(v), 2)
                else:
                    v = await redis.get(f"optq:{contract}")
                    if v:
                        hit = round(float(v), 2)
            except Exception:
                pass
        if hit:
            prices[contract] = hit
        else:
            uncached.append(contract)

    if uncached and kite:
        try:
            import asyncio
            nfo_syms = [f"NFO:{c}" for c in uncached]
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: kite.ltp(nfo_syms))
            for contract in uncached:
                ltp = data.get(f"NFO:{contract}", {}).get("last_price")
                if ltp and float(ltp) > 0:
                    prices[contract] = round(float(ltp), 2)
                    if redis:
                        try:
                            await redis.set(f"optq:{contract}", str(ltp), ex=30)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"Batch LTP fetch failed: {e}")

    return prices


async def _positions_from_engine(engine, kite, redis) -> list:
    """
    Build per-contract position rows from the engine's active spreads and condors.

    Returns one row per contract leg with signed quantity (negative = SELL, positive = BUY)
    so the dashboard's multi-leg grouping logic correctly identifies spreads and condors.

    Unrealized PnL = (market_price − avg_price) × signed_qty, correct for both sides:
      SELL leg (qty < 0): profit when current price falls below entry
      BUY  leg (qty > 0): profit when current price rises above entry
    """
    # Collect every contract in one pass so we can batch the Zerodha LTP fetch
    all_legs: list = []  # (contract, signed_qty, entry_price)

    for sym, spread in engine._active_spreads.items():
        lot = spread.get("lot_size", 0)
        if not lot:
            continue
        all_legs += [
            (spread.get("short_contract", ""), -lot, spread.get("short_premium", 0.0)),
            (spread.get("long_contract",  ""),  lot, spread.get("long_premium",  0.0)),
        ]

    for sym, cond in engine._active_condors.items():
        lot = cond.get("lot_size", 0)
        if not lot:
            continue
        all_legs += [
            (cond.get("put_short_contract",  ""), -lot, cond.get("put_short_premium",  0.0)),
            (cond.get("put_long_contract",   ""),  lot, cond.get("put_long_premium",   0.0)),
            (cond.get("call_short_contract", ""), -lot, cond.get("call_short_premium", 0.0)),
            (cond.get("call_long_contract",  ""),  lot, cond.get("call_long_premium",  0.0)),
        ]

    contracts = [c for c, _, _ in all_legs if c]
    prices    = await _fetch_market_prices(contracts, kite, redis)

    rows = []
    for contract, qty, entry in all_legs:
        if not contract:
            continue
        entry  = round(float(entry), 2)
        mkt    = prices.get(contract, 0.0)
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
        kite  = getattr(request.app.state, "kite",  None)
        return await _positions_from_engine(engine, kite, redis)

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
