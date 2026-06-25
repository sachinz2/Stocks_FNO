import logging
from typing import Dict, Any, Optional
from src.strategies.base import StrategyBase, StrategyRegistry

logger = logging.getLogger(__name__)

@StrategyRegistry.register("EMA_CROSSOVER")
class EMACrossoverStrategy(StrategyBase):
    """
    EMA 20/50 Crossover Strategy.
    Rules:
    - If EMA20 crosses above EMA50 -> BUY
    - If EMA20 crosses below EMA50 -> SELL
    Requires maintaining the previous state of EMAs to detect the actual "cross".
    """
    def initialize(self):
        self.fast_period = self.parameters.get("fast_period", 20)
        self.slow_period = self.parameters.get("slow_period", 50)
        self.stop_loss_pct = self.parameters.get("stop_loss_pct", 0.50)
        self.target_pct = self.parameters.get("target_pct", 1.0)
        self.trailing_stop_pct = self.parameters.get("trailing_stop_pct", 0.25)

        # Signal confirmation: crossover must persist for this many consecutive cycles
        # before a BUY/SELL fires. Prevents rapid BUY↔SELL alternation when EMAs are close.
        self.signal_confirm_bars: int = self.parameters.get("signal_confirm_bars", 2)
        self.min_dte: int = self.parameters.get("min_dte", 10)
        self.max_dte: int = self.parameters.get("max_dte", 25)

        self.prev_fast_ema: Optional[float] = None
        self.prev_slow_ema: Optional[float] = None
        self._pending_signal: Optional[str] = None
        self._pending_count: int = 0
        self._pending_bar_key: Optional[str] = None  # tracks last 5-min bar seen

        logger.info(
            f"Initialized EMA Crossover '{self.name}' ({self.fast_period}/{self.slow_period}) | "
            f"SL={self.stop_loss_pct:.0%} TP={self.target_pct:.0%} Trail={self.trailing_stop_pct:.0%} "
            f"ConfirmBars={self.signal_confirm_bars}"
        )

    def generate_signal(self, data: Dict[str, Any]) -> Optional[str]:
        """
        Expects data dict containing:
        - ema20 (or dynamic fast_period key)
        - ema50 (or dynamic slow_period key)
        - ohlc_bar_key (optional): changes once per 5-min bar; used so that
          signal_confirm_bars counts distinct completed candles, not engine cycles.
        """
        fast_ema = data.get(f"ema{self.fast_period}")
        slow_ema = data.get(f"ema{self.slow_period}")
        bar_key  = data.get("ohlc_bar_key")  # None in test/backtest contexts

        if fast_ema is None or slow_ema is None:
            logger.warning(f"Strategy {self.name}: Missing EMA data.")
            return "HOLD"

        signal = "HOLD"

        if self.prev_fast_ema is not None and self.prev_slow_ema is not None:
            if self.prev_fast_ema <= self.prev_slow_ema and fast_ema > slow_ema:
                raw = "BUY"
            elif self.prev_fast_ema >= self.prev_slow_ema and fast_ema < slow_ema:
                raw = "SELL"
            else:
                raw = None

            if raw is not None:
                if raw != self._pending_signal:
                    # New crossover direction — start fresh
                    self._pending_signal = raw
                    self._pending_count = 1
                    self._pending_bar_key = bar_key
                elif bar_key is None or bar_key != self._pending_bar_key:
                    # Same direction AND we're on a new 5-min bar (or bar_key unavailable)
                    self._pending_count += 1
                    self._pending_bar_key = bar_key
                # else: same bar as last cycle — don't double-count

                if self._pending_count >= self.signal_confirm_bars:
                    logger.info(
                        f"[{self.name}] {raw} confirmed after "
                        f"{self._pending_count} bars — firing."
                    )
                    signal = raw
                    self._pending_signal = None
                    self._pending_count = 0
                    self._pending_bar_key = None
                else:
                    logger.debug(
                        f"[{self.name}] {raw} crossover pending "
                        f"({self._pending_count}/{self.signal_confirm_bars} bars)"
                    )
            else:
                # No crossover this bar — clear pending
                self._pending_signal = None
                self._pending_count = 0
                self._pending_bar_key = None

        self.prev_fast_ema = fast_ema
        self.prev_slow_ema = slow_ema

        return signal

    def manage_position(self, current_position: Dict[str, Any], current_premium: float) -> Optional[str]:
        """
        Options position management based on option premium movement.

        current_position must contain:
          - avg_price      : entry premium paid
          - peak_premium   : highest premium seen since entry (tracked by engine)

        Exit conditions (in priority order):
          1. Hard stop loss  — premium fell >= stop_loss_pct (default 50%) from entry
          2. Profit target   — premium rose >= target_pct (default 100%, i.e. 2×) from entry
          3. Trailing stop   — premium fell >= trailing_stop_pct (default 25%) from its peak
        """
        entry_premium = float(current_position.get("avg_price") or 0)
        if entry_premium <= 0 or current_premium <= 0:
            return "HOLD"

        pnl_pct = (current_premium - entry_premium) / entry_premium

        # 1. Hard stop loss
        if pnl_pct <= -self.stop_loss_pct:
            logger.info(
                f"[{self.name}] Stop loss: entry=Rs{entry_premium:.2f} "
                f"current=Rs{current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        # 2. Profit target
        if pnl_pct >= self.target_pct:
            logger.info(
                f"[{self.name}] Target hit: entry=Rs{entry_premium:.2f} "
                f"current=Rs{current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        # 3. Trailing stop — only activates once we've been in profit
        peak = float(current_position.get("peak_premium") or entry_premium)
        if peak > entry_premium:
            trail_drawdown = (peak - current_premium) / peak
            if trail_drawdown >= self.trailing_stop_pct:
                logger.info(
                    f"[{self.name}] Trailing stop: peak=Rs{peak:.2f} "
                    f"current=Rs{current_premium:.2f} (drawdown {trail_drawdown:.1%})"
                )
                return "EXIT"

        return "HOLD"

    def on_pause(self) -> None:
        """Clear the confirmation buffer so a stale crossover can't fire on resume."""
        if self._pending_signal:
            logger.info(
                f"[{self.name}] on_pause: clearing pending {self._pending_signal} signal "
                f"({self._pending_count} bars accumulated)."
            )
        self._pending_signal = None
        self._pending_count  = 0
        self._pending_bar_key = None

    def shutdown(self):
        logger.info(f"Shutting down EMA Crossover Strategy '{self.name}'")
