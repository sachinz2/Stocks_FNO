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

    Entry-quality redesign (2026-08-20, external review integration):
    an external review argued this entry stack -- as it originally stood --
    selects for moves that have ALREADY happened rather than moves still
    developing: high-ADX + wide-EMA-spread + volume + top-10 RS are all
    lagging confirmations, so by the time every one of them agrees, the
    move is often spent. Real trade_journal data pulled the same day (11
    closed trades, 9.1% win rate, most losses showing the underlying
    reversing double-digit % after entry) was directionally consistent.
    Rather than a separate momentum_v2, the response was integrated
    directly here as four additional, independently-toggleable entry
    filters (adx_rising_required, ema_slope_required, extension_atr_mult,
    vwap_extension_pct -- see initialize()), a lower ADX floor paired with
    the rising requirement (25 vs. the old static 35), a raised RVOL floor,
    delta-based (near-ITM) instead of ATM strike selection, and a new
    underlying-based structural-invalidation exit in manage_position()
    (see its docstring). NOT done: a full pullback-then-breakout event
    model (the review's own preferred design) or changing the premium-
    based stop/target parameters themselves -- the real trade data showed
    the win/loss size RATIO was already healthier than assumed, so the
    entry timing looked like the more likely lever, not the exit sizing.
    """

    def initialize(self):
        self.fast_period = self.parameters.get("fast_period", 20)
        self.slow_period = self.parameters.get("slow_period", 50)

        # Entry: how strong and how established the trend must already be.
        #
        # Fixed 2026-08-20 (external review integration): lowered from 35.
        # An external review argued the whole entry stack (ADX>=35 + spread
        # already wide + 2-bar confirm + RVOL>=1.3 + top-10 RS) selects for
        # moves that have ALREADY happened -- "you're buying after the
        # momentum burst, not at the beginning of it." Real trade_journal
        # data pulled the same day (11 closed trades, 9.1% win rate, most
        # losses showing the underlying reversing double-digit % after
        # entry) was directionally consistent with that. Rather than just
        # requiring a high ADX plateau (which says nothing about whether the
        # trend is still accelerating or already exhausted), this threshold
        # is lower AND paired with adx_rising_required below -- "is momentum
        # accelerating right now" instead of "is ADX high right now."
        self.adx_entry_threshold = self.parameters.get("adx_entry_threshold", 25)
        self.min_ema_spread_pct  = self.parameters.get("min_ema_spread_pct", 0.30)

        # Fixed 2026-08-20 (external review integration): four additional
        # entry-quality filters, each independently toggleable (0/False
        # disables it) so any one can be tuned or turned off without a code
        # change if it proves too restrictive in practice.
        #
        # adx_rising_required: ADX must not be declining vs. 2 bars ago.
        # Distinguishes "27 -> 30 -> 34 -> 37" (accelerating, per the review's
        # own example) from "43 -> 42 -> 41 -> 40" (already exhausted) --
        # both satisfy a bare ADX>=35 threshold but are opposite situations.
        self.adx_rising_required = self.parameters.get("adx_rising_required", True)
        # ema_slope_required: EMA20 itself must be sloping in the signal's
        # direction vs. 2 bars ago, not just sitting above/below EMA50.
        self.ema_slope_required = self.parameters.get("ema_slope_required", True)
        # extension_atr_mult: reject entries where price is already this many
        # ATRs away from EMA20 -- "yes the trend is strong, but the move is
        # already too extended." The review's own "simplest improvement."
        self.extension_atr_mult = self.parameters.get("extension_atr_mult", 1.5)
        # vwap_extension_pct: reject entries this far (%) from session VWAP,
        # for the same reason -- price already run too far from the day's
        # volume-weighted average to be a fresh continuation entry.
        self.vwap_extension_pct = self.parameters.get("vwap_extension_pct", 1.5)
        # How many distinct-bar observations of ADX/EMA20 to keep per symbol
        # for the rising/slope checks above -- only needs enough to compare
        # "now" against "2 bars ago".
        self._HISTORY_LEN = 3

        # rvol_entry_threshold: read by live_trading_engine.py's shared RVOL
        # gate (getattr(strategy, "rvol_entry_threshold", 1.3)) -- momentum_v1
        # raises its own bar above the 1.3 floor ema_crossover_v1 still uses.
        # Per the review: a bare RVOL>1.3 just confirms a move is ALREADY
        # underway ("everybody's rushing in"), which can mean late rather
        # than early.
        self.rvol_entry_threshold = self.parameters.get("rvol_entry_threshold", 1.5)

        # entry_option_delta: read by live_trading_engine.py's single-leg
        # entry path -- if set, buys a strike near this delta instead of ATM
        # (0.60 ~ slightly ITM). Per the review: ATM options are maximally
        # sensitive to IV/theta/gamma noise relative to how much they track
        # the underlying; real intrinsic value makes the position behave
        # more like the underlying itself, which is what a trend-
        # continuation thesis is actually betting on. None/0 = ATM (matches
        # the previous, and ema_crossover_v1's still-current, behavior).
        self.entry_option_delta = self.parameters.get("entry_option_delta", 0.60)

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
        # Fixed 2026-08-20 (external review integration): additional exit --
        # see manage_position()'s new structural-invalidation check. Kept
        # toggleable in case it proves too aggressive against real EMA20
        # noise in practice.
        self.underlying_invalidation_exit = self.parameters.get("underlying_invalidation_exit", True)

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
        # Fixed 2026-08-20 (external review integration): short, bar-aligned
        # ADX/EMA20 history per symbol, feeding adx_rising_required/
        # ema_slope_required above. Advanced on the same genuinely-distinct-
        # bar basis as _pending_bar_key (see generate_signal()) -- reusing
        # that exact debounce avoids re-introducing the bar_key=None class
        # of bug fixed earlier the same day.
        self._adx_history: Dict[str, list] = {}
        self._ema_history: Dict[str, list] = {}
        self._history_bar_key: Dict[str, str] = {}

        logger.info(
            f"Initialized Momentum '{self.name}' ({self.fast_period}/{self.slow_period}) | "
            f"ADX entry>={self.adx_entry_threshold} (rising={self.adx_rising_required}) "
            f"exit<{self.adx_exit_threshold} (<{self.adx_exit_threshold_volatile} if VOLATILE-entered) | "
            f"min_EMA_spread={self.min_ema_spread_pct}% (slope={self.ema_slope_required}) | "
            f"extension<={self.extension_atr_mult}xATR vwap_ext<={self.vwap_extension_pct}% | "
            f"RVOL>={self.rvol_entry_threshold} | delta_target={self.entry_option_delta or 'ATM'} | "
            f"SL={self.stop_loss_pct:.0%} WeakeningSL={self.weakening_stop_loss_pct:.0%} "
            f"TP={self.target_pct:.0%} Trail={self.trailing_stop_pct:.0%} "
            f"Breakeven activates at +{self.breakeven_activation_pct:.0%} | "
            f"UnderlyingInvalidation={self.underlying_invalidation_exit} | "
            f"ConfirmBars={self.signal_confirm_bars}"
        )

    def generate_signal(self, data: Dict[str, Any]) -> Optional[str]:
        """
        Expects data dict containing:
        - symbol: which underlying this tick belongs to.
        - ema20/ema50 (or dynamic fast/slow_period keys), adx14, ohlc_bar_key.
        - close, atr14, vwap (or session_vwap) -- used by the entry-quality
          filters added 2026-08-20 (external review integration); their
          absence degrades those specific filters gracefully (skipped, not
          fail-closed -- unlike the RS/MTF entry filters in
          live_trading_engine.py, these are refinements ON TOP OF an
          already-qualifying ADX/EMA signal, not the sole gate on a trade).
        """
        symbol   = data.get("symbol", "")
        fast_ema = data.get(f"ema{self.fast_period}")
        slow_ema = data.get(f"ema{self.slow_period}")
        adx      = data.get("adx14")
        bar_key  = data.get("ohlc_bar_key")

        if fast_ema is None or slow_ema is None or adx is None or not slow_ema:
            return "HOLD"

        ema_spread_pct = abs(fast_ema - slow_ema) / slow_ema * 100

        # Fixed 2026-08-20 (external review integration): bar-aligned ADX/
        # EMA20 history, advanced only on a genuinely new, identifiable bar
        # -- same debounce as _pending_bar_key below, kept as a SEPARATE key
        # (_history_bar_key) since history should keep accumulating even
        # while no candidate direction is pending (_pending_bar_key gets
        # cleared whenever the ADX/spread condition drops out).
        if bar_key is not None and bar_key != self._history_bar_key.get(symbol):
            self._adx_history.setdefault(symbol, []).append(adx)
            self._ema_history.setdefault(symbol, []).append(fast_ema)
            self._adx_history[symbol] = self._adx_history[symbol][-self._HISTORY_LEN:]
            self._ema_history[symbol] = self._ema_history[symbol][-self._HISTORY_LEN:]
            self._history_bar_key[symbol] = bar_key

        raw = None
        if adx >= self.adx_entry_threshold and ema_spread_pct >= self.min_ema_spread_pct:
            raw = "BUY" if fast_ema > slow_ema else "SELL"

        # Fixed 2026-08-20 (external review integration): four additional
        # entry-quality filters -- see initialize()'s parameter comments for
        # the rationale on each. All fail CLOSED on insufficient history
        # (not enough distinct bars seen yet to compare against) rather than
        # assuming the condition holds, consistent with this codebase's
        # convention for entry-blocking data elsewhere.
        if raw is not None:
            hist_adx = self._adx_history.get(symbol, [])
            hist_ema = self._ema_history.get(symbol, [])

            if self.adx_rising_required:
                if len(hist_adx) < 2 or adx < hist_adx[-2]:
                    logger.debug(
                        f"[{self.name}] {symbol} {raw} candidate rejected — "
                        f"ADX not rising (history={hist_adx})"
                    )
                    raw = None

            if raw is not None and self.ema_slope_required:
                if len(hist_ema) < 2:
                    logger.debug(
                        f"[{self.name}] {symbol} {raw} candidate rejected — "
                        "insufficient EMA20 history to confirm slope"
                    )
                    raw = None
                elif raw == "BUY" and fast_ema < hist_ema[-2]:
                    logger.debug(f"[{self.name}] {symbol} BUY candidate rejected — EMA20 not sloping up")
                    raw = None
                elif raw == "SELL" and fast_ema > hist_ema[-2]:
                    logger.debug(f"[{self.name}] {symbol} SELL candidate rejected — EMA20 not sloping down")
                    raw = None

            if raw is not None and self.extension_atr_mult > 0:
                close = data.get("close")
                atr = data.get("atr14")
                if close and atr and atr > 0 and abs(close - fast_ema) / atr > self.extension_atr_mult:
                    logger.info(
                        f"[{self.name}] {symbol} {raw} candidate rejected — "
                        f"price {abs(close - fast_ema) / atr:.2f}x ATR from EMA20 "
                        f"(> {self.extension_atr_mult}x, too extended)"
                    )
                    raw = None

            if raw is not None and self.vwap_extension_pct > 0:
                close = data.get("close")
                vwap = data.get("vwap") or data.get("session_vwap")
                if close and vwap and vwap > 0:
                    _vwap_dist_pct = abs(close - vwap) / vwap * 100
                    if _vwap_dist_pct > self.vwap_extension_pct:
                        logger.info(
                            f"[{self.name}] {symbol} {raw} candidate rejected — "
                            f"{_vwap_dist_pct:.2f}% from VWAP (> {self.vwap_extension_pct}%, too extended)"
                        )
                        raw = None

        signal = "HOLD"
        if raw is not None:
            if raw != self._pending_signal.get(symbol):
                # New direction — start fresh
                self._pending_signal[symbol] = raw
                self._pending_count[symbol] = 1
                self._pending_bar_key[symbol] = bar_key
            elif bar_key is not None and bar_key != self._pending_bar_key.get(symbol):
                # Same direction AND we're on a genuinely new, identifiable 5-min bar
                self._pending_count[symbol] = self._pending_count.get(symbol, 0) + 1
                self._pending_bar_key[symbol] = bar_key
            # Fixed 2026-08-20 (external review): bar_key is None whenever
            # ltp_poller can't identify the current 5-min bar (no live tick
            # data yet, or a malformed OHLC cache missing "date") -- the old
            # `bar_key is None or ...` condition treated EVERY such cycle as
            # a "new bar," so signal_confirm_bars could complete in a couple
            # of 60s engine cycles instead of 2 genuinely distinct candles,
            # defeating the point of the confirmation debounce. A missing
            # bar_key now simply can't advance the count (falls through to
            # neither branch above) -- same bar or unknown bar, don't
            # double-count either way.

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
          - current_close, current_ema_fast, is_call : optional, feed the
                              structural-invalidation exit added 2026-08-20;
                              that check is skipped (not fail-closed) if
                              missing.

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
          3. Structural invalidation (added 2026-08-20) — underlying's close
                                  crossed back to the wrong side of EMA20,
                                  breaking the trend-participation thesis the
                                  position was entered on, regardless of
                                  premium P&L. Toggle via
                                  underlying_invalidation_exit.
          4. Profit target      — premium rose >= target_pct from entry
          5. Trailing stop      — premium fell >= trailing_stop_pct from its peak
          6. Breakeven stop     — once up >= breakeven_activation_pct from entry,
                                  never allow a close below entry (see below)
          7. Trend exhaustion   — ADX has dropped below adx_exit_threshold, i.e.
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

        # Fixed 2026-08-20 (external review integration): underlying-based
        # structural invalidation -- "your stop should be based on the
        # underlying, not option premium," since the entry thesis was
        # "EMA20 is meaningfully above/below EMA50 AND price is
        # participating in that trend." If the underlying's current close
        # crosses back to the wrong side of EMA20, that thesis is broken
        # regardless of where option premium P&L currently sits (theta/IV
        # can mask a real reversal for a while). Kept ADDITIONAL to, not a
        # replacement for, the premium-based stops above -- premium is what
        # real money is actually gained/lost in, so it stays authoritative;
        # this is a faster-reacting early-warning cut. Toggle via
        # underlying_invalidation_exit if it proves too twitchy against
        # normal EMA20 noise in practice.
        if self.underlying_invalidation_exit:
            current_close = current_position.get("current_close")
            current_ema_fast = current_position.get("current_ema_fast")
            is_call = current_position.get("is_call")
            if current_close and current_ema_fast and current_close > 0 and current_ema_fast > 0:
                if is_call is True and current_close < current_ema_fast:
                    logger.info(
                        f"[{self.name}] Structural invalidation: underlying closed "
                        f"below EMA20 ({current_close:.2f} < {current_ema_fast:.2f}) "
                        "-- uptrend thesis broken, exiting."
                    )
                    return "EXIT"
                if is_call is False and current_close > current_ema_fast:
                    logger.info(
                        f"[{self.name}] Structural invalidation: underlying closed "
                        f"above EMA20 ({current_close:.2f} > {current_ema_fast:.2f}) "
                        "-- downtrend thesis broken, exiting."
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
        # Fixed 2026-08-20 (external review integration): same staleness
        # risk as _pending_* above -- a gap across the pause shouldn't count
        # as "ADX/EMA20 rising" once resumed, since bars during the pause
        # were never observed.
        self._adx_history.clear()
        self._ema_history.clear()
        self._history_bar_key.clear()

    def shutdown(self):
        pass
