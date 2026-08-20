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
        # Weakening-trend stop (added 2026-08-14) -- closes the dead zone
        # between adx_exit_threshold and adx_entry_threshold. Once ADX drops
        # below adx_entry_threshold, the trend is no longer strong enough to
        # reconfirm this position's own entry condition, but it can still sit
        # above adx_exit_threshold indefinitely (ADX is a smoothed, lagging
        # indicator that doesn't reflect a fresh reversal quickly). Confirmed
        # live 2026-08-14: TATASTEEL26AUG180PE entered at ADX=52.2, stopped
        # reconfirming after ADX fell under 35 by ~11:10, but never dropped
        # below the 22 trend-exhaustion threshold before exit -- so the only
        # stop that ever fired was the full 50% hard stop, 2h17m and a much
        # larger loss later. Once the trend is no longer fresh (ADX below
        # entry threshold), this tighter stop applies instead of waiting for
        # the full hard-stop drawdown.
        self.weakening_stop_loss_pct = self.parameters.get("weakening_stop_loss_pct", 0.25)
        # Once a position has been up at least this much from entry, it must
        # never be allowed to close as a realized loss -- see manage_position()'s
        # breakeven-stop check for why trailing_stop_pct alone doesn't guarantee this.
        self.breakeven_activation_pct = self.parameters.get("breakeven_activation_pct", 0.15)
        self.adx_exit_threshold = self.parameters.get("adx_exit_threshold", 22)
        # Tighter ADX-exhaustion threshold for positions entered during
        # VOLATILE (VIX>20) — added 2026-07-31, see manage_position(). A
        # crash-catching entry should bail on trend-decay faster than a normal
        # trending-day entry; 22 lets a lot of decay happen first, appropriate
        # for a steady trend but too slow for a panic that can V-reverse fast.
        self.adx_exit_threshold_volatile = self.parameters.get("adx_exit_threshold_volatile", 30)

        # Signal confirmation: the ADX/EMA-spread condition must hold for this
        # many distinct 5-min bars before firing — same debouncing principle as
        # EMACrossoverStrategy's signal_confirm_bars, needed because ADX/EMA
        # spread can transiently spike without the trend actually continuing.
        self.signal_confirm_bars: int = self.parameters.get("signal_confirm_bars", 2)
        self.min_dte: int = self.parameters.get("min_dte", 10)
        # Fixed 2026-08-20: 25 left a structural monthly dead zone -- see
        # EMACrossoverStrategy.initialize()'s matching comment for the full
        # explanation (both strategies share the same _process_signal entry
        # path and get_near_month_expiry()-based expiry resolution). Confirmed
        # live: zero momentum_v1/ema_crossover_v1 orders 2026-07-25..08-05 and
        # again 2026-08-17..08-20, both immediately after a monthly roll.
        self.max_dte: int = self.parameters.get("max_dte", 42)

        # Keyed by symbol — this single strategy instance evaluates every symbol
        # in its rotating top-5 pool each cycle, so flat scalars would let one
        # symbol's state leak into the next symbol's check within the same cycle.
        self._pending_signal: Dict[str, str] = {}
        self._pending_count: Dict[str, int] = {}
        self._pending_bar_key: Dict[str, str] = {}

        logger.info(
            f"Initialized Momentum '{self.name}' ({self.fast_period}/{self.slow_period}) | "
            f"ADX entry>={self.adx_entry_threshold} exit<{self.adx_exit_threshold} "
            f"(<{self.adx_exit_threshold_volatile} if VOLATILE-entered) | "
            f"min_EMA_spread={self.min_ema_spread_pct}% | "
            f"SL={self.stop_loss_pct:.0%} WeakeningSL={self.weakening_stop_loss_pct:.0%} "
            f"TP={self.target_pct:.0%} Trail={self.trailing_stop_pct:.0%} "
            f"Breakeven activates at +{self.breakeven_activation_pct:.0%} | "
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
          2. Weakening-trend stop — premium fell >= weakening_stop_loss_pct
                                  from entry AND ADX has already dropped below
                                  adx_entry_threshold (the trend no longer
                                  reconfirms this position's own entry
                                  condition). Tighter than the hard stop,
                                  since a trend that's stopped reconfirming
                                  itself shouldn't get the full drawdown
                                  before being cut loose — see initialize().
          3. Profit target      — premium rose >= target_pct from entry
          4. Trailing stop      — premium fell >= trailing_stop_pct from its peak
          5. Breakeven stop     — once up >= breakeven_activation_pct from entry,
                                  never allow a close below entry (see below)
          6. Trend exhaustion   — ADX has dropped below adx_exit_threshold, i.e.
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
        current_adx = current_position.get("current_adx")

        if pnl_pct <= -self.stop_loss_pct:
            logger.info(
                f"[{self.name}] Stop loss hit: entry={entry_premium:.2f} "
                f"current={current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        if (
            current_adx is not None
            and float(current_adx) < self.adx_entry_threshold
            and pnl_pct <= -self.weakening_stop_loss_pct
        ):
            logger.info(
                f"[{self.name}] Weakening-trend stop: ADX={float(current_adx):.1f} "
                f"< entry threshold {self.adx_entry_threshold} and premium down "
                f"{pnl_pct:.1%} (>= {self.weakening_stop_loss_pct:.0%}) -- exiting "
                "before the full hard stop since the trend no longer reconfirms."
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

        # Breakeven stop (added 2026-08-13) -- trailing_stop_pct above is
        # scoped to the PEAK premium, not to locked-in profit. For a peak
        # gain below roughly trailing_stop_pct/(1-trailing_stop_pct) (~43%
        # at the default 30%), trailing_stop_pct off that peak lands BELOW
        # the entry price -- meaning a position that was genuinely
        # profitable at some point could still round-trip all the way into
        # a realized loss before the trailing stop ever fires. Confirmed
        # live 2026-08-11: ULTRACEMCO26AUG11920PE peaked at +31.9% (entry
        # Rs54.04 -> peak Rs71.30) and only exited once it had already
        # fallen to Rs47.34, a real loss of -Rs335, because the trailing
        # stop's own 30%-off-peak floor (~Rs49.91) was still below entry.
        # Once a position has been up at least breakeven_activation_pct
        # from entry, it must never be allowed to close as a realized loss
        # -- exit the moment it falls back to entry, regardless of whether
        # the trailing stop's own (looser) threshold has been reached yet.
        if peak >= entry_premium * (1 + self.breakeven_activation_pct) and current_premium <= entry_premium:
            logger.info(
                f"[{self.name}] Breakeven stop: was up to {peak:.2f} "
                f"(entry {entry_premium:.2f}), now back to {current_premium:.2f} -- "
                "exiting to avoid a profitable trade becoming a loss."
            )
            return "EXIT"

        # VOLATILE-entered positions (see REGIME_STRATEGY_MAP /
        # live_trading_engine.py._process_signal's VOLATILE gate, added
        # 2026-07-31) use a tighter threshold — a crash-catching entry should
        # bail on trend-decay faster than a normal trending-day entry, since a
        # genuine panic can V-reverse fast. Smaller profit is acceptable;
        # riding the reversal is not.
        adx_exit = (
            self.adx_exit_threshold_volatile
            if current_position.get("entry_regime") == "VOLATILE"
            else self.adx_exit_threshold
        )
        if current_adx is not None and float(current_adx) < adx_exit:
            logger.info(
                f"[{self.name}] Trend exhausted: ADX={float(current_adx):.1f} "
                f"< {adx_exit} — exiting regardless of premium P&L."
            )
            return "EXIT"

        return "HOLD"

    def on_pause(self) -> None:
        """Clear the confirmation buffer so a stale pending signal can't fire
        the moment this strategy resumes.

        Without this (found 2026-07-30), a regime-triggered pause (this
        strategy only runs in TRENDING — see regime_detector.py) freezes
        _pending_count mid-confirmation instead of resetting it, since
        generate_signal() is never called while paused (see
        LiveTradingEngine._process_signal's `if not strategy.is_active:
        return`). On resume, if the trend condition is still true, the very
        first post-resume cycle can complete signal_confirm_bars using a
        count accumulated before the pause plus a single fresh bar —
        firing with zero genuinely fresh confirmation bars since resume.
        EMACrossoverStrategy already guards against this same risk in its
        own on_pause(); this mirrors it.
        """
        if self._pending_signal:
            logger.info(
                f"[{self.name}] on_pause: clearing pending signals for "
                f"{list(self._pending_signal.keys())}"
            )
        self._pending_signal.clear()
        self._pending_count.clear()
        self._pending_bar_key.clear()

    def shutdown(self):
        pass
