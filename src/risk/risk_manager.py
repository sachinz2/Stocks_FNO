import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from src.core.constants import (
    FNO_SECTORS,
    MAX_SECTOR_POSITIONS,
    STRATEGY_CAPITAL_ALLOCATION,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Multi-layer risk management evaluated before every order.

    Layers (in order):
      1. Kill switch / circuit breaker
      2. Daily loss limit (5% of capital)
      3. IV rank gate — skip spread/condor entries when options are cheap
      4. Sector concentration — max 2 open structures per sector
      5. Per-strategy capital allocation — each strategy has a fixed budget
      6. Open position count — max 25 total (accommodates multi-leg structures)
      7. Per-leg exposure — BUY legs capped at 20% of capital each

    is_spread_leg=True bypasses the position-count and sector checks for
    legs 2-4 of multi-leg strategies (the first leg still goes through all checks).
    """

    def __init__(self, initial_capital: float = 300_000.0):
        self.initial_capital = initial_capital

        self.rules = {
            "max_daily_loss_pct":        0.05,
            "max_open_positions":         25,
            "max_exposure_per_trade_pct": 0.20,
            "circuit_breaker_active":     False,
            "kill_switch_active":         False,
        }

        self.current_open_positions: List[Dict[str, Any]] = []
        self.daily_realized_pnl:   float = 0.0
        self.daily_unrealized_pnl: float = 0.0

        # Per-strategy deployed capital tracking: {strategy_name: float}
        self._strategy_deployed: Dict[str, float] = defaultdict(float)

    # ── State management ──────────────────────────────────────────────────────

    def update_state(
        self,
        positions: List[Dict[str, Any]],
        realized_pnl: float,
        unrealized_pnl: float,
    ) -> None:
        self.current_open_positions = positions
        self.daily_realized_pnl    = realized_pnl
        self.daily_unrealized_pnl  = unrealized_pnl

    def reset_daily_state(self) -> None:
        """Call at 09:15 IST every day to reset intraday PnL accumulators."""
        self.daily_realized_pnl   = 0.0
        self.daily_unrealized_pnl = 0.0
        self._strategy_deployed.clear()
        logger.info("RiskManager: daily state reset.")

    def add_deployed_capital(self, strategy_name: str, amount: float) -> None:
        """Called by engine on every confirmed order to track per-strategy exposure."""
        self._strategy_deployed[strategy_name] += amount

    def release_deployed_capital(self, strategy_name: str, amount: float) -> None:
        """Called by engine when a position is closed."""
        self._strategy_deployed[strategy_name] = max(
            0.0, self._strategy_deployed[strategy_name] - amount
        )

    def get_deployed_by_strategy(self) -> Dict[str, float]:
        return dict(self._strategy_deployed)

    # ── Kill switch ───────────────────────────────────────────────────────────

    def activate_kill_switch(self, reason: str) -> None:
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
        self.rules["kill_switch_active"] = True

    def deactivate_kill_switch(self) -> None:
        logger.warning("KILL SWITCH DEACTIVATED. Trading resumed.")
        self.rules["kill_switch_active"] = False

    # ── Core validation ───────────────────────────────────────────────────────

    def validate_trade(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        is_spread_leg: bool = False,
        is_exit_order: bool = False,
        strategy_name: Optional[str] = None,
        iv_rank: Optional[float] = None,
        vix: Optional[float] = None,
    ) -> bool:
        """
        Returns True if the trade passes all risk checks, False otherwise.

        Parameters
        ----------
        symbol        : Option contract symbol (e.g. HDFCBANK25JUL1600CE)
        side          : "BUY" or "SELL"
        quantity      : Lot size
        price         : Expected fill price
        is_spread_leg : True for legs 2-4 of spreads/condors — skips count + sector checks
        is_exit_order : True when closing an existing position — skips entry-only checks
                        (sector concentration, capital allocation, position count, BUY exposure)
        strategy_name : Used for per-strategy capital allocation check
        iv_rank       : Per-symbol IV rank [0,1] — gates spread/condor entries
        vix           : India VIX — secondary IV gate
        """

        # ── 0. Exit orders bypass ALL checks ─────────────────────────────────────
        # An open position must always be closeable — kill switch, daily loss limit,
        # and every entry-only check are irrelevant when closing a position.
        # Trapping an open loss behind a kill switch is far more dangerous than
        # allowing the exit order through.
        if is_exit_order:
            logger.info(f"Risk OK (exit — all checks bypassed): {side} {quantity} {symbol} @ {price}")
            return True

        # ── 1. Kill switch / circuit breaker ──────────────────────────────────
        if self.rules["kill_switch_active"] or self.rules["circuit_breaker_active"]:
            logger.error("Risk: kill switch / circuit breaker active — entry blocked.")
            return False

        # ── 2. Daily loss limit ───────────────────────────────────────────────
        total_daily_pnl = self.daily_realized_pnl + self.daily_unrealized_pnl
        max_allowed_loss = -(self.initial_capital * self.rules["max_daily_loss_pct"])
        if total_daily_pnl <= max_allowed_loss:
            logger.error(
                f"Risk: daily loss limit reached — PnL {total_daily_pnl:.2f} "
                f"<= limit {max_allowed_loss:.2f}"
            )
            self.activate_kill_switch("Max Daily Loss Reached")
            return False

        # Spread legs bypass entry-only checks (but kill switch above still applies).
        # By the time leg 2-4 is placed, leg 1 has already executed — blocking the
        # hedge would leave a naked short, which is more dangerous than proceeding.
        if is_spread_leg:
            logger.info(f"Risk OK (spread leg): {side} {quantity} {symbol} @ {price}")
            return True

        # ── 3. IV Rank gate (only for premium-selling strategies) ─────────────
        if strategy_name in ("CREDIT_SPREAD", "IRON_CONDOR"):
            if iv_rank is not None and iv_rank < 0.30:
                logger.warning(
                    f"Risk: IV rank {iv_rank:.2f} < 0.30 for {symbol} "
                    f"[{strategy_name}] — options too cheap, skipping."
                )
                return False
            if vix is not None and vix < 14.0:
                logger.warning(
                    f"Risk: India VIX {vix:.1f} < 14 — options too cheap market-wide, "
                    f"skipping [{strategy_name}]."
                )
                return False

        underlying = self._get_underlying(symbol)

        # ── 4. Sector concentration ───────────────────────────────────────────
        sector = FNO_SECTORS.get(underlying, "UNKNOWN")
        if sector != "UNKNOWN":
            sector_count = sum(
                1
                for p in self.current_open_positions
                if p.get("quantity", 0) != 0
                and FNO_SECTORS.get(self._get_underlying(p.get("symbol", "")), "") == sector
                and p.get("symbol", "") != symbol
            )
            if sector_count >= MAX_SECTOR_POSITIONS:
                logger.warning(
                    f"Risk: sector '{sector}' already has {sector_count} open positions "
                    f"(max {MAX_SECTOR_POSITIONS}). Skipping {symbol}."
                )
                return False

        # ── 5. Per-strategy capital allocation ────────────────────────────────
        if strategy_name and strategy_name in STRATEGY_CAPITAL_ALLOCATION:
            alloc_pct = STRATEGY_CAPITAL_ALLOCATION[strategy_name]
            budget = self.initial_capital * alloc_pct
            deployed = self._strategy_deployed.get(strategy_name, 0.0)
            trade_value = quantity * price if side == "BUY" else 0.0
            # For SELL legs the margin is taken by the broker, not our capital tracking
            if side == "BUY" and deployed + trade_value > budget:
                logger.warning(
                    f"Risk: {strategy_name} budget ₹{budget:,.0f} would be exceeded. "
                    f"Deployed: ₹{deployed:,.0f} + new: ₹{trade_value:,.0f}. Skipping."
                )
                return False

        # ── 6. Max open positions ─────────────────────────────────────────────
        is_new = not any(
            p.get("symbol") == symbol and p.get("quantity", 0) != 0
            for p in self.current_open_positions
        )
        if is_new and len(self.current_open_positions) >= self.rules["max_open_positions"]:
            logger.error(
                f"Risk: max open positions ({self.rules['max_open_positions']}) reached."
            )
            return False

        # ── 7. Per-leg BUY exposure ───────────────────────────────────────────
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get_underlying(contract: str) -> str:
        """Strip expiry + strike + type from an option contract symbol."""
        from src.core.constants import FNO_SYMBOLS
        for sym in sorted(FNO_SYMBOLS, key=len, reverse=True):
            if contract.startswith(sym):
                return sym
        return contract[:10]
