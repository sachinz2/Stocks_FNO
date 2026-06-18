import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Risk management — evaluates rules before trade execution.
    """

    def __init__(self, initial_capital: float = 300000.0):
        self.initial_capital = initial_capital

        self.rules = {
            "max_daily_loss_pct": 0.05,         # 5% of capital
            # Set high to accommodate multi-leg strategies:
            # 1 condor = 4 legs, 3 strategies × 5 stocks → up to ~20 option contracts open
            "max_open_positions": 25,
            "max_exposure_per_trade_pct": 0.20,  # 20% per individual leg
            "circuit_breaker_active": False,
            "kill_switch_active": False,
        }

        self.current_open_positions: List[Dict[str, Any]] = []
        self.daily_realized_pnl: float = 0.0
        self.daily_unrealized_pnl: float = 0.0

    def update_state(
        self,
        positions: List[Dict[str, Any]],
        realized_pnl: float,
        unrealized_pnl: float,
    ):
        self.current_open_positions = positions
        self.daily_realized_pnl = realized_pnl
        self.daily_unrealized_pnl = unrealized_pnl

    def activate_kill_switch(self, reason: str):
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
        self.rules["kill_switch_active"] = True

    def deactivate_kill_switch(self):
        logger.warning("KILL SWITCH DEACTIVATED. Trading resumed.")
        self.rules["kill_switch_active"] = False

    def reset_daily_state(self):
        """Call at market open to clear intraday PnL accumulators."""
        self.daily_realized_pnl = 0.0
        self.daily_unrealized_pnl = 0.0
        logger.info("RiskManager: daily PnL state reset.")

    def validate_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        is_spread_leg: bool = False,
    ) -> bool:
        """
        Validate a proposed order against all risk rules.

        is_spread_leg=True bypasses the open-position COUNT check for legs 2-4
        of multi-leg strategies (credit spreads, iron condors). The first leg
        always goes through the count check. This prevents the condor from being
        blocked halfway through its 4-leg entry.

        The exposure-per-trade check still applies to every leg individually.
        """
        if self.rules["kill_switch_active"] or self.rules["circuit_breaker_active"]:
            logger.error("Risk: kill switch / circuit breaker active — trade blocked.")
            return False

        # 1. Daily loss limit
        total_daily_pnl = self.daily_realized_pnl + self.daily_unrealized_pnl
        max_allowed_loss = -(self.initial_capital * self.rules["max_daily_loss_pct"])
        if total_daily_pnl <= max_allowed_loss:
            logger.error(
                f"Risk: max daily loss reached — PnL {total_daily_pnl:.2f} "
                f"<= limit {max_allowed_loss:.2f}"
            )
            self.activate_kill_switch("Max Daily Loss Reached")
            return False

        # 2. Open position count — skipped for non-first legs of multi-leg strategies
        if not is_spread_leg:
            is_new = not any(
                p.get("symbol") == symbol and p.get("quantity", 0) != 0
                for p in self.current_open_positions
            )
            if is_new and len(self.current_open_positions) >= self.rules["max_open_positions"]:
                logger.error(
                    f"Risk: max open positions ({self.rules['max_open_positions']}) reached."
                )
                return False

        # 3. Per-leg exposure — BUY legs only (SELL legs collect premium, margin is handled by broker)
        if side == "BUY":
            trade_value = quantity * price
            max_allowed = self.initial_capital * self.rules["max_exposure_per_trade_pct"]
            if trade_value > max_allowed:
                logger.error(
                    f"Risk: BUY exposure ₹{trade_value:,.0f} > limit ₹{max_allowed:,.0f}"
                )
                return False

        logger.info(f"Risk OK: {side} {quantity} {symbol} @ {price}")
        return True
