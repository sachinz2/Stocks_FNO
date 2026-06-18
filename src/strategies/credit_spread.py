"""
Credit Spread Strategy — collects theta (time decay) instead of fighting it.

When ATR% is LOW (market trending steadily, not explosive):
  EMA bullish → Bull Put Spread: SELL ATM PE + BUY OTM PE
  EMA bearish → Bear Call Spread: SELL ATM CE + BUY OTM CE

Both structures collect premium upfront. Theta decays the short leg to zero,
and the profit is the credit collected minus cost of the hedge (long leg).

When ATR% is HIGH → returns HOLD (EMA crossover handles explosive moves by
buying options instead — the two strategies never fight each other).

Exit rules (managed by the engine's _check_spread_exits):
  1. Short leg decays to 25% of original value   → take 75% of max profit
  2. Short leg rises to 2× original value         → stop loss, cut loss
  3. Underlying crosses the short strike           → emergency stop
  4. DTE < 7                                       → close early before gamma risk
"""
import logging
from typing import Any, Dict, Optional

from src.strategies.base import StrategyBase, StrategyRegistry

logger = logging.getLogger(__name__)


@StrategyRegistry.register("CREDIT_SPREAD")
class CreditSpreadStrategy(StrategyBase):

    def initialize(self):
        self.fast_period = self.parameters.get("fast_period", 20)
        self.slow_period = self.parameters.get("slow_period", 50)
        # ATR as % of price — below this we use credit spreads, above we hold
        self.low_vol_threshold = self.parameters.get("low_vol_threshold", 1.2)
        # How many strike intervals wide the spread should be
        self.spread_width = self.parameters.get("spread_width", 2)
        # Close short leg when it has decayed to this fraction of sold price (75% profit)
        self.profit_close_pct = self.parameters.get("profit_close_pct", 0.25)
        # Stop loss: close if short leg rises to this multiple of sold price
        self.stop_loss_multiple = self.parameters.get("stop_loss_multiple", 2.0)
        # Close spread this many days before expiry to avoid gamma/assignment risk
        self.min_dte = self.parameters.get("min_dte", 7)

        logger.info(
            f"Initialized Credit Spread '{self.name}' | "
            f"low_vol_threshold={self.low_vol_threshold}% | "
            f"spread_width={self.spread_width} intervals | "
            f"profit_target={int((1 - self.profit_close_pct) * 100)}% | "
            f"SL_multiple={self.stop_loss_multiple}x"
        )

    def generate_signal(self, data: Dict[str, Any]) -> str:
        """
        Returns 'BULL_PUT_SPREAD', 'BEAR_CALL_SPREAD', or 'HOLD'.
        Only fires when ATR% is below the low_vol_threshold.
        """
        fast_ema = data.get(f"ema{self.fast_period}")
        slow_ema = data.get(f"ema{self.slow_period}")
        close = data.get("close", 0)
        atr = data.get("atr14", 0)

        if not fast_ema or not slow_ema or not close:
            return "HOLD"

        atr_pct = (atr / close * 100) if close > 0 else 0

        # High volatility → long options are better, we step aside
        if atr_pct >= self.low_vol_threshold:
            return "HOLD"

        if fast_ema > slow_ema:
            return "BULL_PUT_SPREAD"
        elif fast_ema < slow_ema:
            return "BEAR_CALL_SPREAD"

        return "HOLD"

    def manage_position(
        self, current_position: Dict[str, Any], current_short_premium: float
    ) -> Optional[str]:
        """
        Called by the engine with the current estimated value of the SHORT leg.
        current_position must contain 'short_premium' (original credit per share).
        Returns 'EXIT' when a stop or target is hit, else 'HOLD'.
        """
        short_premium = float(current_position.get("short_premium") or 0)
        if short_premium <= 0:
            return "HOLD"

        # Take profit: short has decayed to ≤25% of sold value → 75% profit captured
        if current_short_premium <= short_premium * self.profit_close_pct:
            logger.info(
                f"[{self.name}] Profit target: sold Rs{short_premium:.2f}, "
                f"now Rs{current_short_premium:.2f}"
            )
            return "EXIT"

        # Stop loss: short has risen to ≥2× sold value
        if current_short_premium >= short_premium * self.stop_loss_multiple:
            logger.info(
                f"[{self.name}] Stop loss: sold Rs{short_premium:.2f}, "
                f"now Rs{current_short_premium:.2f}"
            )
            return "EXIT"

        return "HOLD"

    def shutdown(self):
        logger.info(f"Shutting down Credit Spread Strategy '{self.name}'")
