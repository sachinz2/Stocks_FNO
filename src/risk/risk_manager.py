import logging
from typing import Dict, Any, List
from decimal import Decimal

logger = logging.getLogger(__name__)

class RiskManager:
    """
    Institutional-grade risk management.
    Evaluates risk rules before trade execution.
    """
    def __init__(self, initial_capital: float = 300000.0):
        self.initial_capital = initial_capital
        
        # Default Risk Rules
        self.rules = {
            "max_daily_loss_pct": 0.05, # 5% of capital
            "max_open_positions": 5,
            "max_exposure_per_trade_pct": 0.20, # Max 20% of capital in a single trade
            "circuit_breaker_active": False,
            "kill_switch_active": False
        }
        
        # State tracking (In production, this would be fetched from the DB/Cache)
        self.current_open_positions: List[Dict[str, Any]] = []
        self.daily_realized_pnl: float = 0.0
        self.daily_unrealized_pnl: float = 0.0

    def update_state(self, positions: List[Dict[str, Any]], realized_pnl: float, unrealized_pnl: float):
        """Update the risk manager with current portfolio state."""
        self.current_open_positions = positions
        self.daily_realized_pnl = realized_pnl
        self.daily_unrealized_pnl = unrealized_pnl

    def activate_kill_switch(self, reason: str):
        """Immediately blocks all new trades."""
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
        self.rules["kill_switch_active"] = True

    def deactivate_kill_switch(self):
        """Manually unblocks trades."""
        logger.warning("KILL SWITCH DEACTIVATED. Trading resumed.")
        self.rules["kill_switch_active"] = False

    def validate_trade(self, symbol: str, side: str, quantity: int, price: float) -> bool:
        """
        Validates if an order passes all risk checks.
        Returns True if passed, False otherwise.
        """
        if self.rules["kill_switch_active"] or self.rules["circuit_breaker_active"]:
            logger.error("Risk Violation: Kill switch or circuit breaker is active. Trade blocked.")
            return False

        # 1. Maximum Daily Loss Check
        total_daily_pnl = self.daily_realized_pnl + self.daily_unrealized_pnl
        max_allowed_loss = -1 * (self.initial_capital * self.rules["max_daily_loss_pct"])
        
        if total_daily_pnl <= max_allowed_loss:
            logger.error(f"Risk Violation: Max daily loss reached. PnL: {total_daily_pnl}, Allowed: {max_allowed_loss}")
            self.activate_kill_switch("Max Daily Loss Reached")
            return False

        # 2. Maximum Open Positions Check
        # If this is a new position (symbol not in current positions), check the limit
        is_new_position = True
        for pos in self.current_open_positions:
            if pos["symbol"] == symbol and pos.get("quantity", 0) > 0:
                is_new_position = False
                break
                
        if is_new_position and len(self.current_open_positions) >= self.rules["max_open_positions"]:
            logger.error(f"Risk Violation: Max open positions ({self.rules['max_open_positions']}) reached.")
            return False

        # 3. Maximum Exposure Check
        trade_value = quantity * price
        max_allowed_exposure = self.initial_capital * self.rules["max_exposure_per_trade_pct"]
        
        if trade_value > max_allowed_exposure:
            logger.error(f"Risk Violation: Trade value {trade_value} exceeds max allowed exposure {max_allowed_exposure}.")
            return False

        logger.info(f"Risk Check Passed for {side} {quantity} {symbol} @ {price}")
        return True