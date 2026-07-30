import logging
from typing import Dict, Any, Optional
from src.strategies.base import StrategyBase, StrategyRegistry

logger = logging.getLogger(__name__)


@StrategyRegistry.register("MOMENTUM")
class MomentumStrategy(StrategyBase):
    """
    Trend-continuation strategy — buys directional options on an ALREADY
    established strong trend (high ADX + meaningful EMA20/50 separation),
    unlike EMACrossoverStrategy which needs a FRESH sign-change event.

    Exists specifically to capture strong-trend days where ADX is high but no
    clean crossover ever fires — confirmed 2026-07-30: on a day with ADX up to
    55 on several stocks, ema_crossover_v1 got no fresh cross (several stocks
    were already deep in an established trend, past the point a cross could
    recur) and credit_spread_v1 explicitly skipped every ADX>30 candidate
    (correct for it — blowthrough risk for a premium seller), so nothing in
    the system captured that move. This strategy targets exactly that gap.

    Rules:
    - BUY when ADX >= adx_entry_threshold AND EMA20 is meaningfully above
      EMA50 (ema_spread_pct >= min_ema_spread_pct), sustained for
      signal_confirm_bars distinct 5-min bars.
    - SELL for the mirrored downtrend (EMA20 meaningfully below EMA50).
    - No fresh crossover required — the strategy explicitly wants stocks
      ALREADY trending hard, the opposite selection criterion from EMA
      crossover's "near a cross, not deep in a trend" pool.
    """

    def initialize(self):
        self.fast_period = self.parameters.get("fast_period", 20)
        self.slow_period = self.parameters.get("slow_period", 50)

        # Entry: how strong and how established the trend must already be.
        self.adx_entry_threshold = self.parameters.get("adx_entry_threshold", 35)
        self.min_ema_spread_pct  = self.parameters.get("min_ema_spread_pct", 0.30)

        # Exit: stop loss / profit target / trailing stop on the option premium
        # (same mechanics as EMACrossoverStrategy), plus a trend-exhaustion exit
        # unique to this strategy — see manage_position().
        self.stop_loss_pct     = self.parameters.get("stop_loss_pct", 0.50)
        self.target_pct        = self.parameters.get("target_pct", 1.50)
        self.trailing_stop_pct = self.parameters.get("trailing_stop_pct", 0.30)
        self.adx_exit_threshold = self.parameters.get("adx_exit_threshold", 22)

        # Signal confirmation: the ADX/EMA-spread condition must hold for this
        # many distinct 5-min bars before firing — same debouncing principle as
        # EMACrossoverStrategy's signal_confirm_bars, needed because ADX/EMA
        # spread can transiently spike without the trend actually continuing.
        self.signal_confirm_bars: int = self.parameters.get("signal_confirm_bars", 2)
        self.min_dte: int = self.parameters.get("min_dte", 10)
        self.max_dte: int = self.parameters.get("max_dte", 25)

        # Keyed by symbol — this single strategy instance evaluates every symbol
        # in its rotating top-5 pool each cycle, so flat scalars would let one
        # symbol's state leak into the next symbol's check within the same cycle.
        self._pending_signal: Dict[str, str] = {}
        self._pending_count: Dict[str, int] = {}
        self._pending_bar_key: Dict[str, str] = {}

        logger.info(
            f"Initialized Momentum '{self.name}' ({self.fast_period}/{self.slow_period}) | "
            f"ADX entry>={self.adx_entry_threshold} exit<{self.adx_exit_threshold} | "
            f"min_EMA_spread={self.min_ema_spread_pct}% | "
            f"SL={self.stop_loss_pct:.0%} TP={self.target_pct:.0%} Trail={self.trailing_stop_pct:.0%} "
            f"ConfirmBars={self.signal_confirm_bars}"
        )

    def generate_signal(self, data: Dict[str, Any]) -> Optional[str]:
        """
        Expects data dict containing:
        - symbol: which underlying this tick belongs to.
        - ema20/ema50 (or dynamic fast/slow_period keys), adx14, ohlc_bar_key.
        """
        symbol   = data.get("symbol", "")
        fast_ema = data.get(f"ema{self.fast_period}")
        slow_ema = data.get(f"ema{self.slow_period}")
        adx      = data.get("adx14")
        bar_key  = data.get("ohlc_bar_key")

        if fast_ema is None or slow_ema is None or adx is None or not slow_ema:
            return "HOLD"

        ema_spread_pct = abs(fast_ema - slow_ema) / slow_ema * 100

        raw = None
        if adx >= self.adx_entry_threshold and ema_spread_pct >= self.min_ema_spread_pct:
            raw = "BUY" if fast_ema > slow_ema else "SELL"

        signal = "HOLD"
        if raw is not None:
            if raw != self._pending_signal.get(symbol):
                # New direction — start fresh
                self._pending_signal[symbol] = raw
                self._pending_count[symbol] = 1
                self._pending_bar_key[symbol] = bar_key
            elif bar_key is None or bar_key != self._pending_bar_key.get(symbol):
                # Same direction AND we're on a new 5-min bar
                self._pending_count[symbol] = self._pending_count.get(symbol, 0) + 1
                self._pending_bar_key[symbol] = bar_key
            # else: same bar as last cycle — don't double-count

            if self._pending_count.get(symbol, 0) >= self.signal_confirm_bars:
                logger.info(
                    f"[{self.name}] {symbol} {raw} confirmed after "
                    f"{self._pending_count[symbol]} bars (ADX={adx:.1f}, "
                    f"spread={ema_spread_pct:.2f}%) — firing."
                )
                signal = raw
                self._pending_signal.pop(symbol, None)
                self._pending_count.pop(symbol, None)
                self._pending_bar_key.pop(symbol, None)
            else:
                logger.debug(
                    f"[{self.name}] {symbol} {raw} pending "
                    f"({self._pending_count.get(symbol, 0)}/{self.signal_confirm_bars} bars)"
                )
        else:
            # Trend condition no longer met this bar — clear pending
            self._pending_signal.pop(symbol, None)
            self._pending_count.pop(symbol, None)
            self._pending_bar_key.pop(symbol, None)

        return signal

    def manage_position(self, current_position: Dict[str, Any], current_premium: float) -> Optional[str]:
        """
        Options position management based on option premium movement, plus a
        trend-exhaustion exit unique to this strategy.

        current_position must contain:
          - avg_price      : entry premium paid
          - peak_premium   : highest premium seen since entry (tracked by engine)
          - current_adx    : underlying's current ADX14 (optional — only the
                              exhaustion exit uses it; other checks run without it)

        Exit conditions (in priority order):
          1. Hard stop loss     — premium fell >= stop_loss_pct from entry
          2. Profit target      — premium rose >= target_pct from entry
          3. Trailing stop      — premium fell >= trailing_stop_pct from its peak
          4. Trend exhaustion   — ADX has dropped below adx_exit_threshold, i.e.
                                  the established trend that justified entry has
                                  since weakened. Unlike EMA crossover (which
                                  waits for premium-based signals only), this
                                  strategy's entire thesis is "the trend
                                  continues" — once ADX confirms it no longer
                                  does, there's no reason to keep holding even
                                  if SL/TP haven't been hit yet.
        """
        entry_premium = float(current_position.get("avg_price") or 0)
        if entry_premium <= 0 or current_premium <= 0:
            return "HOLD"

        pnl_pct = (current_premium - entry_premium) / entry_premium

        if pnl_pct <= -self.stop_loss_pct:
            logger.info(
                f"[{self.name}] Stop loss hit: entry={entry_premium:.2f} "
                f"current={current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        if pnl_pct >= self.target_pct:
            logger.info(
                f"[{self.name}] Profit target hit: entry={entry_premium:.2f} "
                f"current={current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        peak = float(current_position.get("peak_premium") or entry_premium)
        if peak > entry_premium:
            drawdown_from_peak = (peak - current_premium) / peak
            if drawdown_from_peak >= self.trailing_stop_pct:
                logger.info(
                    f"[{self.name}] Trailing stop hit: peak={peak:.2f} "
                    f"current={current_premium:.2f} ({drawdown_from_peak:.1%} off peak)"
                )
                return "EXIT"

        current_adx = current_position.get("current_adx")
        if current_adx is not None and float(current_adx) < self.adx_exit_threshold:
            logger.info(
                f"[{self.name}] Trend exhausted: ADX={float(current_adx):.1f} "
                f"< {self.adx_exit_threshold} — exiting regardless of premium P&L."
            )
            return "EXIT"

        return "HOLD"

    def shutdown(self):
        pass
