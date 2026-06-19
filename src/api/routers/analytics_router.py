"""
Analytics API — reads the trade_journal table to surface strategy performance.
No auth required (internal network, read-only).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
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
        "id":                 t.id,
        "strategy":           t.strategy_name,
        "underlying":         t.underlying,
        "structure_type":     t.structure_type,
        "entry_time":         t.entry_time.isoformat() if t.entry_time else None,
        "exit_time":          t.exit_time.isoformat()  if t.exit_time  else None,
        "entry_price":        float(t.entry_price or 0),
        "exit_price":         float(t.exit_price  or 0),
        "quantity":           t.quantity,
        "pnl":                float(t.pnl or 0),
        "hold_days":          t.hold_days,
        "exit_reason":        t.exit_reason,
        "regime_atr_pct":     t.regime_atr_pct,
        "iv_rank":            t.iv_rank,
        "vix_at_entry":       t.vix_at_entry,
        "day_of_week":        t.day_of_week,
        "hour_of_day":        t.hour_of_day,
        "atr_at_exit":        t.atr_at_exit,
        "vix_at_exit":        t.vix_at_exit,
        "regime_label":       t.regime_label,
        "total_slippage_pts": t.total_slippage_pts,
        "slippage":           t.slippage,
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


@router.get("/strategy-performance")
async def get_strategy_performance():
    """
    Deep rolling performance stats per strategy.

    Returns for each strategy:
      - Rolling profit factor (last 30 closed trades)
      - Win rate / avg PnL / max drawdown
      - PnL breakdown by day-of-week and hour-of-day (edge discovery)
      - PnL breakdown by regime (TRENDING / RANGE_BOUND / VOLATILE)
      - Average total slippage per trade
      - is_active flag (paused or running)
    """
    try:
        from src.strategies.base import StrategyRegistry
        trades = await _get_closed_trades()
        if not trades:
            return {"message": "No closed trades yet.", "strategies": {}}

        by_strategy: Dict[str, List[TradeJournal]] = {}
        for t in trades:
            by_strategy.setdefault(t.strategy_name, []).append(t)

        active_map = {sid: inst.is_active for sid, inst in StrategyRegistry.get_active_strategies().items()}

        result = {}
        for strat, strat_trades in by_strategy.items():
            pnls  = [float(t.pnl or 0) for t in strat_trades]
            wins  = sum(1 for p in pnls if p > 0)
            gross_wins   = sum(p for p in pnls if p > 0)
            gross_losses = abs(sum(p for p in pnls if p < 0))
            pf = round(gross_wins / gross_losses, 3) if gross_losses > 0 else None

            # Rolling window PF (last 30)
            recent_pnls = pnls[:30]
            rw_wins   = sum(p for p in recent_pnls if p > 0)
            rw_losses = abs(sum(p for p in recent_pnls if p < 0))
            rolling_pf = round(rw_wins / rw_losses, 3) if rw_losses > 0 else None

            # Max drawdown
            cumulative, peak, max_dd = 0.0, 0.0, 0.0
            for p in reversed(pnls):
                cumulative += p
                if cumulative > peak:
                    peak = cumulative
                dd = peak - cumulative
                if dd > max_dd:
                    max_dd = dd

            # Day-of-week breakdown (0=Mon … 4=Fri)
            by_dow: Dict[int, List[float]] = {}
            for t in strat_trades:
                if t.day_of_week is not None:
                    by_dow.setdefault(t.day_of_week, []).append(float(t.pnl or 0))
            dow_stats = {
                str(dow): {
                    "count": len(ps),
                    "avg_pnl": round(sum(ps) / len(ps), 2),
                    "win_rate": round(sum(1 for p in ps if p > 0) / len(ps), 3),
                }
                for dow, ps in sorted(by_dow.items())
            }

            # Hour-of-day breakdown
            by_hour: Dict[int, List[float]] = {}
            for t in strat_trades:
                if t.hour_of_day is not None:
                    by_hour.setdefault(t.hour_of_day, []).append(float(t.pnl or 0))
            hour_stats = {
                str(hr): {
                    "count": len(ps),
                    "avg_pnl": round(sum(ps) / len(ps), 2),
                    "win_rate": round(sum(1 for p in ps if p > 0) / len(ps), 3),
                }
                for hr, ps in sorted(by_hour.items())
            }

            # Regime breakdown
            by_regime: Dict[str, List[float]] = {}
            for t in strat_trades:
                lbl = t.regime_label or "UNKNOWN"
                by_regime.setdefault(lbl, []).append(float(t.pnl or 0))
            regime_stats = {
                lbl: {
                    "count": len(ps),
                    "avg_pnl": round(sum(ps) / len(ps), 2),
                    "win_rate": round(sum(1 for p in ps if p > 0) / len(ps), 3),
                }
                for lbl, ps in sorted(by_regime.items())
            }

            # Avg slippage
            slips = [t.total_slippage_pts for t in strat_trades if t.total_slippage_pts is not None]
            avg_slip = round(sum(slips) / len(slips), 4) if slips else None

            result[strat] = {
                "is_active":         active_map.get(strat, None),
                "trade_count":       len(strat_trades),
                "win_rate":          round(wins / len(strat_trades), 3),
                "avg_pnl":           round(sum(pnls) / len(pnls), 2) if pnls else 0,
                "total_pnl":         round(sum(pnls), 2),
                "max_win":           round(max(pnls), 2) if pnls else 0,
                "max_loss":          round(min(pnls), 2) if pnls else 0,
                "max_drawdown":      round(max_dd, 2),
                "profit_factor":     pf,
                "rolling_pf_30":     rolling_pf,
                "avg_slippage_pts":  avg_slip,
                "by_day_of_week":    dow_stats,
                "by_hour_of_day":    hour_stats,
                "by_regime":         regime_stats,
            }

        return result

    except Exception as e:
        logger.error(f"Analytics /strategy-performance error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/strategy-health")
async def get_strategy_health(request: Request):
    """
    Live health snapshot from StrategyMonitor.

    Shows rolling PF, rolling drawdown, expected drawdown thresholds,
    and whether each strategy has been auto-paused and why.
    """
    try:
        engine = getattr(request.app.state, "trading_engine", None)
        if not engine or not engine.strategy_monitor:
            return {"message": "StrategyMonitor not initialised."}
        return await engine.strategy_monitor.get_report()
    except Exception as e:
        logger.error(f"Analytics /strategy-health error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/portfolio-exposure")
async def get_portfolio_exposure(request: Request):
    """
    Live portfolio correlation and sector concentration analysis.

    Returns:
      - sector_exposure: notional and count per sector
      - beta_exposure: per-symbol beta and weighted portfolio beta
      - correlation_flags: pairs of open positions with r > 0.85
      - concentration_alerts: sectors over the MAX_SECTOR_POSITIONS / notional limit
    """
    try:
        engine = getattr(request.app.state, "trading_engine", None)
        if not engine or not engine.portfolio_analyzer:
            return {"message": "PortfolioAnalyzer not initialised."}
        positions = await engine.broker.get_positions()
        return engine.portfolio_analyzer.get_report(positions)
    except Exception as e:
        logger.error(f"Analytics /portfolio-exposure error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/market-regime")
async def get_market_regime(request: Request):
    """
    Current market regime and which strategies are enabled/disabled.

    Regime classification:
      TRENDING    → VIX 12–20 + NIFTY ATR% ≥ 1.5%  → EMA crossover active
      RANGE_BOUND → VIX 12–20 + flat EMA            → Iron condor active
      VOLATILE    → VIX > 20                          → Credit spread active
      LOW_VOL     → VIX < 12                          → Spread + condor active
    """
    try:
        engine = getattr(request.app.state, "trading_engine", None)
        if not engine or not engine.regime_detector:
            return {"message": "MarketRegimeDetector not initialised."}
        return await engine.regime_detector.get_regime_report()
    except Exception as e:
        logger.error(f"Analytics /market-regime error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/rs-ranks")
async def get_rs_ranks(request: Request, top: int = Query(10, le=40)):
    """
    Relative Strength rankings for all F&O symbols vs NIFTY50.

    Returns ranked list of {symbol, rs_score, rank}.
    Only symbols in the top-10 by RS are eligible for EMA crossover trades.
    RS score composition: 40% 5-day rel. return + 35% 20-day rel. return + 25% EMA structure.
    """
    try:
        engine = getattr(request.app.state, "trading_engine", None)
        if not engine or not engine.rs_ranker:
            return {"message": "RSRanker not initialised."}
        ranks = await engine.rs_ranker.get_ranks()
        return {"top": top, "ranks": ranks[:top], "total_ranked": len(ranks)}
    except Exception as e:
        logger.error(f"Analytics /rs-ranks error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/walk-forward")
async def run_walk_forward(
    strategy_name: str = Query("EMA_CROSSOVER"),
    symbol:        str = Query("RELIANCE"),
    start_year:    int = Query(2021),
    end_year:      int = Query(2025),
    fast_min:      int = Query(18),
    fast_max:      int = Query(22),
    slow_min:      int = Query(45),
    slow_max:      int = Query(55),
    train_years:   int = Query(2),
    test_years:    int = Query(1),
):
    """
    Run walk-forward analysis for a strategy + symbol.
    Downloads daily history, runs IS/OOS windows, returns robustness verdict.

    Typical run: ~10-30 seconds depending on symbol history depth.
    Results are also saved to the walk_forward_results table.
    """
    try:
        from src.backtesting.walk_forward import WalkForwardTester
        param_grid = {
            "fast_period": list(range(fast_min, fast_max + 1)),
            "slow_period": list(range(slow_min, slow_max + 1, 2)),
        }
        tester  = WalkForwardTester(
            strategy_name=strategy_name,
            param_grid=param_grid,
            train_years=train_years,
            test_years=test_years,
        )
        results = await tester.run(symbol=symbol, start_year=start_year, end_year=end_year)
        if not results:
            return {"message": "No results — check symbol name or date range."}
        await tester.save(results)
        summary = tester.summary(results)
        return {
            "summary": summary,
            "window_count": len(results),
            "windows": [
                {
                    "window_start": r.window_start,
                    "train_end":    r.train_end,
                    "window_end":   r.window_end,
                    "is_oos":       r.is_oos,
                    "params":       r.parameters,
                    "profit_factor": r.profit_factor,
                    "sharpe_ratio":  r.sharpe_ratio,
                    "win_rate":      r.win_rate,
                    "total_pnl":     r.total_pnl,
                    "trade_count":   r.trade_count,
                }
                for r in results
            ],
        }
    except Exception as e:
        logger.error(f"Analytics /walk-forward error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/monte-carlo")
async def run_monte_carlo(
    strategy_name: Optional[str] = Query(None),
    n_simulations: int = Query(10000, le=50000),
):
    """
    Run Monte Carlo simulation on closed trades from trade_journal.

    Pass strategy_name to simulate one strategy; omit for all strategies combined.
    Returns confidence intervals: p5/median/p95 for PnL, drawdown, and CAGR.
    The drawdown_p95 figure tells you how much reserve capital to hold.
    """
    try:
        from src.backtesting.monte_carlo import MonteCarloSimulator
        mc = MonteCarloSimulator(n_simulations=n_simulations)
        return await mc.run_from_db(strategy_name=strategy_name)
    except Exception as e:
        logger.error(f"Analytics /monte-carlo error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/robustness")
async def run_robustness_check(
    strategy_name: str = Query("EMA_CROSSOVER"),
    symbol:        str = Query("RELIANCE"),
    years:         int = Query(3, ge=1, le=6),
    fast_min:      int = Query(18),
    fast_max:      int = Query(22),
    slow_min:      int = Query(45),
    slow_max:      int = Query(55),
):
    """
    Parameter robustness check — detects curve-fitting.

    Runs the strategy on a grid of nearby parameter values and measures
    what fraction are profitable. If only EMA 20/50 works and 19/49 + 21/51 lose
    money, the strategy has no real edge.

    Verdict:
      ROBUST     → ≥60% of parameter combos profitable
      MARGINAL   → 35–60% profitable — proceed with caution
      CURVE_FIT  → <35% profitable — do NOT trade live
    """
    try:
        from src.backtesting.robustness import ParameterRobustnessAnalyzer
        param_grid = {
            "fast_period": list(range(fast_min, fast_max + 1)),
            "slow_period": list(range(slow_min, slow_max + 1, 2)),
        }
        analyzer = ParameterRobustnessAnalyzer(
            strategy_name=strategy_name,
            param_grid=param_grid,
        )
        return await analyzer.analyze(symbol=symbol, years=years)
    except Exception as e:
        logger.error(f"Analytics /robustness error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/walk-forward-results")
async def get_walk_forward_results(
    strategy_name: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
):
    """Return stored walk-forward results from the DB."""
    try:
        from src.database.models.walk_forward import WalkForwardResult
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            q = select(WalkForwardResult).order_by(WalkForwardResult.run_at.desc()).limit(limit)
            if strategy_name:
                q = q.where(WalkForwardResult.strategy_name == strategy_name)
            rows = (await session.execute(q)).scalars().all()
        return [
            {
                "id":            r.id,
                "strategy":      r.strategy_name,
                "symbol":        r.symbol,
                "window_start":  r.window_start,
                "train_end":     r.train_end,
                "window_end":    r.window_end,
                "is_oos":        bool(r.is_oos),
                "parameters":    r.parameters,
                "profit_factor": r.profit_factor,
                "sharpe_ratio":  r.sharpe_ratio,
                "max_drawdown":  r.max_drawdown,
                "win_rate":      r.win_rate,
                "total_pnl":     r.total_pnl,
                "trade_count":   r.trade_count,
                "expectancy":    r.expectancy,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Analytics /walk-forward-results error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
