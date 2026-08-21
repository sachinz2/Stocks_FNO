from fastapi import APIRouter, HTTPException, Request, status
from src.database.connection import AsyncSessionLocal
from src.database.models.position import Position
from src.database.models.trade_journal import TradeJournal
from src.database.repositories.base import BaseRepository
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/positions", tags=["Positions"])

# Fixed 2026-08-13: /positions, /analytics/pnl-summary, and
# /analytics/capital-periods each independently called
# _positions_from_engine() (which walks engine state and fetches live
# market prices) on every dashboard render -- the dashboard fetches all
# three in the same page load, every 60s auto-refresh, so this was doing
# the identical work 3x per render. A short TTL is enough to collapse
# those into one real fetch while staying well under the refresh interval
# (so it never serves meaningfully stale data). Keyed by id(engine) --
# there's only ever one real engine per running process, so this is
# effectively a single global cache in production, but keying it avoids
# cross-contamination between different engine instances passed in tests.
_POSITIONS_CACHE_TTL_SECONDS = 3.0
_positions_cache: dict = {}  # id(engine) -> (fetched_at, result)


async def _fetch_market_prices(contracts: list, kite, redis) -> dict:
    """
    Fetch current LTPs for a list of option contracts.

    Priority:
      1. optltp:{contract} — 5-second Redis cache written by ZerodhaLTPPoller
      2. optq:{contract}   — 30-second on-demand Redis cache
      3. kite.quote()      — single batched REST call for all cache-miss contracts,
                             resolved through resolve_reliable_option_price() (same
                             staleness check steps 1-2's upstream writers already
                             apply) so a stale last-traded price on a thin/illiquid
                             leg isn't silently shown as current; result cached in
                             optq: for 30 s

    Batching all misses into one kite.quote() call avoids N serial REST round-trips
    for N contracts (important for 4-leg condors × multiple positions).

    Fixed 2026-08-21: this fallback used to call kite.ltp() and read last_price
    directly with no staleness check, unlike steps 1-2 (which are staleness-aware
    at their source -- see resolve_reliable_option_price()'s docstring). Now uses
    kite.quote() + resolve_reliable_option_price(), matching the other 2 call
    sites (ZerodhaLTPPoller's option refresh, option_chain.get_option_quote()'s
    own step-3 fallback).
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
            from src.market_data.option_chain import resolve_reliable_option_price
            nfo_syms = [f"NFO:{c}" for c in uncached]
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, lambda: kite.quote(nfo_syms))
            for contract in uncached:
                ltp = resolve_reliable_option_price(data.get(f"NFO:{contract}", {}))
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


async def _positions_from_engine_uncached(engine, kite, redis) -> list:
    """
    Build per-contract position rows from the engine's active spreads and condors.

    Returns one row per contract leg with signed quantity (negative = SELL, positive = BUY)
    so the dashboard's multi-leg grouping logic correctly identifies spreads and condors.

    Unrealized PnL = (market_price − avg_price) × signed_qty, correct for both sides:
      SELL leg (qty < 0): profit when current price falls below entry
      BUY  leg (qty > 0): profit when current price rises above entry
    """
    # Collect every contract in one pass so we can batch the Zerodha LTP fetch
    all_legs: list = []  # (contract, signed_qty, entry_price, group_id, structure_type)

    # Fixed 2026-08-13: group_id/structure_type let the dashboard group legs
    # by the SAME structure the engine actually tracks them under, instead of
    # a same-underlying + has-short-and-long heuristic. That heuristic could
    # misattribute an unrelated standalone single-leg position (ema_crossover_v1/
    # momentum_v1) into a credit_spread_v1/iron_condor_v1 structure's group
    # whenever both happen to be open on the same underlying at once --
    # entirely plausible since both trade from the same 41-symbol F&O universe.
    for sym, spread in engine._active_spreads.items():
        lot = spread.get("lot_size", 0)
        if not lot:
            continue
        group_id = f"spread:{sym}"
        all_legs += [
            (spread.get("short_contract", ""), -lot, spread.get("short_premium", 0.0), group_id, "credit_spread"),
            (spread.get("long_contract",  ""),  lot, spread.get("long_premium",  0.0), group_id, "credit_spread"),
        ]

    for sym, cond in engine._active_condors.items():
        lot = cond.get("lot_size", 0)
        if not lot:
            continue
        group_id = f"condor:{sym}"
        all_legs += [
            (cond.get("put_short_contract",  ""), -lot, cond.get("put_short_premium",  0.0), group_id, "iron_condor"),
            (cond.get("put_long_contract",   ""),  lot, cond.get("put_long_premium",   0.0), group_id, "iron_condor"),
            (cond.get("call_short_contract", ""), -lot, cond.get("call_short_premium", 0.0), group_id, "iron_condor"),
            (cond.get("call_long_contract",  ""),  lot, cond.get("call_long_premium",  0.0), group_id, "iron_condor"),
        ]

    # Single-leg positions (ema_crossover_v1, momentum_v1) live in
    # engine._single_leg_journals, not _active_spreads/_active_condors — this
    # loop was missing entirely, so single-leg BUYs never appeared here even
    # though they were correctly persisted to trade_journal. The journal only
    # stores journal_id/strategy/regime (not price/qty), so look those up from
    # trade_journal directly, keyed by the journal_id already on hand.
    # group_id=None -- standalone, never merged with any spread/condor group.
    single_leg_items = [(c, info) for c, info in engine._single_leg_journals.items() if c]
    if single_leg_items:
        journal_repo = BaseRepository(TradeJournal, AsyncSessionLocal)
        journals = await asyncio.gather(*[
            journal_repo.get_by_id(info["journal_id"]) for _, info in single_leg_items
        ])
        for (contract, _info), journal in zip(single_leg_items, journals):
            if journal is None or journal.entry_price is None:
                continue
            all_legs.append((contract, journal.quantity or 0, float(journal.entry_price), None, "single_leg"))

    contracts = [c for c, _, _, _, _ in all_legs if c]
    prices    = await _fetch_market_prices(contracts, kite, redis)

    rows = []
    for contract, qty, entry, group_id, structure_type in all_legs:
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
            "group_id":       group_id,
            "structure_type": structure_type,
        })
    return rows


async def _positions_from_engine(engine, kite, redis) -> list:
    """Cached wrapper -- see _POSITIONS_CACHE_TTL_SECONDS above for why."""
    now = time.monotonic()
    key = id(engine)
    cached = _positions_cache.get(key)
    if cached is not None and (now - cached[0]) < _POSITIONS_CACHE_TTL_SECONDS:
        return cached[1]
    result = await _positions_from_engine_uncached(engine, kite, redis)
    _positions_cache[key] = (now, result)
    return result


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
                "group_id":       None,
                "structure_type": None,
            }
            for p in positions
            if p.deleted_at is None and p.quantity != 0
        ]
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get("/{symbol}")
async def get_position(symbol: str, request: Request):
    """Get specific position by contract symbol — no auth required."""
    engine = getattr(request.app.state, "trading_engine", None)
    if engine is not None:
        redis = getattr(request.app.state, "redis", None)
        kite  = getattr(request.app.state, "kite",  None)
        all_pos = await _positions_from_engine(engine, kite, redis)
        matches = [p for p in all_pos if p["symbol"] == symbol]
        if matches:
            return matches[0]
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No position found for {symbol}")

    # Fallback: MySQL (engine never writes here — always empty in normal operation)
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
async def get_position_pnl(symbol: str, request: Request):
    """Get PnL for specific position by contract symbol — no auth required."""
    engine = getattr(request.app.state, "trading_engine", None)
    if engine is not None:
        redis = getattr(request.app.state, "redis", None)
        kite  = getattr(request.app.state, "kite",  None)
        all_pos = await _positions_from_engine(engine, kite, redis)
        matches = [p for p in all_pos if p["symbol"] == symbol]
        if not matches:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No position found for {symbol}")
        p = matches[0]
        return {
            "symbol":         p["symbol"],
            "unrealized_pnl": p.get("unrealized_pnl", 0.0),
            "realized_pnl":   p.get("realized_pnl",   0.0),
            "total_pnl":      p.get("unrealized_pnl", 0.0) + p.get("realized_pnl", 0.0),
        }

    # Fallback: MySQL (engine never writes here — always empty in normal operation)
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
