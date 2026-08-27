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
    underlying-based structural-invalidation exit in manage_position().

    Round 2 (2026-08-21, same review -- remaining recommendations): the
    user asked for a precise accounting of what from the review was still
    unimplemented and to implement it. Added here:
    - A real pullback-then-breakout EVENT-based confirmation model
      (use_pullback_continuation_model, see generate_signal() and
      _pullback_continuation_signal()) -- the review's own preferred
      design ("confirm an event, not a state"), replacing the flat
      N-consecutive-bars debounce as the default path. The old debounce
      logic is kept, selectable via the same toggle, for rollback safety.
    - A two-tier RVOL breakout confirmation (pullback_rvol_low /
      breakout_rvol_min) -- a genuine volume contraction during the
      pullback followed by expansion on the breakout bar is treated as a
      stronger signal than a flat RVOL floor alone.
    - Underlying-based stop AND target as the PRIMARY exit driver
      (underlying_stop_atr_mult / underlying_target_atr_mult), checked
      first in manage_position(), ahead of the premium-based stop/target
      which remain as a backstop.
    NOT done (see docs/LIVE_TRADING_CHECKLIST.md for the full reasoning):
    DTE range A/B testing (10-25 vs 20-35) -- the review itself frames this
    as "test both," and no live A/B-testing infrastructure exists to do
    that without guessing; the full 5m/15m/30m MFE/MAE snapshot schema is
    approximated instead by a simpler running-since-entry MFE/MAE (see
    live_trading_engine.py's _single_leg_journals tracking and
    trade_journal's new columns) rather than fixed-offset snapshots.
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
        #
        # Fixed 2026-08-27 (trade review): confirmed live that 1.5x was
        # miscalibrated for actual NSE F&O intraday behavior, not just
        # occasionally tight -- zero momentum_v1 trades fired over 3 full
        # trading days (Aug 24-26), and grepping every one of the 1,449
        # "too extended" rejections logged in that window showed a MEDIAN
        # extension of 2.69x ATR among candidates that had already passed
        # every other gate (ADX>=25 rising, EMA20/50 spread, EMA20 sloping)
        # -- only 15% were even under 2.0x. By the time a setup takes
        # several bars to build enough ADX/spread/slope to qualify, price
        # has typically already moved well past 1.5x ATR from EMA20 in this
        # market; the filter was rejecting the qualifying population
        # almost entirely, not screening out its extreme tail. Not one
        # single candidate reached the pullback+breakout state machine
        # below in any of the 3 days -- the strategy's actual entry logic
        # never got a chance to run. Raised to 2.5x, just above the
        # observed p25 (2.2x) -- still screens out the genuinely
        # blown-out top quartile+ (p75 was 3.26x) while letting normal
        # trending-day setups through to the pullback model, which already
        # provides its own protection against chasing an exhausted move by
        # requiring a real pullback-then-breakout event rather than an
        # immediate entry.
        self.extension_atr_mult = self.parameters.get("extension_atr_mult", 2.5)
        # vwap_extension_pct: reject entries this far (%) from session VWAP,
        # for the same reason -- price already run too far from the day's
        # volume-weighted average to be a fresh continuation entry.
        #
        # Fixed 2026-08-27 (trade review): same miscalibration as
        # extension_atr_mult above, same evidence window -- the (much
        # smaller, since extension_atr_mult already rejected most
        # candidates first) sample of VWAP rejections that did occur had a
        # median of 2.33%, well past the old 1.5% floor. Raised to 2.5% to
        # stop compounding the extension fix's effect with a second filter
        # tuned against the same wrong assumption.
        self.vwap_extension_pct = self.parameters.get("vwap_extension_pct", 2.5)
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

        # Fixed 2026-08-21 (external review, round 2 -- sections 20-21):
        # underlying-based stop AND target, checked FIRST in
        # manage_position() (primary exit driver) -- "your stop should be
        # based on the underlying's actual move, not option premium," since
        # premium reacts to IV/theta/gamma noise on top of the underlying's
        # real move. 0/None disables either independently. Requires
        # entry_underlying_price/entry_atr (captured by the engine at entry
        # -- see live_trading_engine.py's _single_leg_journals) and skips
        # gracefully, like the structural-invalidation exit above, if
        # either is missing (e.g. an older restored position).
        self.underlying_stop_atr_mult   = self.parameters.get("underlying_stop_atr_mult", 1.0)
        self.underlying_target_atr_mult = self.parameters.get("underlying_target_atr_mult", 2.0)

        # Signal confirmation: the ADX/EMA-spread condition must hold for this
        # many distinct 5-min bars before firing — same debouncing principle as
        # EMACrossoverStrategy's signal_confirm_bars, needed because ADX/EMA
        # spread can transiently spike without the trend actually continuing.
        # Only used when use_pullback_continuation_model is False (legacy
        # path) -- see that parameter below.
        self.signal_confirm_bars: int = self.parameters.get("signal_confirm_bars", 2)

        # Fixed 2026-08-21 (external review, round 2 -- sections 8/9/13): a
        # real pullback-then-breakout EVENT-based confirmation model, the
        # review's own preferred design over the flat N-bar debounce above
        # ("confirm an event, not a state"). Once the ADX/EMA-spread/rising/
        # slope/extension quality gate above qualifies a symbol+direction,
        # this tracks TREND ESTABLISHED -> PULLBACK -> BREAKOUT rather than
        # just requiring the same state to repeat for N bars: it waits for
        # price to pull back off its local high/low (a real consolidation,
        # not just "still qualifies"), then fires only once price actually
        # breaks back through that pullback's reference level -- an event,
        # not a static condition. See _pullback_continuation_signal().
        # True is the new default; set False to fall back to the exact
        # pre-2026-08-21 signal_confirm_bars debounce (rollback path).
        self.use_pullback_continuation_model = self.parameters.get("use_pullback_continuation_model", True)
        # How many distinct bars a pullback may persist without breaking out
        # before the setup is abandoned (avoid waiting indefinitely for a
        # breakout that may never come).
        self.max_pullback_bars = self.parameters.get("max_pullback_bars", 6)
        # Two-tier RVOL breakout confirmation (section 10): a genuine volume
        # CONTRACTION during the pullback (RVOL below pullback_rvol_low at
        # any point while pulling back) followed by EXPANSION on the
        # breakout bar (RVOL >= breakout_rvol_min, deliberately lower than
        # the flat rvol_entry_threshold) is treated as a stronger signal
        # than a flat RVOL floor alone -- "the crowd stepped away, then
        # rushed back in on the resumption," vs. rvol_entry_threshold's
        # "volume was already elevated the whole time" (which can mean the
        # move is late, not fresh -- the same critique the review made of
        # the original flat ADX threshold). If no contraction was ever
        # observed during the pullback, the breakout still needs to clear
        # the higher flat rvol_entry_threshold.
        self.pullback_rvol_low  = self.parameters.get("pullback_rvol_low", 0.8)
        self.breakout_rvol_min  = self.parameters.get("breakout_rvol_min", 1.3)
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

        # Fixed 2026-08-21 (external review, round 2): state for the
        # pullback+breakout event model, keyed by symbol for the same
        # reason as the dicts above (one strategy instance, many symbols).
        self._trend_state:      Dict[str, str] = {}    # "ESTABLISHED" | "PULLBACK"
        self._trend_direction:  Dict[str, str] = {}    # "BUY" | "SELL"
        self._pullback_ref:     Dict[str, float] = {}  # reference swing level to break
        self._pullback_bars:    Dict[str, int] = {}    # bars spent in PULLBACK so far
        self._pullback_bar_key: Dict[str, str] = {}
        self._rvol_history:     Dict[str, list] = {}   # bar-aligned, feeds the two-tier RVOL check

        # Read by live_trading_engine.py's RVOL entry gate: when the pullback
        # model is active, _pullback_continuation_signal() already makes a
        # richer, history-aware RVOL decision (two-tier pattern) internally
        # as part of firing the breakout signal -- the engine's own flat
        # rvol_entry_threshold re-check must NOT also run afterwards, or a
        # breakout that passed the two-tier check at RVOL>=breakout_rvol_min
        # (which can be below rvol_entry_threshold) would get silently
        # rejected downstream while this strategy's own pullback state has
        # already been consumed (fired), losing the setup with no retry.
        self.rvol_checked_internally = self.use_pullback_continuation_model

        # Fixed 2026-08-21 (external review of ema_crossover_v1, applied
        # here too): generate_signal() already gates on its own
        # adx_entry_threshold (paired with adx_rising_required) before ever
        # returning BUY/SELL -- the engine's separate flat ADX>=25 check
        # was already fully redundant for this strategy, just not
        # previously marked so. True regardless of
        # use_pullback_continuation_model, since both the pullback model
        # and the legacy debounce path route through the same ADX gate in
        # generate_signal().
        self.adx_checked_internally = True

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
            f"UnderlyingInvalidation={self.underlying_invalidation_exit} "
            f"UnderlyingStop={self.underlying_stop_atr_mult}xATR "
            f"UnderlyingTarget={self.underlying_target_atr_mult}xATR | "
            f"PullbackModel={self.use_pullback_continuation_model} "
            f"(max_bars={self.max_pullback_bars}, breakout_RVOL>={self.breakout_rvol_min} "
            f"after contraction<{self.pullback_rvol_low}) | "
            f"ConfirmBars={self.signal_confirm_bars} (legacy path only)"
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

            # Fixed 2026-08-27 (live incident): extension_atr_mult/
            # vwap_extension_pct answer "is this a FRESH, not-already-
            # blown-out entry point" -- a question about distance from a
            # reference level, not about whether the trend itself is still
            # real (that's what adx_rising_required/ema_slope_required
            # just checked, above). Confirmed live: on a genuinely strong
            # trending day (2026-08-27, TRENDING regime) these two filters
            # rejected 100% of candidates for hours even AFTER the same-day
            # extension_atr_mult widening (1.5->2.5x) -- real observed
            # values ran 2.66x-5.11x, because EMA20 lags price and a
            # sustained trend mechanically keeps extending further from it
            # the longer it runs. Recomputing this gate every cycle and
            # feeding straight into `raw` (as ADX/slope still do above) was
            # wiping an already-tracked pullback+breakout setup the moment
            # a maturing trend crossed the extension line, even though the
            # pullback model's own reference-level mechanism (_pullback_ref
            # below) already independently protects against chasing an
            # exhausted move. These two checks now only gate the FRESH-
            # qualification moment (starting to track a new setup) inside
            # _pullback_continuation_signal(), not every re-evaluation of
            # an already-tracked one -- see entry_extension_ok below. The
            # legacy (non-pullback) confirmation path has no comparable
            # multi-bar tracked state to protect, so it keeps the original
            # every-cycle behavior unchanged.
            extension_ok = True
            if raw is not None and self.extension_atr_mult > 0:
                close = data.get("close")
                atr = data.get("atr14")
                if close and atr and atr > 0 and abs(close - fast_ema) / atr > self.extension_atr_mult:
                    logger.info(
                        f"[{self.name}] {symbol} {raw} candidate too extended — "
                        f"price {abs(close - fast_ema) / atr:.2f}x ATR from EMA20 "
                        f"(> {self.extension_atr_mult}x)"
                    )
                    extension_ok = False

            vwap_ok = True
            if raw is not None and self.vwap_extension_pct > 0:
                close = data.get("close")
                vwap = data.get("vwap") or data.get("session_vwap")
                if close and vwap and vwap > 0:
                    _vwap_dist_pct = abs(close - vwap) / vwap * 100
                    if _vwap_dist_pct > self.vwap_extension_pct:
                        logger.info(
                            f"[{self.name}] {symbol} {raw} candidate too extended — "
                            f"{_vwap_dist_pct:.2f}% from VWAP (> {self.vwap_extension_pct}%)"
                        )
                        vwap_ok = False
        else:
            extension_ok = True
            vwap_ok = True

        if self.use_pullback_continuation_model:
            return self._pullback_continuation_signal(
                symbol, raw, data, bar_key, entry_extension_ok=(extension_ok and vwap_ok),
            )
        legacy_raw = raw if (extension_ok and vwap_ok) else None
        return self._legacy_confirm_bars_signal(symbol, legacy_raw, adx, ema_spread_pct, bar_key)

    def _legacy_confirm_bars_signal(
        self, symbol: str, raw: Optional[str], adx: float, ema_spread_pct: float, bar_key: Optional[str],
    ) -> str:
        """
        Pre-2026-08-21 signal model: fires once the quality-gated `raw`
        direction has held for signal_confirm_bars distinct 5-min bars.
        Kept as a rollback path via use_pullback_continuation_model=False --
        see _pullback_continuation_signal() for the current default.
        """
        signal = "HOLD"
        if raw is not None:
            if raw != self._pending_signal.get(symbol):
                # New direction (or first candidate) — start fresh. Seeds
                # UNCONDITIONALLY (even if bar_key is currently unknown) so
                # signal_confirm_bars=1 still fires immediately on the very
                # cycle a candidate first appears -- needed both for live
                # trading with signal_confirm_bars=1 and for any
                # bar_key-agnostic caller (e.g. the backtest engine, which
                # never sets ohlc_bar_key at all since each row already
                # deterministically represents one distinct bar).
                self._pending_signal[symbol] = raw
                self._pending_count[symbol] = 1
                self._pending_bar_key[symbol] = bar_key
            elif (
                bar_key is not None
                and self._pending_bar_key.get(symbol) is not None
                and bar_key != self._pending_bar_key.get(symbol)
            ):
                # Same direction, and BOTH the stored reference and the
                # current bar_key are known real values that differ --
                # genuinely a new, distinct bar. Safe to advance.
                self._pending_count[symbol] = self._pending_count.get(symbol, 0) + 1
                self._pending_bar_key[symbol] = bar_key
            elif bar_key is not None and self._pending_bar_key.get(symbol) is None:
                # Fixed 2026-08-21 (external review): the reference bar was
                # seeded while bar_key was still unknown -- this is the
                # first cycle we can actually identify which bar we're on.
                # Record it WITHOUT advancing the count: we can't tell
                # whether this real bar_key is the SAME anonymous candle
                # the count was seeded on, or a genuinely new one, so this
                # is "now we know bar 1's identity," not "here's bar 2."
                # Previously this branch didn't exist -- the elif above it
                # alone (bar_key != stored, with stored possibly None) let
                # None-to-real-value transitions silently count as
                # advancing, fast-tracking signal_confirm_bars by one cycle
                # right when a fresh candidate emerges. Only a LATER,
                # different, also-known bar_key can advance the count now.
                self._pending_bar_key[symbol] = bar_key
            # else: same bar as last cycle, or bar still unknown -- don't double-count

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

    def _reset_pullback_state(self, symbol: str) -> None:
        self._trend_state.pop(symbol, None)
        self._trend_direction.pop(symbol, None)
        self._pullback_ref.pop(symbol, None)
        self._pullback_bars.pop(symbol, None)

    def _pullback_continuation_signal(
        self, symbol: str, raw: Optional[str], data: Dict[str, Any], bar_key: Optional[str],
        entry_extension_ok: bool = True,
    ) -> str:
        """
        Event-based confirmation (external review, round 2, sections 8/9/13):
        TREND ESTABLISHED -> PULLBACK -> BREAKOUT, firing only on the
        breakout EVENT rather than "the quality gate has stayed true for N
        bars." `raw` is this bar's core trend-quality candidate direction
        (ADX rising, EMA sloping -- see generate_signal()) or None if that
        gate doesn't currently hold. `entry_extension_ok` is separate: it
        answers "is price still close enough to EMA20/VWAP to treat a FIRST
        qualification as a fresh entry point" -- see the fix note below.

        State machine per symbol (only advances on a genuinely new,
        identifiable bar_key -- same debounce convention as the ADX/EMA
        history tracking above, to avoid the bar_key=None class of bug):
          None/absent -> ESTABLISHED: first bar the quality gate qualifies
              AND entry_extension_ok is True (see fix note). _pullback_ref
              is seeded at this bar's close and kept rising (BUY) / falling
              (SELL) each subsequent bar the trend keeps extending without
              pulling back.
          ESTABLISHED -> PULLBACK: the first bar that qualifies but does NOT
              extend past the current reference (a genuine dip/consolidation
              off the local high/low) locks in _pullback_ref as the level
              that must be broken to confirm resumption.
          PULLBACK -> fires: a later bar's close breaks back through
              _pullback_ref in the trend's direction, with RVOL confirmation
              (see the two-tier check below) -- this is the actual "event."
          PULLBACK -> reset: quality gate drops out, direction flips, or the
              pullback persists longer than max_pullback_bars without a
              qualifying breakout.
        Fully resets (both directions) whenever `raw` is None, matching the
        legacy model's behavior of clearing state the moment the trend
        condition itself is no longer met.

        Fixed 2026-08-27 (live incident): entry_extension_ok used to be
        folded into `raw` itself (generate_signal() set raw=None whenever
        price was too extended from EMA20/VWAP), so it was re-checked and
        could wipe an ALREADY-TRACKED setup every single cycle -- confirmed
        live on a genuinely strong trending day: extension ran 2.66x-5.11x
        ATR for hours (well past the 2.5x floor), continuously resetting
        ESTABLISHED/PULLBACK state the moment a maturing trend crossed the
        line, even though the pullback+breakout mechanism itself (tracking
        _pullback_ref) already independently guards against chasing an
        exhausted move -- that's the model's whole point. Now
        entry_extension_ok only gates the FRESH-qualification moment
        (starting to track a new setup); once already tracking, further
        extension no longer resets progress on its own.
        """
        close = data.get("close")
        rvol  = data.get("rvol")
        # Fixed 2026-08-21 (deep review): ltp_poller emits rvol=0.0 (with
        # rvol_valid=False) as an "insufficient volume history" sentinel for
        # roughly the first 100 minutes of every session -- treated as a
        # real low RVOL reading here, that sentinel counted as a genuine
        # volume contraction and could satisfy had_contraction below. Fail
        # closed the same way the rest of the codebase treats rvol_valid:
        # an invalid bar's RVOL is unknown, not a real reading.
        rvol_valid = data.get("rvol_valid")

        is_new_bar = bar_key is not None and bar_key != self._pullback_bar_key.get(symbol)
        if is_new_bar:
            self._pullback_bar_key[symbol] = bar_key
            if rvol is not None and rvol_valid:
                self._rvol_history.setdefault(symbol, []).append(rvol)
                self._rvol_history[symbol] = self._rvol_history[symbol][-self.max_pullback_bars:]

        if raw is None or close is None:
            if self._trend_state.get(symbol):
                logger.info(f"[{self.name}] {symbol} pullback setup cleared — quality gate no longer met")
            self._reset_pullback_state(symbol)
            self._rvol_history.pop(symbol, None)
            return "HOLD"

        state     = self._trend_state.get(symbol)
        direction = self._trend_direction.get(symbol)

        # Fixed 2026-08-21 (deep review): a direction flip (state is not
        # None and direction != raw) must also wait for is_new_bar, same as
        # every other transition in this function -- otherwise calling this
        # twice with the SAME bar_key but a flipped raw direction wiped
        # accumulated pullback progress and reseeded a new state mid-bar.
        # A first-ever qualification (state is None) still seeds regardless
        # of is_new_bar, since there's no existing progress to protect --
        # bar_key-known-ness is handled below via the `bar_key is None`
        # check instead.
        if state is None or (direction != raw and is_new_bar):
            # Fresh qualification, or a direction flip mid-setup — start
            # over. Fixed 2026-08-21 (external review): only seed the
            # ESTABLISHED baseline once bar_key is actually known -- same
            # principle as the legacy debounce fix (see
            # _legacy_confirm_bars_signal). Crediting a later-identified
            # bar_key as "the next bar" when it might be the SAME
            # anonymous candle this cycle already saw would let the
            # pullback/breakout timeline fast-track by one cycle right at
            # the moment a fresh candidate emerges. Always clears any stale
            # state for a different direction regardless, so nothing
            # mismatched lingers either way.
            self._reset_pullback_state(symbol)
            if bar_key is None:
                return "HOLD"
            # Fixed 2026-08-27 (live incident): entry_extension_ok is
            # checked ONLY here, at the moment a NEW setup would start being
            # tracked -- not on later cycles once a setup is already
            # ESTABLISHED/PULLBACK (see the class docstring fix note
            # above). Price already too far from EMA20/VWAP right now just
            # means this isn't a fresh entry point yet; don't start
            # tracking it, but don't otherwise treat it as invalidated.
            if not entry_extension_ok:
                return "HOLD"
            self._trend_state[symbol] = "ESTABLISHED"
            self._trend_direction[symbol] = raw
            self._pullback_ref[symbol] = close
            self._pullback_bars[symbol] = 0
            self._rvol_history[symbol] = [rvol] if rvol is not None and rvol_valid else []
            # Fixed 2026-08-27 (live incident, monitoring gap): promoted
            # from no log at all to INFO -- with LOG_LEVEL=INFO in
            # production, there was no way to observe whether the strategy
            # was ever actually starting to track a setup, only the (much
            # noisier) per-cycle rejection lines. This is a rare,
            # meaningful event, not a firehose.
            logger.info(f"[{self.name}] {symbol} {raw} trend established, tracking for a pullback (ref={close:.2f})")
            return "HOLD"

        if not is_new_bar:
            return "HOLD"  # nothing new to evaluate this cycle

        ref = self._pullback_ref.get(symbol, close)

        if state == "ESTABLISHED":
            extending = (close > ref) if raw == "BUY" else (close < ref)
            if extending:
                self._pullback_ref[symbol] = close
                return "HOLD"
            self._trend_state[symbol] = "PULLBACK"
            self._pullback_bars[symbol] = 1
            logger.info(f"[{self.name}] {symbol} {raw} pullback started, ref={ref:.2f}")
            return "HOLD"

        # state == "PULLBACK"
        broke_out = (close > ref) if raw == "BUY" else (close < ref)
        if not broke_out:
            self._pullback_bars[symbol] = self._pullback_bars.get(symbol, 0) + 1
            # Fixed 2026-08-21 (deep review): _pullback_bars is seeded at 1
            # on entering PULLBACK, so `> max_pullback_bars` let a setup
            # survive max_pullback_bars + 1 bars instead of the documented
            # max_pullback_bars. `>=` expires it after exactly the
            # documented count.
            if self._pullback_bars[symbol] >= self.max_pullback_bars:
                logger.info(
                    f"[{self.name}] {symbol} pullback setup expired after "
                    f"{self.max_pullback_bars} bars without a breakout"
                )
                self._reset_pullback_state(symbol)
            return "HOLD"

        # Breakout bar — two-tier RVOL confirmation (section 10).
        # Fixed 2026-08-21 (deep review): rvol_valid gates both the history
        # (appended above) and this bar's own rvol -- ltp_poller's rvol=0.0
        # "insufficient history" sentinel must be treated as unknown, not a
        # real low reading, same fail-closed convention used for rvol_valid
        # elsewhere (e.g. live_trading_engine.py's RVOL entry gate).
        hist_rvol = self._rvol_history.get(symbol, [])
        had_contraction = any(v is not None and v < self.pullback_rvol_low for v in hist_rvol)
        rvol_ok = False
        if rvol is not None and rvol_valid:
            if had_contraction and rvol >= self.breakout_rvol_min:
                rvol_ok = True
            elif rvol >= self.rvol_entry_threshold:
                rvol_ok = True

        if not rvol_ok:
            logger.info(
                f"[{self.name}] {symbol} {raw} breakout rejected — RVOL="
                f"{rvol if rvol is not None else 'n/a'} insufficient (needs "
                f">={self.breakout_rvol_min} after a contraction <{self.pullback_rvol_low}, "
                f"or >={self.rvol_entry_threshold} flat)"
            )
            # Give the setup another bar or two rather than discarding it
            # outright on one weak-volume breakout attempt.
            self._pullback_bars[symbol] = self._pullback_bars.get(symbol, 0) + 1
            if self._pullback_bars[symbol] >= self.max_pullback_bars:
                self._reset_pullback_state(symbol)
            return "HOLD"

        logger.info(
            f"[{self.name}] {symbol} {raw} pullback+breakout confirmed — "
            f"close={close:.2f} broke ref={ref:.2f} after "
            f"{self._pullback_bars.get(symbol, 0)} pullback bar(s), "
            f"RVOL={rvol:.2f}{' (post-contraction)' if had_contraction else ''} — firing."
        )
        self._reset_pullback_state(symbol)
        self._rvol_history.pop(symbol, None)
        return raw

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
          - entry_underlying_price, entry_atr : optional, feed the
                              underlying-based stop/target added 2026-08-21
                              (round 2); skipped (not fail-closed) if either
                              is missing.

        Exit conditions (in priority order):
          1. Underlying-based stop/target (added 2026-08-21, round 2) — the
                                  PRIMARY exit driver per the external
                                  review: underlying's close has moved
                                  underlying_stop_atr_mult/
                                  underlying_target_atr_mult ATRs against/in
                                  favor of entry, in the underlying's own
                                  terms rather than option premium (which
                                  carries IV/theta/gamma noise on top of the
                                  underlying's real move). Checked first,
                                  ahead of every premium-based check below,
                                  which remain as a backstop.
          2. Hard stop loss     — premium fell >= stop_loss_pct from entry
          3. Weakening-trend stop — premium fell >= weakening_stop_loss_pct
                                  from entry AND ADX has already dropped below
                                  adx_entry_threshold (the trend no longer
                                  reconfirms this position's own entry
                                  condition). Tighter than the hard stop,
                                  since a trend that's stopped reconfirming
                                  itself shouldn't get the full drawdown
                                  before being cut loose — see initialize().
          4. Structural invalidation (added 2026-08-20) — underlying's close
                                  crossed back to the wrong side of EMA20,
                                  breaking the trend-participation thesis the
                                  position was entered on, regardless of
                                  premium P&L. Toggle via
                                  underlying_invalidation_exit.
          5. Profit target      — premium rose >= target_pct from entry
          6. Trailing stop      — premium fell >= trailing_stop_pct from its peak
          7. Breakeven stop     — once up >= breakeven_activation_pct from entry,
                                  never allow a close below entry (see below)
          8. Trend exhaustion   — ADX has dropped below adx_exit_threshold, i.e.
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

        # Fixed 2026-08-21 (external review, round 2 -- sections 20-21):
        # underlying-based stop/target, the PRIMARY exit driver per the
        # review -- checked first, ahead of every premium-based check below.
        # Skips gracefully (not fail-closed) if entry_underlying_price/
        # entry_atr weren't captured at entry (e.g. a position restored from
        # before this field existed).
        entry_underlying = current_position.get("entry_underlying_price")
        entry_atr        = current_position.get("entry_atr")
        current_close     = current_position.get("current_close")
        is_call           = current_position.get("is_call")
        if (
            (self.underlying_stop_atr_mult > 0 or self.underlying_target_atr_mult > 0)
            and entry_underlying and entry_atr and current_close
            and entry_underlying > 0 and entry_atr > 0 and is_call is not None
        ):
            if is_call:
                stop_level   = entry_underlying - self.underlying_stop_atr_mult * entry_atr
                target_level = entry_underlying + self.underlying_target_atr_mult * entry_atr
                hit_stop     = self.underlying_stop_atr_mult > 0 and current_close <= stop_level
                hit_target   = self.underlying_target_atr_mult > 0 and current_close >= target_level
            else:
                stop_level   = entry_underlying + self.underlying_stop_atr_mult * entry_atr
                target_level = entry_underlying - self.underlying_target_atr_mult * entry_atr
                hit_stop     = self.underlying_stop_atr_mult > 0 and current_close >= stop_level
                hit_target   = self.underlying_target_atr_mult > 0 and current_close <= target_level
            if hit_stop:
                logger.info(
                    f"[{self.name}] Underlying-based stop: close={current_close:.2f} "
                    f"past {stop_level:.2f} (entry {entry_underlying:.2f} "
                    f"{'-' if is_call else '+'} {self.underlying_stop_atr_mult}x "
                    f"ATR({entry_atr:.2f})) -- exiting."
                )
                return "EXIT"
            if hit_target:
                logger.info(
                    f"[{self.name}] Underlying-based target: close={current_close:.2f} "
                    f"past {target_level:.2f} (entry {entry_underlying:.2f} "
                    f"{'+' if is_call else '-'} {self.underlying_target_atr_mult}x "
                    f"ATR({entry_atr:.2f})) -- exiting."
                )
                return "EXIT"

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
        # Fixed 2026-08-21 (external review, round 2): same staleness risk
        # for the pullback+breakout state machine -- a pullback/reference
        # level observed before the pause shouldn't be trusted against bars
        # that occurred while paused.
        self._trend_state.clear()
        self._trend_direction.clear()
        self._pullback_ref.clear()
        self._pullback_bars.clear()
        self._pullback_bar_key.clear()
        self._rvol_history.clear()

    def shutdown(self):
        pass
