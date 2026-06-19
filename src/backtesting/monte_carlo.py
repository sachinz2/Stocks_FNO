"""
Monte Carlo Simulator

After backtesting, you have N trades with their PnL values.
The problem: the sequence of those trades was historically fixed.
In real life, the order will be different.

Monte Carlo answers the question:
  "What is the RANGE of outcomes if this strategy runs 10,000 more times?"

Algorithm:
  1. Take the closed trade PnL list from trade_journal
  2. Resample with replacement (bootstrap) 10,000 times
  3. For each bootstrap, compute: total PnL, max drawdown, CAGR
  4. Return percentile distributions

Key outputs:
  worst_case_drawdown (95th pct)  → "In 95% of scenarios, drawdown stays below X"
  median_cagr                     → expected return in normal market
  ruin_probability                → % of simulations that hit -50% capital loss

Usage:
    mc = MonteCarloSimulator(initial_capital=300_000)
    result = mc.run(pnl_list)       # from trade_journal
    print(result["drawdown_p95"])   # size capital reserves accordingly
"""

import logging
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Default simulation parameters
DEFAULT_SIMULATIONS = 10_000
DEFAULT_RUIN_THRESHOLD = 0.50   # 50% capital loss = "ruin"


class MonteCarloSimulator:
    """
    Bootstrap Monte Carlo on a closed trade PnL list.
    Pure numpy — fast even for 10,000 simulations × 500 trades.
    """

    def __init__(
        self,
        initial_capital: float = 300_000.0,
        n_simulations:   int   = DEFAULT_SIMULATIONS,
        ruin_threshold:  float = DEFAULT_RUIN_THRESHOLD,
    ):
        self.initial_capital = initial_capital
        self.n_simulations   = n_simulations
        self.ruin_threshold  = ruin_threshold

    def run(self, pnl_list: List[float]) -> Dict:
        """
        Run Monte Carlo simulation on the provided PnL list.

        pnl_list : list of per-trade PnL values (from trade_journal.pnl)

        Returns:
          {
            trade_count          : int
            total_pnl_median     : float
            total_pnl_p5         : float  (worst 5% scenario)
            total_pnl_p95        : float  (best 5% scenario)
            drawdown_median      : float
            drawdown_p50         : float
            drawdown_p75         : float
            drawdown_p95         : float  (size your reserves to this)
            drawdown_p99         : float  (catastrophic tail)
            cagr_median          : float  (annualised, assuming 252 trading days)
            cagr_p5              : float
            ruin_probability     : float  (0.0–1.0)
            ruin_threshold_pct   : float  (the % loss level = "ruin")
            simulations          : int
          }
        """
        if len(pnl_list) < 10:
            return {"error": f"Need at least 10 trades, got {len(pnl_list)}."}

        pnls = np.array(pnl_list, dtype=float)
        n    = len(pnls)

        logger.info(
            f"MonteCarloSimulator: {self.n_simulations} simulations "
            f"× {n} trades | capital=₹{self.initial_capital:,.0f}"
        )

        # Bootstrap: resample pnls with replacement for each simulation
        # Shape: (n_simulations, n_trades)
        sampled = np.random.choice(pnls, size=(self.n_simulations, n), replace=True)

        # Total PnL per simulation
        total_pnls = sampled.sum(axis=1)

        # Max drawdown per simulation
        cumulative = np.cumsum(sampled, axis=1)   # (sims, trades)
        peak       = np.maximum.accumulate(cumulative, axis=1)
        drawdowns  = (peak - cumulative).max(axis=1)   # (sims,)

        # CAGR (annualised assuming 252 trading days per year)
        years = n / 252.0
        final_equity = self.initial_capital + total_pnls
        # CAGR = (final / initial)^(1/years) - 1
        cagr = np.where(
            final_equity > 0,
            (final_equity / self.initial_capital) ** (1.0 / years) - 1.0,
            -1.0,
        )

        # Ruin: simulations that lose >= ruin_threshold × initial_capital
        ruin_loss   = self.initial_capital * self.ruin_threshold
        ruin_count  = (drawdowns >= ruin_loss).sum()
        ruin_prob   = float(ruin_count) / self.n_simulations

        def pct(arr, p):
            return round(float(np.percentile(arr, p)), 2)

        result = {
            "trade_count":          n,
            "simulations":          self.n_simulations,
            "initial_capital":      self.initial_capital,
            # PnL distribution
            "total_pnl_p5":         pct(total_pnls,  5),
            "total_pnl_median":     pct(total_pnls, 50),
            "total_pnl_p95":        pct(total_pnls, 95),
            # Drawdown distribution
            "drawdown_p25":         pct(drawdowns, 25),
            "drawdown_median":      pct(drawdowns, 50),
            "drawdown_p75":         pct(drawdowns, 75),
            "drawdown_p95":         pct(drawdowns, 95),   # size reserves to this
            "drawdown_p99":         pct(drawdowns, 99),   # catastrophic tail
            # CAGR
            "cagr_p5":              round(float(np.percentile(cagr,  5)) * 100, 2),
            "cagr_median":          round(float(np.percentile(cagr, 50)) * 100, 2),
            "cagr_p95":             round(float(np.percentile(cagr, 95)) * 100, 2),
            # Ruin
            "ruin_probability":     round(ruin_prob * 100, 2),   # as percentage
            "ruin_threshold_pct":   round(self.ruin_threshold * 100, 1),
            # Practical interpretation
            "interpretation": self._interpret(
                ruin_prob, pct(drawdowns, 95), pct(total_pnls, 50)
            ),
        }

        logger.info(
            f"MC result: median_pnl=₹{result['total_pnl_median']:,.0f} "
            f"| DD_p95=₹{result['drawdown_p95']:,.0f} "
            f"| CAGR_median={result['cagr_median']:.1f}% "
            f"| ruin={result['ruin_probability']:.1f}%"
        )
        return result

    @staticmethod
    def _interpret(ruin_prob: float, dd_p95: float, median_pnl: float) -> dict:
        """Human-readable verdict for the dashboard."""
        if ruin_prob > 0.10:
            risk_label = "HIGH — ruin probability exceeds 10%. Do NOT scale capital."
        elif ruin_prob > 0.03:
            risk_label = "ELEVATED — ruin probability 3–10%. Cap at ₹3L, monitor closely."
        else:
            risk_label = "LOW — ruin probability < 3%. Strategy appears well-risk-controlled."

        if median_pnl > 0:
            pnl_label = f"Strategy is median-profitable at ₹{median_pnl:,.0f} per cycle."
        else:
            pnl_label = f"Strategy median PnL is negative (₹{median_pnl:,.0f}). Do not trade live."

        return {
            "risk_assessment":   risk_label,
            "pnl_assessment":    pnl_label,
            "reserve_for_dd":    f"Hold ₹{dd_p95:,.0f} in reserve (95th pct worst drawdown).",
        }

    async def run_from_db(self, strategy_name: Optional[str] = None) -> Dict:
        """
        Convenience: load PnL list from trade_journal table and run simulation.
        Pass strategy_name=None to simulate across all strategies.
        """
        from src.database.connection import AsyncSessionLocal
        from src.database.models.trade_journal import TradeJournal
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            q = select(TradeJournal.pnl).where(TradeJournal.exit_time.isnot(None))
            if strategy_name:
                q = q.where(TradeJournal.strategy_name == strategy_name)
            rows = (await session.execute(q)).scalars().all()

        pnls = [float(p) for p in rows if p is not None]
        if not pnls:
            return {"error": "No closed trades found for Monte Carlo simulation."}

        return self.run(pnls)
