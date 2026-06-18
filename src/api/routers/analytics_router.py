"""
Analytics API — reads the trade_journal table to surface strategy performance.
No auth required (internal network, read-only).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, func, text

from src.database.connection import AsyncSessionLocal
from src.database.models.trade_journal import TradeJournal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/analytics", tags=["Analytics"])


async def _get_closed_trades() -> List[TradeJournal]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TradeJournal)
            .where(TradeJournal.exit_time.isnot(None))
            .order_by(TradeJournal.entry_time.desc())
        )
        return result.scalars().all()


def _trade_to_dict(t: TradeJournal) -> Dict[str, Any]:
    return {
        "id":             t.id,
        "strategy":       t.strategy_name,
        "underlying":     t.underlying,
        "structure_type": t.structure_type,
        "entry_time":     t.entry_time.isoformat() if t.entry_time else None,
        "exit_time":      t.exit_time.isoformat()  if t.exit_time  else None,
        "entry_price":    float(t.entry_price or 0),
        "exit_price":     float(t.exit_price  or 0),
        "quantity":       t.quantity,
        "pnl":            float(t.pnl or 0),
        "hold_days":      t.hold_days,
        "exit_reason":    t.exit_reason,
        "regime_atr_pct": t.regime_atr_pct,
        "iv_rank":        t.iv_rank,
        "vix_at_entry":   t.vix_at_entry,
        "slippage":       t.slippage,
    }


@router.get("/trades")
async def get_all_trades(
    strategy: Optional[str] = Query(None),
    underlying: Optional[str] = Query(None),
    limit: int = Query(200, le=1000),
):
    """All closed trades with optional strategy / symbol filter."""
    try:
        trades = await _get_closed_trades()
        if strategy:
            trades = [t for t in trades if t.strategy_name == strategy]
        if underlying:
            trades = [t for t in trades if t.underlying == underlying]
        return [_trade_to_dict(t) for t in trades[:limit]]
    except Exception as e:
        logger.error(f"Analytics /trades error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/summary")
async def get_summary():
    """
    Aggregate performance stats per strategy.
    Returns win_rate, avg_pnl, total_pnl, trade_count, avg_hold_days.
    """
    try:
        trades = await _get_closed_trades()
        if not trades:
            return {"message": "No closed trades yet.", "strategies": {}}

        by_strategy: Dict[str, List[TradeJournal]] = {}
        for t in trades:
            by_strategy.setdefault(t.strategy_name, []).append(t)

        result = {}
        for strat, strat_trades in by_strategy.items():
            pnls = [float(t.pnl or 0) for t in strat_trades]
            wins = sum(1 for p in pnls if p > 0)
            result[strat] = {
                "trade_count":   len(strat_trades),
                "win_rate":      round(wins / len(strat_trades), 3) if strat_trades else 0,
                "avg_pnl":       round(sum(pnls) / len(pnls), 2) if pnls else 0,
                "total_pnl":     round(sum(pnls), 2),
                "max_win":       round(max(pnls), 2) if pnls else 0,
                "max_loss":      round(min(pnls), 2) if pnls else 0,
                "avg_hold_days": round(
                    sum(t.hold_days or 0 for t in strat_trades) / len(strat_trades), 1
                ),
            }

        # Overall
        all_pnls = [float(t.pnl or 0) for t in trades]
        wins_total = sum(1 for p in all_pnls if p > 0)
        result["__overall__"] = {
            "trade_count": len(trades),
            "win_rate":    round(wins_total / len(trades), 3),
            "total_pnl":   round(sum(all_pnls), 2),
            "avg_pnl":     round(sum(all_pnls) / len(all_pnls), 2),
        }
        return result
    except Exception as e:
        logger.error(f"Analytics /summary error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/by-symbol")
async def get_by_symbol():
    """PnL and win rate grouped by underlying symbol."""
    try:
        trades = await _get_closed_trades()
        by_sym: Dict[str, List[float]] = {}
        for t in trades:
            by_sym.setdefault(t.underlying, []).append(float(t.pnl or 0))

        return sorted(
            [
                {
                    "underlying":  sym,
                    "trade_count": len(pnls),
                    "total_pnl":   round(sum(pnls), 2),
                    "avg_pnl":     round(sum(pnls) / len(pnls), 2),
                    "win_rate":    round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
                }
                for sym, pnls in by_sym.items()
            ],
            key=lambda x: x["total_pnl"],
            reverse=True,
        )
    except Exception as e:
        logger.error(f"Analytics /by-symbol error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/slippage")
async def get_slippage_stats():
    """Slippage report — shows how accurate our premium estimates are vs actual fills."""
    try:
        trades = await _get_closed_trades()
        slippages = [t.slippage for t in trades if t.slippage is not None]
        if not slippages:
            return {"message": "No slippage data yet (requires live mode fills)."}
        return {
            "sample_count":    len(slippages),
            "avg_slippage":    round(sum(slippages) / len(slippages), 4),
            "max_slippage":    round(max(slippages), 4),
            "min_slippage":    round(min(slippages), 4),
            "positive_count":  sum(1 for s in slippages if s > 0),
            "negative_count":  sum(1 for s in slippages if s < 0),
        }
    except Exception as e:
        logger.error(f"Analytics /slippage error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
