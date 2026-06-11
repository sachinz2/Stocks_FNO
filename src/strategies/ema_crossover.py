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
        self.stop_loss_pct = self.parameters.get("stop_loss_pct", 0.02) # 2% default
        self.trailing_stop_pct = self.parameters.get("trailing_stop_pct", 0.01) # 1% default
        
        # State tracking for crossover detection
        self.prev_fast_ema: Optional[float] = None
        self.prev_slow_ema: Optional[float] = None
        
        logger.info(f"Initialized EMA Crossover Strategy '{self.name}' ({self.fast_period}/{self.slow_period})")

    def generate_signal(self, data: Dict[str, Any]) -> Optional[str]:
        """
        Expects data dict containing:
        - ema20 (or dynamic fast_period key)
        - ema50 (or dynamic slow_period key)
        """
        fast_ema = data.get(f"ema{self.fast_period}")
        slow_ema = data.get(f"ema{self.slow_period}")

        if fast_ema is None or slow_ema is None:
            logger.warning(f"Strategy {self.name}: Missing EMA data.")
            return "HOLD"

        signal = "HOLD"

        # Check for crossover if we have previous state
        if self.prev_fast_ema is not None and self.prev_slow_ema is not None:
            # Bullish Cross: Fast was below Slow, now Fast is above Slow
            if self.prev_fast_ema <= self.prev_slow_ema and fast_ema > slow_ema:
                logger.info(f"[{self.name}] BUY Signal Generated. Bullish Crossover detected.")
                signal = "BUY"
            # Bearish Cross: Fast was above Slow, now Fast is below Slow
            elif self.prev_fast_ema >= self.prev_slow_ema and fast_ema < slow_ema:
                logger.info(f"[{self.name}] SELL Signal Generated. Bearish Crossover detected.")
                signal = "SELL"

        # Update state for next tick
        self.prev_fast_ema = fast_ema
        self.prev_slow_ema = slow_ema

        return signal

    def manage_position(self, current_position: Dict[str, Any], current_price: float) -> Optional[str]:
        """
        Manages Trailing Stop Loss and Fixed Stop Loss.
        Expects current_position to have 'avg_price', 'side', and optionally 'highest_price_reached' or 'lowest_price_reached'.
        """
        avg_price = current_position.get("avg_price")
        side = current_position.get("side")

        if not avg_price or not side:
            return None

        # Absolute stop loss check (e.g. 2% below entry)
        if side == "BUY":
            hard_sl = avg_price * (1 - self.stop_loss_pct)
            if current_price <= hard_sl:
                logger.info(f"[{self.name}] Hard Stop Loss Hit for BUY. Exiting at {current_price}")
                return "EXIT"
                
            # Trailing stop check
            highest_price = current_position.get("highest_price_reached", avg_price)
            trail_sl = highest_price * (1 - self.trailing_stop_pct)
            if current_price <= trail_sl:
                logger.info(f"[{self.name}] Trailing Stop Hit for BUY. Exiting at {current_price}")
                return "EXIT"

        elif side == "SELL":
            hard_sl = avg_price * (1 + self.stop_loss_pct)
            if current_price >= hard_sl:
                logger.info(f"[{self.name}] Hard Stop Loss Hit for SELL. Exiting at {current_price}")
                return "EXIT"
                
            # Trailing stop check
            lowest_price = current_position.get("lowest_price_reached", avg_price)
            trail_sl = lowest_price * (1 + self.trailing_stop_pct)
            if current_price >= trail_sl:
                logger.info(f"[{self.name}] Trailing Stop Hit for SELL. Exiting at {current_price}")
                return "EXIT"

        return "HOLD"

    def shutdown(self):
        logger.info(f"Shutting down EMA Crossover Strategy '{self.name}'")
