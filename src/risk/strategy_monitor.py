"""
StrategyMonitor — Concern #4

Automatically pauses strategies that show statistical deterioration:
  1. Rolling profit factor < ROLLING_PF_FLOOR (default 0.9) over last N closed trades
  2. Rolling drawdown > DRAWDOWN_MULTIPLIER × expected_drawdown

The monitor reads closed trades from the trade_journal table; it does NOT touch
live positions. Strategy auto-kill only blocks NEW entries — the engine continues
to run exits for any positions already open.

Usage:
    monitor = StrategyMonitor(trade_journal_repo, expected_drawdown_map)
    await monitor.evaluate_all()        # called every cycle by LiveTradingEngine
    await monitor.get_report()          # called by /analytics/strategy-health API
"""

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from src.strategies.base import StrategyRegistry

logger = logging.getLogger(__name__)

# ── Configurable thresholds ──────────────────────────────────────────────────
ROLLING_WINDOW      = 30    # number of recent closed trades to evaluate
ROLLING_PF_FLOOR    = 0.9   # pause if gross_wins / gross_losses < this
DRAWDOWN_MULTIPLIER = 1.5   # pause if rolling_dd > multiplier × expected_dd
MIN_TRADES_REQUIRED = 10    # don't kill a strategy with fewer trades (too little data)

# Expected per-strategy max drawdown (₹) — operator-configurable at startup.
# These are conservative defaults; override via constructor.
DEFAULT_EXPECTED_DRAWDOWN: Dict[str, float] = {
    "ema_crossover":  15_000.0,
    "credit_spread":  10_000.0,
    "iron_condor":     8_000.0,
}


class StrategyMonitor:
    """
    Evaluates rolling metrics for each active strategy and auto-pauses
    when performance falls below operator-defined thresholds.
    """

    def __init__(
        self,
        trade_journal_repo,
        expected_drawdown: Optional[Dict[str, float]] = None,
        rolling_window:      int   = ROLLING_WINDOW,
        pf_floor:            float = ROLLING_PF_FLOOR,
        dd_multiplier:       float = DRAWDOWN_MULTIPLIER,
    ):
        self.trade_journal_repo  = trade_journal_repo
        self.expected_drawdown   = expected_drawdown or DEFAULT_EXPECTED_DRAWDOWN
        self.rolling_window      = rolling_window
        self.pf_floor            = pf_floor
        self.dd_multiplier       = dd_multiplier

        # in-memory: strategy_id → reason string (or None if healthy)
        self._pause_reasons: Dict[str, Optional[str]] = {}
        # strategy_id → ISO timestamp of last auto-pause
        self._paused_at: Dict[str, Optional[str]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def evaluate_all(self) -> None:
        """
        Main evaluation loop. Call once per trading cycle.
        Reads last `rolling_window` closed trades per strategy and applies
        the profit-factor and drawdown checks.
        """
        active = StrategyRegistry.get_active_strategies()
        for strategy_id in list(active.keys()):
            await self._evaluate_strategy(strategy_id)

    async def get_report(self) -> Dict[str, dict]:
        """
        Return a health snapshot for every known strategy.
        Used by the /analytics/strategy-health API endpoint.
        """
        active = StrategyRegistry.get_active_strategies()
        report: Dict[str, dict] = {}
        for strategy_id, instance in active.items():
            trades = await self._load_recent_trades(strategy_id)
            pf     = self._profit_factor(trades)
            dd     = self._rolling_drawdown(trades)
            exp_dd = self.expected_drawdown.get(strategy_id, DEFAULT_EXPECTED_DRAWDOWN.get(strategy_id, 0))
            report[strategy_id] = {
                "is_active":        instance.is_active,
                "trades_in_window": len(trades),
                "rolling_pf":       round(pf, 4)  if pf  is not None else None,
                "rolling_drawdown": round(dd, 2)  if dd  is not None else None,
                "expected_drawdown": exp_dd,
                "pf_floor":         self.pf_floor,
                "dd_threshold":     round(exp_dd * self.dd_multiplier, 2),
                "paused_reason":    self._pause_reasons.get(strategy_id),
                "paused_at":        self._paused_at.get(strategy_id),
            }
        return report

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _evaluate_strategy(self, strategy_id: str) -> None:
        trades = await self._load_recent_trades(strategy_id)

        if len(trades) < MIN_TRADES_REQUIRED:
            return  # not enough history to make a call

        # ── Check 1: Rolling profit factor ────────────────────────────────────
        pf = self._profit_factor(trades)
        if pf is not None and pf < self.pf_floor:
            reason = (
                f"Rolling PF {pf:.3f} < floor {self.pf_floor} "
                f"(last {len(trades)} trades)"
            )
            self._auto_pause(strategy_id, reason)
            return

        # ── Check 2: Rolling drawdown vs expected ─────────────────────────────
        rolling_dd = self._rolling_drawdown(trades)
        exp_dd     = self.expected_drawdown.get(
            strategy_id,
            DEFAULT_EXPECTED_DRAWDOWN.get(strategy_id, 0),
        )
        if exp_dd > 0 and rolling_dd is not None:
            threshold = self.dd_multiplier * exp_dd
            if rolling_dd > threshold:
                reason = (
                    f"Rolling drawdown ₹{rolling_dd:.0f} > "
                    f"{self.dd_multiplier}× expected ₹{exp_dd:.0f} = ₹{threshold:.0f} "
                    f"(last {len(trades)} trades)"
                )
                self._auto_pause(strategy_id, reason)
                return

        # ── All checks passed — log if previously paused ──────────────────────
        if self._pause_reasons.get(strategy_id):
            logger.info(
                f"StrategyMonitor: {strategy_id} now healthy "
                f"(PF={pf:.3f}, DD=₹{rolling_dd:.0f}). "
                "Operator must manually /resume to re-enable."
            )

    def _auto_pause(self, strategy_id: str, reason: str) -> None:
        """
        Pause the strategy if it is still running.
        Idempotent — safe to call repeatedly; only logs on the FIRST pause.
        """
        active = StrategyRegistry.get_active_strategies()
        instance = active.get(strategy_id)
        if not instance:
            return

        if instance.is_active:
            StrategyRegistry.pause_strategy(strategy_id)
            self._pause_reasons[strategy_id] = reason
            self._paused_at[strategy_id] = datetime.now(timezone.utc).isoformat()
            logger.error(
                f"AUTO-KILL: Strategy '{strategy_id}' paused. Reason: {reason}"
            )

    async def _load_recent_trades(self, strategy_id: str):
        """
        Fetch the last `rolling_window` CLOSED trades for this strategy.
        Returns a list of dicts with at least {'pnl': float}.
        """
        try:
            rows = await self.trade_journal_repo.filter(
                strategy_name=strategy_id,
                limit=self.rolling_window,
                order_by="exit_time DESC",
            )
            return [
                {"pnl": float(r.pnl)}
                for r in rows
                if r.pnl is not None
            ]
        except Exception as e:
            logger.warning(f"StrategyMonitor: could not load trades for {strategy_id}: {e}")
            return []

    @staticmethod
    def _profit_factor(trades: list) -> Optional[float]:
        """
        Gross profit factor = sum(winning trades) / abs(sum(losing trades)).
        Returns None if there are no losing trades (can't compute a denominator).
        """
        if not trades:
            return None
        gross_wins   = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_losses = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        if gross_losses == 0:
            return None  # pure winning streak — don't mis-trigger
        return gross_wins / gross_losses

    @staticmethod
    def _rolling_drawdown(trades: list) -> Optional[float]:
        """
        Peak-to-trough drawdown over the rolling window of trades.
        Assumes trades are ordered newest-first; we reverse to get chronological order.
        """
        if not trades:
            return None
        pnls = [t["pnl"] for t in reversed(trades)]
        cumulative = 0.0
        peak       = 0.0
        max_dd     = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        return max_dd
