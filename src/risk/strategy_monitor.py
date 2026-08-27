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

from src.core.utils import now_ist
from src.strategies.base import StrategyRegistry

logger = logging.getLogger(__name__)

# ── Configurable thresholds ──────────────────────────────────────────────────
ROLLING_WINDOW      = 30    # number of recent closed trades to evaluate
ROLLING_PF_FLOOR    = 0.9   # pause if gross_wins / gross_losses < this
DRAWDOWN_MULTIPLIER = 1.5   # pause if rolling_dd > multiplier × expected_dd
MIN_TRADES_REQUIRED = 30    # don't evaluate with fewer trades — 10 is statistically meaningless

# Expected per-strategy max drawdown (₹) — operator-configurable at startup.
# These are conservative defaults; override via constructor.
#
# Fixed 2026-08-06: these keys were "ema_crossover"/"credit_spread"/
# "iron_condor" -- the actual strategy instance IDs used everywhere else in
# the system (StrategyRegistry, STRATEGY_CAPITAL_ALLOCATION,
# REGIME_STRATEGY_MAP) are "ema_crossover_v1"/"credit_spread_v1"/
# "iron_condor_v1"/"momentum_v1". Since StrategyMonitor is instantiated with
# no override (api/main.py: StrategyMonitor(trade_journal_repo)), every
# exp_dd lookup used strategy_id (e.g. "credit_spread_v1") against this dict,
# which never matched -- exp_dd was always 0, and Check 2 (rolling drawdown)
# is gated on `exp_dd > 0`, so the drawdown-based auto-pause has never
# actually fired for any strategy since this file was written. Only Check 1
# (profit factor) was ever live.
DEFAULT_EXPECTED_DRAWDOWN: Dict[str, float] = {
    "ema_crossover_v1": 15_000.0,
    "credit_spread_v1": 10_000.0,
    "iron_condor_v1":    8_000.0,
    # momentum_v1 (added 2026-07-30) never had a default at all -- 20% capital
    # allocation vs ema_crossover_v1's 30%, scaled proportionally from its
    # 15,000 default (~10,000), rounded up slightly since it's a newer,
    # less-tested strategy. Operator-tunable like the rest.
    "momentum_v1":       12_000.0,
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
        # Fixed 2026-08-27 (trade review follow-up): strategy_id → datetime
        # of the last MANUAL resume, if any. Without this, a manual resume
        # was immediately undone -- evaluate_all() re-reads the same last-30
        # rolling window on the very next cycle, still dominated by
        # whatever pre-resume disaster triggered the pause, and re-pauses
        # before the strategy could ever close a single new trade to start
        # displacing that window. See _filtered_trades().
        self._resumed_at: Dict[str, datetime] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def mark_resumed(self, strategy_id: str) -> None:
        """
        Call this whenever an operator manually resumes a strategy (see
        strategy_router.py's /activate endpoint) -- NOT on the initial
        load_strategy() at startup, which should still be judged against
        full history immediately if it already has 30+ trades.

        Grants a grace window: only trades that closed AFTER this moment
        count toward the next auto-pause evaluation, so a strategy just
        given a deliberate second chance (e.g. after a fix) gets to
        accumulate its own fresh track record instead of being re-judged
        on the exact trades that got it paused in the first place.
        """
        self._resumed_at[strategy_id] = now_ist().replace(tzinfo=None)
        self._pause_reasons[strategy_id] = None
        self._paused_at[strategy_id] = None
        logger.info(
            f"StrategyMonitor: {strategy_id} manually resumed -- auto-pause "
            f"evaluation now scoped to trades closed after this point until "
            f"{self.rolling_window} fresh ones have accumulated."
        )

    async def evaluate_all(self) -> None:
        """
        Main evaluation loop. Call once per trading cycle.
        Reads last `rolling_window` closed trades per strategy and applies
        the profit-factor and drawdown checks.
        """
        active = StrategyRegistry.get_active_strategies()
        for strategy_id in list(active.keys()):
            # Fixed 2026-08-20 (deep review): one strategy's evaluation
            # exception (e.g. the pf=None format-string crash fixed below)
            # used to propagate straight out of evaluate_all() -- which is
            # called directly from run_signal_cycle() with no try/except
            # around it -- aborting every other strategy's evaluation AND the
            # rest of that cycle's regime detection and entry-signal
            # generation. Isolate per-strategy like every other per-item loop
            # in this codebase (LTPPoller.poll(), the engine's exit loops).
            try:
                await self._evaluate_strategy(strategy_id)
            except Exception as exc:
                logger.error(f"StrategyMonitor: evaluation failed for {strategy_id}: {exc}")

    async def get_report(self) -> Dict[str, dict]:
        """
        Return a health snapshot for every known strategy.
        Used by the /analytics/strategy-health API endpoint.
        """
        active = StrategyRegistry.get_active_strategies()
        report: Dict[str, dict] = {}
        for strategy_id, instance in active.items():
            trades = await self._filtered_trades(strategy_id)
            pf     = self._profit_factor(trades)
            dd     = self._rolling_drawdown(trades)
            exp_dd = self.expected_drawdown.get(strategy_id, DEFAULT_EXPECTED_DRAWDOWN.get(strategy_id, 0))
            resumed_at = self._resumed_at.get(strategy_id)
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
                "resumed_at":       resumed_at.isoformat() if resumed_at else None,
            }
        return report

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _filtered_trades(self, strategy_id: str) -> list:
        """
        Trades this strategy's next auto-pause decision should be judged
        against: the last `rolling_window` closed trades, EXCEPT that if the
        strategy was manually resumed (see mark_resumed()), anything that
        closed before that resume is excluded -- a deliberate second chance
        shouldn't be immediately revoked by the exact trades that justified
        the pause it's recovering from. Once enough genuinely fresh trades
        accumulate this converges back to the plain rolling window on its
        own (no special-casing needed to "turn it off").
        """
        trades = await self._load_recent_trades(strategy_id)
        resumed_at = self._resumed_at.get(strategy_id)
        if resumed_at is not None:
            trades = [t for t in trades if t["exit_time"] is not None and t["exit_time"] > resumed_at]
        return trades

    async def _evaluate_strategy(self, strategy_id: str) -> None:
        trades = await self._filtered_trades(strategy_id)

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
            # Fixed 2026-08-20 (deep review): pf/rolling_dd can legitimately
            # be None here (_profit_factor() returns None whenever the
            # rolling window has zero losing trades -- "can't compute a
            # denominator"), but this f-string used to format them with
            # `:.3f`/`:.0f` unconditionally, raising an uncaught TypeError
            # that -- with no try/except around evaluate_all()'s caller at
            # the time -- aborted the rest of that signal cycle, including
            # all new-entry generation, and would repeat every cycle for as
            # long as the zero-losers condition persisted.
            pf_str = f"{pf:.3f}" if pf is not None else "N/A"
            dd_str = f"₹{rolling_dd:.0f}" if rolling_dd is not None else "N/A"
            logger.info(
                f"StrategyMonitor: {strategy_id} now healthy "
                f"(PF={pf_str}, DD={dd_str}). "
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
            StrategyRegistry.pause_strategy(strategy_id, reason=reason)
            self._pause_reasons[strategy_id] = reason
            # IST-naive, matching the rest of the system's convention (not
            # currently displayed on the dashboard, but kept consistent —
            # same bug class fixed elsewhere today).
            self._paused_at[strategy_id] = now_ist().replace(tzinfo=None).isoformat()
            logger.error(
                f"AUTO-KILL: Strategy '{strategy_id}' paused. Reason: {reason}"
            )

    async def _load_recent_trades(self, strategy_id: str):
        """
        Fetch the last `rolling_window` CLOSED trades for this strategy.
        Returns a list of dicts with {'pnl': float, 'exit_time': datetime}
        -- exit_time is carried through so _filtered_trades() can exclude
        anything that closed before a manual resume (see mark_resumed()).
        """
        try:
            rows = await self.trade_journal_repo.filter(
                strategy_name=strategy_id,
                limit=self.rolling_window,
                order_by="exit_time DESC",
            )
            return [
                {"pnl": float(r.pnl), "exit_time": r.exit_time}
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
