"""
Iron Condor Strategy — range-bound theta collection.

Fires when a stock is in the "dead zone": ATR% is LOW (not making explosive moves)
AND the EMA is FLAT (no clear directional trend).

This is the 3rd pillar, complementing the other two without ever conflicting:
  EMA Crossover   → high ATR%, clear EMA cross    (momentum long option)
  Credit Spread   → low ATR%, EMA directional      (mild trend, one-sided premium)
  Iron Condor     → low ATR%, EMA flat             (sideways, collect from both sides)

Structure (defined-risk on both sides):
  PUT  wing: SELL OTM PE (ATM - 1 interval) + BUY further OTM PE (ATM - 3 intervals)
  CALL wing: SELL OTM CE (ATM + 1 interval) + BUY further OTM CE (ATM + 3 intervals)

Net credit collected = two wing premiums minus two hedge costs.
Max profit: entire net credit (both short legs expire worthless).
Max loss:   wider of the two wing spreads minus net credit (capped, defined).

Exit triggers (managed by engine's _check_condor_exits):
  1. DTE < 7                        — close before gamma risk explodes near expiry
  2. Underlying breaches short put OR short call strike  — stop immediately
  3. Either short leg rises to 2× sold price             — stop loss on that wing
  4. Either short leg decays to 25% of sold price         — lock in profit, close entire condor
"""
import logging
from typing import Any, Dict, Optional

from src.strategies.base import StrategyBase, StrategyRegistry

logger = logging.getLogger(__name__)


@StrategyRegistry.register("IRON_CONDOR")
class IronCondorStrategy(StrategyBase):

    def initialize(self):
        self.fast_period = self.parameters.get("fast_period", 20)
        self.slow_period = self.parameters.get("slow_period", 50)
        # Below this ATR% the market is not making explosive moves
        self.low_vol_threshold = self.parameters.get("low_vol_threshold", 1.2)
        # Below this EMA spread% the trend is flat — condor is appropriate
        self.flat_threshold = self.parameters.get("flat_threshold", 0.1)
        # Close at 75% profit: short legs have decayed to this fraction of sold price
        self.profit_close_pct = self.parameters.get("profit_close_pct", 0.25)
        # Stop loss: close if either short leg rises to this multiple of sold price
        self.stop_loss_multiple = self.parameters.get("stop_loss_multiple", 2.0)
        # Days before expiry to force-close (gamma risk)
        self.min_dte = self.parameters.get("min_dte", 7)

        logger.info(
            f"Initialized Iron Condor '{self.name}' | "
            f"low_vol={self.low_vol_threshold}% | flat_EMA={self.flat_threshold}% | "
            # Fixed 2026-09-04 (thorough cross-check of all strategies):
            # short_offset/hedge_offset used to be logged here as if they
            # drove strike selection -- they never did. The engine's
            # _process_iron_condor() selects all 4 strikes purely by
            # Black-Scholes delta targeting (short legs ~0.20 delta, long
            # legs ~0.10 delta via find_delta_strike()); short_offset/
            # hedge_offset were read into self.* but never consulted by
            # that path. Removed as dead config (see git history for the
            # 1/2-interval literals this used to describe); log now
            # describes what actually determines the strikes.
            f"strike_selection=delta-targeted (short~0.20/0.10 delta) | "
            f"profit_target={int((1 - self.profit_close_pct) * 100)}% | "
            f"SL={self.stop_loss_multiple}x | min_DTE={self.min_dte}"
        )

    def generate_signal(self, data: Dict[str, Any]) -> str:
        """
        Returns 'IRON_CONDOR' only when:
          - ATR% < low_vol_threshold (not making explosive moves)
          - EMA spread% < flat_threshold (no directional bias)
        Returns 'HOLD' in all other cases.

        The other strategies handle the other regimes:
          ATR% >= 1.2%              → EMA crossover handles it
          ATR% < 1.2%, EMA trending → Credit spread handles it
        """
        fast_ema = data.get(f"ema{self.fast_period}")
        slow_ema = data.get(f"ema{self.slow_period}")
        close = data.get("close", 0)
        atr = data.get("atr14", 0)

        # Fixed 2026-08-20 (deep review): same NaN/None atr14 bypass fixed in
        # credit_spread.py -- see its comment for the failure scenario.
        # Fixed 2026-08-21 (deep review): the atr != atr NaN check above
        # didn't cover fast_ema/slow_ema -- `not float('nan')` is False (NaN
        # is truthy), so a NaN EMA slipped past this guard and then hit
        # `... if slow_ema > 0 else 0` below, where `NaN > 0` is also False,
        # silently producing ema_spread_pct=0 -- exactly the flat-market
        # entry condition -- instead of HOLD.
        if (
            not fast_ema or not slow_ema or not close
            or fast_ema != fast_ema or slow_ema != slow_ema or close != close
            or atr is None or atr != atr
        ):
            return "HOLD"

        atr_pct = (atr / close * 100) if close > 0 else 0
        ema_spread_pct = abs(fast_ema - slow_ema) / slow_ema * 100 if slow_ema > 0 else 0

        if atr_pct >= self.low_vol_threshold:
            return "HOLD"  # EMA crossover territory

        if ema_spread_pct >= self.flat_threshold:
            return "HOLD"  # credit spread territory

        return "IRON_CONDOR"

    def manage_position(
        self, current_position: Dict[str, Any], current_short_premium: float
    ) -> Optional[str]:
        """
        Evaluate a single wing of the condor (put wing or call wing).
        Called by the engine with 'short_premium' (original sold price of that wing's short leg).
        Returns 'EXIT' if the wing's stop or profit target is triggered, else 'HOLD'.
        The engine closes the ENTIRE condor when either wing returns EXIT.

        NOT actually called by the live engine: _check_condor_exits() in
        live_trading_engine.py reimplements this same threshold logic inline instead,
        since a condor needs both wings evaluated independently against different
        strikes in the same pass, which this single-wing signature can't express in
        one call. Kept only to satisfy StrategyBase's abstract-method contract — the
        two implementations should be kept in sync by hand if either changes.
        """
        short_premium = float(current_position.get("short_premium") or 0)
        if short_premium <= 0:
            return "HOLD"

        # Take profit: this short leg has decayed to ≤25% of sold value
        if current_short_premium <= short_premium * self.profit_close_pct:
            return "EXIT"

        # Stop loss: this short leg has risen to ≥2× sold value (wrong direction)
        if current_short_premium >= short_premium * self.stop_loss_multiple:
            return "EXIT"

        return "HOLD"

    def shutdown(self):
        logger.info(f"Shutting down Iron Condor Strategy '{self.name}'")
