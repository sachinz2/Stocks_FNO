import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.constants import (
    FNO_SECTORS,
    MAX_SECTOR_POSITIONS,
    STRATEGY_CAPITAL_ALLOCATION,
)
from src.core.utils import now_ist

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Multi-layer risk management evaluated before every order.

    Layers (in order):
      1. Kill switch / circuit breaker
      2. Daily loss limit (max_daily_loss_pct of capital, default 5% -- see __init__)
      3. IV rank gate — skip spread/condor entries when options are cheap
      4. Sector concentration — max 2 open structures per sector
      5. Per-strategy capital allocation — each strategy has a fixed budget
      6. Open position count — max 25 total (accommodates multi-leg structures)
      7. Per-leg exposure — BUY legs capped at max_exposure_per_trade_pct of
         capital each, default 30% (see __init__ -- fixed 2026-08-13, this
         used to be a hardcoded 20% literal disconnected from settings)

    is_spread_leg=True bypasses the position-count and sector checks for
    legs 2-4 of multi-leg strategies (the first leg still goes through all checks).
    """

    def __init__(
        self, initial_capital: float = 300_000.0,
        max_exposure_per_trade_pct: float = 0.30,
        max_daily_loss_pct: float = 0.05,
    ):
        self.initial_capital = initial_capital

        self.rules = {
            # Fixed 2026-08-13: max_daily_loss_pct/max_exposure_per_trade_pct
            # were hardcoded literals, completely disconnected from
            # settings.MAX_DAILY_LOSS_PCT/MAX_EXPOSURE_PCT (.env) -- changing
            # the .env value silently did nothing. Now constructor args, same
            # pattern as initial_capital already was.
            #
            # max_open_positions is DELIBERATELY still hardcoded, not wired
            # to settings.MAX_OPEN_POSITIONS -- that .env value (5) and this
            # 25 aren't the same unit: this counts individual legs/orders
            # (25 accommodates ~6 four-leg iron condors), while
            # MAX_OPEN_POSITIONS reads like a "max concurrent structures"
            # figure. Wiring them together as-is would silently cut real
            # trading capacity roughly 5x -- needs a deliberate decision on
            # what MAX_OPEN_POSITIONS is actually meant to mean, not a
            # find-and-replace.
            "max_daily_loss_pct":        max_daily_loss_pct,
            "max_open_positions":         25,
            "max_exposure_per_trade_pct": max_exposure_per_trade_pct,
            "circuit_breaker_active":     False,
            "kill_switch_active":         False,
        }
        self.kill_switch_reason: Optional[str] = None
        self.kill_switch_activated_at: Optional[str] = None

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
        self.kill_switch_reason = reason
        self.kill_switch_activated_at = datetime.utcnow().isoformat()

    def deactivate_kill_switch(self) -> None:
        logger.warning("KILL SWITCH DEACTIVATED. Trading resumed.")
        self.rules["kill_switch_active"] = False
        self.kill_switch_reason = None
        self.kill_switch_activated_at = None

    def set_capital(self, new_capital: float) -> None:
        """
        Update the capital base used by every %-of-capital risk check
        (daily-loss limit, per-strategy budget, per-trade exposure cap).
        Called by the expiry-to-expiry capital-period rollover
        (src/portfolio/capital_periods.py) so profits/losses compound into
        next period's limits immediately, not just in reporting.
        """
        old_capital = self.initial_capital
        self.initial_capital = new_capital
        logger.info(f"RiskManager capital updated: Rs{old_capital:,.2f} -> Rs{new_capital:,.2f}")

    def get_kill_switch_status(self) -> Dict[str, Any]:
        return {
            "active":       self.rules["kill_switch_active"],
            "reason":       self.kill_switch_reason,
            "activated_at": self.kill_switch_activated_at,
        }

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
        capital_at_risk: Optional[float] = None,
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
        capital_at_risk : Explicit max-loss figure for the per-strategy capital
                        budget check (layer 5). Needed for credit_spread_v1/
                        iron_condor_v1: their only leg that reaches this check
                        (is_spread_leg=False) is a SELL, and the default
                        BUY-only trade_value computation below would always
                        see 0 for it — silently disabling the budget check for
                        both strategies entirely (found 2026-07-30). Callers
                        that already know the structure's true max loss before
                        placing the anchor leg (both spread strategies do —
                        strikes/net-credit are computed before any leg is
                        placed) should pass it here. Falls back to the
                        BUY-quantity*price / 0-for-SELL default when omitted,
                        so single-leg BUY strategies (ema_crossover_v1,
                        momentum_v1) are unaffected.
        """

        # ── -1. Quantity/price sanity check ───────────────────────────────────
        # A non-positive quantity or price makes trade_value = quantity * price
        # zero or negative, which trivially passes the max-exposure check
        # (layer 7: trade_value > max_allowed) and the per-strategy budget
        # check (layer 5: trade_value > 0 guards it, but a negative value
        # slips under deployed + trade_value > budget too). Reject outright,
        # before anything else runs — including the exit-order bypass below,
        # since a negative-quantity "exit" is exactly as invalid as a
        # negative-quantity entry.
        if quantity <= 0 or price <= 0:
            logger.error(
                f"Risk: invalid trade — quantity={quantity}, price={price} "
                f"(both must be > 0) for {side} {symbol}."
            )
            return False

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
        # EXPERIMENT (2026-10-09, explicit user instruction): fully deactivated
        # through 2026-10-23 (2 weeks) -- "I asked you to deactivate the kill
        # switch for 2 weeks." This supersedes the earlier same-day
        # MAX_DAILY_LOSS_PCT 5%->20% resize (a4be7e7/a6be7e7), which has been
        # reverted back to the original 5% -- that value only matters again
        # once this bypass expires, so it resumes at the conservative
        # default, not a still-loosened one, consistent with the "then we
        # start tightening" plan. Flagged plainly before making this change:
        # unlike every other gate in this experiment, there is now NO daily
        # loss circuit breaker at all during this window -- a bad day (or a
        # bug) has nothing automatic stopping it short of this date. The
        # kill switch FLAG and its manual activate/deactivate API (layer 1
        # above) are UNCHANGED -- a manual override is still possible.
        # Self-reactivates on the date below without needing a manual revert.
        from datetime import date as _date
        _DAILY_LOSS_AUTO_TRIP_DISABLED_UNTIL = _date(2026, 10, 23)
        if now_ist().date() >= _DAILY_LOSS_AUTO_TRIP_DISABLED_UNTIL:
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
        # Fixed 2026-08-28 (metrics-calculation audit): `is not None and ...`
        # meant a missing iv_rank/vix silently skipped this gate entirely
        # (fails OPEN), same bug class as vix_allows_selling()/
        # iv_rank_allows_selling() in option_chain.py, fixed earlier the same
        # day to fail closed. Real live entry paths already gate upstream via
        # those now-fixed functions, so this is defense-in-depth for any
        # other/future caller of validate_trade(), not a behavior change for
        # today's known call sites.
        if strategy_name in ("CREDIT_SPREAD", "IRON_CONDOR", "credit_spread_v1", "iron_condor_v1"):
            if iv_rank is None:
                logger.warning(
                    f"Risk: IV rank unavailable for {symbol} [{strategy_name}] "
                    "— cannot verify premium is rich enough, skipping."
                )
                return False
            # EXPERIMENT (2026-10-08, explicit user instruction): both
            # thresholds dropped to 0.0 -- these are defense-in-depth
            # duplicates of option_chain.py's iv_rank_allows_selling()/
            # vix_allows_selling(), loosened the same way and for the same
            # reason there. The None-fail-closed checks above/below are
            # UNCHANGED. Restore 0.30/12.0 after reviewing case-by-case losses.
            if iv_rank < 0.0:
                logger.warning(
                    f"Risk: IV rank {iv_rank:.2f} < 0.0 for {symbol} "
                    f"[{strategy_name}] — options too cheap, skipping."
                )
                return False
            if vix is None:
                logger.warning(
                    f"Risk: India VIX unavailable for {symbol} [{strategy_name}] "
                    "— cannot verify market-wide premium level, skipping."
                )
                return False
            if vix < 0.0:
                logger.warning(
                    f"Risk: India VIX {vix:.1f} < 0 — market unusually quiet, "
                    f"premiums too cheap market-wide, skipping [{strategy_name}]."
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
            # EXPERIMENT (2026-10-08, explicit user instruction: "bump the
            # budget to 10 lakhs... we want as much trades as possible"):
            # flat Rs10,00,000 per strategy, replacing the normal
            # initial_capital * alloc_pct formula. Found live: momentum_v1's
            # % share of the capital-period-compounded initial_capital (down
            # to ~Rs2,23,000 after cumulative paper losses) had shrunk its
            # budget to just Rs44,681, blocking every confirmed signal that
            # cycle even though no momentum_v1 position was actually open --
            # the % model couples "how much room a strategy gets to trade"
            # to "how much it has already lost," which fights directly
            # against this experiment's goal of generating enough case-by-
            # case data to evaluate the strategies at all. The real hard
            # ceiling is unchanged: the paper broker's own cash balance and
            # _check_available_margin() still cap what can physically be
            # deployed -- this only removes an additional, now-undersized
            # soft cap on top of that. Restore `self.initial_capital *
            # alloc_pct` after reviewing case-by-case losses.
            budget = 1_000_000.0
            deployed = self._strategy_deployed.get(strategy_name, 0.0)
            if capital_at_risk is not None:
                # Explicit max-loss figure from the caller (credit spreads/condors —
                # see capital_at_risk in the docstring above).
                trade_value = capital_at_risk
            elif side == "BUY":
                trade_value = quantity * price
            else:
                # For naked SELL legs (not covered by capital_at_risk) the margin is
                # taken by the broker, not our capital tracking.
                trade_value = 0.0
            if trade_value > 0 and deployed + trade_value > budget:
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
