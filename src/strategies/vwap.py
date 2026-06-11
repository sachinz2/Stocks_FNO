import logging
from typing import Dict, Any, Optional
from src.strategies.base import StrategyBase, StrategyRegistry

logger = logging.getLogger(__name__)

@StrategyRegistry.register("VWAP_REVERSION")
class VWAPStrategy(StrategyBase):
    """
    VWAP Mean Reversion Strategy.
    Rules:
    - If price drops below VWAP by X ATR multipliers -> BUY
    - If price rises above VWAP by X ATR multipliers -> SELL
    """
    def initialize(self):
        self.atr_multiplier = self.parameters.get("atr_multiplier", 2.0)
        self.stop_loss_atr = self.parameters.get("stop_loss_atr", 1.5)
        logger.info(f"Initialized VWAP Reversion Strategy '{self.name}' with ATR Multiplier: {self.atr_multiplier}")

    def generate_signal(self, data: Dict[str, Any]) -> Optional[str]:
        """
        Expects data dict containing:
        - close
        - vwap
        - atr14
        """
        close = data.get("close")
        vwap = data.get("vwap")
        atr = data.get("atr14")

        if close is None or vwap is None or atr is None:
            logger.warning(f"Strategy {self.name}: Missing required indicator data.")
            return "HOLD"

        lower_band = vwap - (self.atr_multiplier * atr)
        upper_band = vwap + (self.atr_multiplier * atr)

        if close < lower_band:
            logger.info(f"[{self.name}] BUY Signal Generated. Close: {close}, Lower Band: {lower_band}")
            return "BUY"
        elif close > upper_band:
            logger.info(f"[{self.name}] SELL Signal Generated. Close: {close}, Upper Band: {upper_band}")
            return "SELL"
            
        return "HOLD"

    def manage_position(self, current_position: Dict[str, Any], current_price: float) -> Optional[str]:
        """
        Simple Stop Loss based on ATR.
        Expects current_position to have 'avg_price', 'side' (BUY/SELL), and 'atr_at_entry'.
        """
        avg_price = current_position.get("avg_price")
        side = current_position.get("side")
        atr_at_entry = current_position.get("atr_at_entry", 0)

        if not avg_price or not side or not atr_at_entry:
            return None

        sl_distance = self.stop_loss_atr * atr_at_entry

        if side == "BUY":
            stop_loss_price = avg_price - sl_distance
            if current_price <= stop_loss_price:
                logger.info(f"[{self.name}] Stop Loss Hit for BUY position. Exiting at {current_price}")
                return "EXIT"
        elif side == "SELL":
            stop_loss_price = avg_price + sl_distance
            if current_price >= stop_loss_price:
                logger.info(f"[{self.name}] Stop Loss Hit for SELL position. Exiting at {current_price}")
                return "EXIT"

        return "HOLD"

    def shutdown(self):
        logger.info(f"Shutting down VWAP Strategy '{self.name}'")
