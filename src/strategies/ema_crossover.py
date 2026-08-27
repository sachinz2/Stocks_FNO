import logging
from typing import Dict, Any, Optional
from src.strategies.base import StrategyBase, StrategyRegistry

logger = logging.getLogger(__name__)

@StrategyRegistry.register("EMA_CROSSOVER")
class EMACrossoverStrategy(StrategyBase):
    """
    EMA 20/50 Crossover Strategy.
    Rules:
    - If EMA20 crosses above EMA50 -> BUY
    - If EMA20 crosses below EMA50 -> SELL
    Requires maintaining the previous state of EMAs to detect the actual "cross".

    Redesign (2026-08-21, external PDF review of ema_crossover_v1): the review's
    core thesis is the mirror image of the one that drove momentum_v1's redesign
    the day before -- momentum_v1 was entering too LATE (confirming an already-
    played-out move), while ema_crossover_v1 was filtered so aggressively (ADX>=25
    + RVOL>=1.3 hard gates + strict 15m agreement, stacked on top of the 2-bar
    confirmation) that its opportunity set was almost eliminated -- confirmed live:
    1 trade in ~2 months. "EMA crossover" had effectively become "EMA crossover +
    volume breakout + already-strong trend + higher-timeframe agreement," a much
    narrower strategy than the one actually being evaluated. Per the user's
    established preference (integrate into v1 directly, no separate v2 -- same
    call made for momentum_v1's redesign), addressed here:
    - ADX moved from a flat, engine-level hard gate (>=25) to this strategy's own
      internal, looser gate (>=adx_entry_threshold OR ADX rising -- see
      generate_signal()) -- an early crossover with a still-low-but-rising ADX
      is exactly the setup the review argues the old flat threshold discarded.
    - RVOL demoted from a hard gate to a non-blocking confidence note
      (rvol_hard_gate=False) -- a crossover doesn't require above-average volume
      to be real, per the review's own example (a valid EMA20/50 cross at
      RVOL=0.95 was being rejected outright).
    - The 15-min MTF filter made asymmetric/graduated (mtf_strict=False) --
      only a STRONGLY opposing higher timeframe still blocks the entry; a
      weakly opposing or turning 15m trend no longer does, since that's
      precisely the higher-timeframe-weakening-into-a-reversal setup a
      crossover strategy should be able to catch.
    - The EMA-reversal exit, previously scoped only to VOLATILE-entered
      positions, is now the PRIMARY exit for every position (see
      manage_position()) -- "the thesis that justified entry has reversed"
      is not a VOLATILE-specific concept.
    - A new underlying-based stop/target (underlying_stop_atr_mult /
      underlying_target_atr_mult), mirroring momentum_v1's round-2 addition,
      using the same entry_underlying_price/entry_atr already captured by the
      engine for every single-leg entry.
    NOT done (needs real backtesting infrastructure this system doesn't have,
    and the review itself frames these as things to TEST, not firm
    recommendations): a full weighted composite entry score (the review's own
    "something like this would be much better" diagram is explicitly
    illustrative, not tuned); testing alternate EMA pairs (10/30, 13/21, 9/21)
    or alternate stop_loss_pct/target_pct values; comparing 10-25 vs 20-35 DTE.
    One review claim was already outdated by the time this was read: the
    15-minute MTF filter's fail-OPEN-on-exception bug (review section 22) was
    fixed the day before in the external review (2026-08-20) that redesigned
    momentum_v1 -- that filter already fails closed for both strategies.
    """
    def initialize(self):
        self.fast_period = self.parameters.get("fast_period", 20)
        self.slow_period = self.parameters.get("slow_period", 50)
        self.stop_loss_pct = self.parameters.get("stop_loss_pct", 0.50)
        self.target_pct = self.parameters.get("target_pct", 1.0)
        self.trailing_stop_pct = self.parameters.get("trailing_stop_pct", 0.25)
        # Once a position has been up at least this much from entry, it must
        # never be allowed to close as a realized loss -- see manage_position()'s
        # breakeven-stop check for why trailing_stop_pct alone doesn't guarantee this.
        self.breakeven_activation_pct = self.parameters.get("breakeven_activation_pct", 0.15)

        # Fixed 2026-08-21 (external review): ADX moved in-strategy and
        # loosened -- previously a flat, engine-level `ADX < 25` hard gate
        # applied AFTER generate_signal() already fired, unconditionally,
        # to every BUY/SELL this strategy produced. Per the review: "a
        # crossover is often the beginning of a trend; ADX measures an
        # already-developed one" -- 25 discarded early, still-developing
        # setups. Now: passes at ADX >= adx_entry_threshold (18, the low
        # end of the review's own "test 18/20/22" range) OR ADX rising
        # (using the same bar-aligned history pattern as momentum.py's
        # adx_rising_required), matching the review's own "ADX>=18 OR ADX
        # rising" diagram box. adx_checked_internally=True tells the engine
        # to skip its own now-redundant flat gate for this strategy.
        self.adx_entry_threshold = self.parameters.get("adx_entry_threshold", 22)
        self.adx_checked_internally = True
        self._HISTORY_LEN = 3

        # Fixed 2026-08-21 (external review): RVOL demoted from a hard
        # engine-level gate to a non-blocking confidence note -- a genuine
        # EMA20/50 cross doesn't require above-average volume to be real
        # (the review's own example: a valid cross at RVOL=0.95 was
        # rejected outright). rvol_entry_threshold is still read for the
        # confidence note itself; rvol_hard_gate=False tells the engine not
        # to block on it.
        self.rvol_hard_gate = self.parameters.get("rvol_hard_gate", False)

        # Fixed 2026-08-21 (external review): the 15-min MTF filter becomes
        # asymmetric/graduated instead of a binary reject-on-disagreement --
        # only a STRONGLY opposing 15m trend (spread magnitude >=
        # mtf_strong_opposition_pct) still blocks the entry; a weakly
        # opposing or turning 15m trend is allowed, since that's precisely
        # the "higher timeframe weakening into a reversal" setup a
        # crossover strategy should be able to catch (review section 10).
        self.mtf_strict = self.parameters.get("mtf_strict", False)
        self.mtf_strong_opposition_pct = self.parameters.get("mtf_strong_opposition_pct", 0.3)

        # Fixed 2026-08-21 (external review, section 11): RS (top-10 vs
        # NIFTY) demoted from a hard, engine-level gate to opt-in -- an
        # early crossover shouldn't need the stock to ALREADY be a top-10
        # RS leader, itself a lagging confirmation. False here; momentum_v1
        # doesn't set this and keeps the shared default (True, unchanged).
        self.require_rs = self.parameters.get("require_rs", False)

        # Fixed 2026-08-21 (external review, sections 15-18): the EMA-
        # reversal exit, previously scoped only to VOLATILE-entered
        # positions, is now the PRIMARY exit for every position (see
        # manage_position()) -- toggleable in case it proves too twitchy
        # against normal EMA20/50 noise in practice, same convention as
        # momentum_v1's structural-invalidation exit.
        self.ema_reversal_exit = self.parameters.get("ema_reversal_exit", True)

        # Fixed 2026-08-27 (trade review, Aug 24-26): confirmed live that
        # "too twitchy against normal EMA20/50 noise" wasn't hypothetical --
        # 12 of 27 exits over 3 days were this check firing on an EMA20/50
        # gap of a few hundredths of a point (once literally equal), each
        # one an automatic loss regardless of how the position was
        # otherwise doing. Two independent fixes, mirroring how the ENTRY
        # side already guards against exactly this kind of noise:
        #   - ema_reversal_min_gap_pct: the flipped relationship must be by
        #     at least this fraction of the slow EMA, not any sign flip --
        #     a literal tie or a hundredth-of-a-point cross no longer counts.
        #   - ema_reversal_confirm_bars: the (gap-qualified) reversal must
        #     hold across this many genuinely distinct bars before it
        #     fires, via the same bar_key debounce pattern generate_signal()
        #     already uses for entries -- so a one-tick flicker back across
        #     the line doesn't exit a position that's still fine a bar
        #     later. The entry side already required 2 confirming bars;
        #     the exit side required none, which was the actual asymmetry.
        self.ema_reversal_min_gap_pct  = self.parameters.get("ema_reversal_min_gap_pct", 0.001)
        self.ema_reversal_confirm_bars = self.parameters.get("ema_reversal_confirm_bars", 2)
        self._reversal_pending_count:   Dict[str, int] = {}
        self._reversal_pending_bar_key: Dict[str, str] = {}

        # Fixed 2026-08-21 (external review, sections 16-18): underlying-
        # based stop AND target, mirroring momentum_v1's round-2 addition --
        # uses entry_underlying_price/entry_atr, already captured by the
        # engine for every single-leg entry regardless of strategy. See
        # manage_position(). 0 disables either independently.
        #
        # Fixed 2026-08-27 (trade review): stop raised from 1.0x to 1.4x ATR
        # -- 8 of 27 exits over Aug 24-26 were this stop, and 1.0x ATR is
        # tight enough that normal intraday noise (not a genuine thesis
        # invalidation) was plausibly tripping a meaningful share of those.
        self.underlying_stop_atr_mult   = self.parameters.get("underlying_stop_atr_mult", 1.4)
        self.underlying_target_atr_mult = self.parameters.get("underlying_target_atr_mult", 2.0)

        # Fixed 2026-08-21 (external review, section 14): expose the same
        # delta-based strike selection mechanism built for momentum_v1 --
        # None/0 keeps the existing ATM behavior (this strategy's own
        # default, unchanged) unless explicitly tuned.
        self.entry_option_delta = self.parameters.get("entry_option_delta", None)

        # Signal confirmation: crossover must persist for this many consecutive cycles
        # before a BUY/SELL fires. Prevents rapid BUY↔SELL alternation when EMAs are close.
        self.signal_confirm_bars: int = self.parameters.get("signal_confirm_bars", 2)
        self.min_dte: int = self.parameters.get("min_dte", 10)
        # Fixed 2026-08-20: 25 left a structural monthly dead zone. Single-leg
        # entries resolve their expiry via get_near_month_expiry() (only rolls
        # once DTE<7, unlike credit_spread/iron_condor's get_entry_expiry()),
        # so right after that roll the fresh contract's DTE can be as high as
        # 41 (6 remaining on the old contract + up to a 35-day gap to the next
        # monthly expiry). With max_dte=25, every entry was rejected for the
        # ~1-2 weeks following every monthly roll -- confirmed live: zero
        # ema_crossover_v1/momentum_v1 orders 2026-07-25..08-05 and again
        # 2026-08-17..08-20, both immediately after a monthly roll, same
        # cause both times. 42 comfortably covers the worst-case fresh DTE
        # across the real NSE expiry calendar (verified by walking every
        # month's expiry-to-expiry gap, max observed 41).
        self.max_dte: int = self.parameters.get("max_dte", 42)

        # Keyed by symbol — this single strategy instance evaluates every symbol in
        # its rotating top-5 pool each cycle (see LiveTradingEngine.run_signal_cycle),
        # so flat scalars here would let one symbol's EMA state leak into the next
        # symbol's crossover check within the same cycle.
        self.prev_fast_ema: Dict[str, float] = {}
        self.prev_slow_ema: Dict[str, float] = {}
        self._pending_signal: Dict[str, str] = {}
        self._pending_count: Dict[str, int] = {}
        self._pending_bar_key: Dict[str, str] = {}  # tracks last 5-min bar seen, per symbol
        # Fixed 2026-08-21 (external review): bar-aligned ADX history per
        # symbol, feeding the ADX-rising half of the new internal gate --
        # same pattern as momentum.py's _adx_history/_history_bar_key.
        self._adx_history: Dict[str, list] = {}
        self._history_bar_key: Dict[str, str] = {}

        logger.info(
            f"Initialized EMA Crossover '{self.name}' ({self.fast_period}/{self.slow_period}) | "
            f"ADX entry>={self.adx_entry_threshold} (OR rising) | "
            f"RVOL hard_gate={self.rvol_hard_gate} | require_RS={self.require_rs} | "
            f"MTF strict={self.mtf_strict} strong_opposition>={self.mtf_strong_opposition_pct}% | "
            f"delta_target={self.entry_option_delta or 'ATM'} | "
            f"SL={self.stop_loss_pct:.0%} TP={self.target_pct:.0%} Trail={self.trailing_stop_pct:.0%} "
            f"Breakeven activates at +{self.breakeven_activation_pct:.0%} | "
            f"UnderlyingStop={self.underlying_stop_atr_mult}xATR "
            f"UnderlyingTarget={self.underlying_target_atr_mult}xATR | "
            f"ConfirmBars={self.signal_confirm_bars} | "
            f"EMAReversalExit={self.ema_reversal_exit} "
            f"(min_gap={self.ema_reversal_min_gap_pct:.2%}, confirm_bars={self.ema_reversal_confirm_bars})"
        )

    def generate_signal(self, data: Dict[str, Any]) -> Optional[str]:
        """
        Expects data dict containing:
        - symbol: which underlying this tick belongs to — required to keep this
          strategy's per-symbol crossover state straight across its rotating pool.
        - ema20 (or dynamic fast_period key)
        - ema50 (or dynamic slow_period key)
        - ohlc_bar_key (optional): changes once per 5-min bar; used so that
          signal_confirm_bars counts distinct completed candles, not engine cycles.
        """
        symbol   = data.get("symbol", "")
        fast_ema = data.get(f"ema{self.fast_period}")
        slow_ema = data.get(f"ema{self.slow_period}")
        bar_key  = data.get("ohlc_bar_key")  # None in test/backtest contexts
        adx      = data.get("adx14")

        if fast_ema is None or slow_ema is None:
            logger.warning(f"Strategy {self.name}: Missing EMA data.")
            return "HOLD"

        # Fixed 2026-08-21 (external review): bar-aligned ADX history,
        # advanced only on a genuinely new, identifiable bar -- same
        # debounce as _pending_bar_key below and the identical pattern in
        # momentum.py. Feeds the ADX-rising half of the gate applied below,
        # once a crossover has actually confirmed.
        if adx is not None and bar_key is not None and bar_key != self._history_bar_key.get(symbol):
            self._adx_history.setdefault(symbol, []).append(adx)
            self._adx_history[symbol] = self._adx_history[symbol][-self._HISTORY_LEN:]
            self._history_bar_key[symbol] = bar_key

        signal = "HOLD"
        prev_fast = self.prev_fast_ema.get(symbol)
        prev_slow = self.prev_slow_ema.get(symbol)

        # Fixed 2026-08-07: found live that this strategy had fired ZERO
        # confirmed trades in its ENTIRE history despite genuine EMA20/50
        # crossovers happening ~once/day/symbol (verified via kite.historical_data()
        # replay + a regime-timeline cross-reference: 91 crossovers across just 8
        # sampled symbols occurred while the strategy was active and held for
        # well over the 2-bar confirmation window, yet none confirmed).
        #
        # Root cause: prev_fast_ema/prev_slow_ema were overwritten on EVERY call
        # (see below), so "prev_fast <= prev_slow and fast_ema > slow_ema" (the
        # crossover test) could only ever be True on the single cycle the sign
        # actually flips. The very next cycle, prev_fast already reflected the
        # POST-cross state, so the same test evaluated False, which hit the
        # "no crossover this bar -- clear pending" branch and wiped
        # _pending_count back to 0 before a second confirming bar could ever
        # accumulate. With signal_confirm_bars=2, confirmation was mathematically
        # impossible -- it needed the transition-moment condition true on two
        # separate cycles, but that condition can only be true on one.
        #
        # Fix: separate "is this a FRESH cross" (compare prev vs current
        # relationship, only used to START a pending count) from "is the
        # crossed relationship still holding" (compare current relationship
        # against the pending direction, used to CONTINUE counting on each new
        # bar). momentum_v1 never had this bug -- its condition is state-based
        # (ADX/spread right now), not transition-based, so it doesn't
        # self-erase the moment after the first confirming cycle.
        current_dir = None
        if fast_ema > slow_ema:
            current_dir = "BUY"
        elif fast_ema < slow_ema:
            current_dir = "SELL"

        if current_dir is None:
            # Exactly equal (rare) — nothing to track.
            self._pending_signal.pop(symbol, None)
            self._pending_count.pop(symbol, None)
            self._pending_bar_key.pop(symbol, None)
        elif current_dir == self._pending_signal.get(symbol):
            # Still in the same pending direction as last time we saw it —
            # keep counting distinct confirming bars.
            #
            # Fixed 2026-08-20 (external review): bar_key is None whenever
            # ltp_poller can't identify the current 5-min bar (no live tick
            # data yet, or a malformed OHLC cache missing "date") -- the old
            # `bar_key is None or ...` condition treated EVERY such cycle as
            # a "new bar," so signal_confirm_bars could complete in a couple
            # of 60s engine cycles instead of 2 genuinely distinct candles.
            # A missing bar_key now simply can't advance the count -- same
            # bar or unknown bar, don't double-count either way. Same fix as
            # momentum.py's identical pattern.
            #
            # Fixed 2026-08-21 (external review): a SECOND, subtler gap --
            # if the pending count was originally SEEDED while bar_key was
            # still unknown (see the fresh-crossover branch below, which
            # seeds unconditionally so signal_confirm_bars=1 still fires
            # immediately), the stored reference bar_key is None. The old
            # `bar_key != self._pending_bar_key.get(symbol)` check alone
            # then treated the FIRST real bar_key we ever see as "different
            # from None" and advanced the count -- even though that real
            # bar_key might identify the very SAME anonymous candle the
            # count was seeded on, not a genuinely new one. Split into two
            # cases: only advance when BOTH sides are known real values
            # that differ; when the stored side is still None, just record
            # the now-known bar_key without advancing.
            if (
                bar_key is not None
                and self._pending_bar_key.get(symbol) is not None
                and bar_key != self._pending_bar_key.get(symbol)
            ):
                self._pending_count[symbol] = self._pending_count.get(symbol, 0) + 1
                self._pending_bar_key[symbol] = bar_key
            elif bar_key is not None and self._pending_bar_key.get(symbol) is None:
                self._pending_bar_key[symbol] = bar_key
            # else: same bar as last cycle, or bar still unknown — don't double-count
        else:
            # Direction differs from whatever was pending (or nothing was
            # pending) — only start a fresh pending count if this is a
            # genuine crossover, i.e. the PREVIOUS relationship was the
            # opposite of the current one. Guards against starting a count
            # on the first-ever data point (prev unknown) or on a symbol
            # that's simply always been on one side (never actually crossed).
            prev_dir = None
            if prev_fast is not None and prev_slow is not None:
                if prev_fast > prev_slow:
                    prev_dir = "BUY"
                elif prev_fast < prev_slow:
                    prev_dir = "SELL"
            if prev_dir is not None and prev_dir != current_dir:
                # Genuine fresh crossover. Seeds UNCONDITIONALLY (even if
                # bar_key is currently unknown) so signal_confirm_bars=1
                # still fires immediately on the crossing bar itself --
                # needed both for live trading with signal_confirm_bars=1
                # and for any bar_key-agnostic caller (e.g. the backtest
                # engine, which never sets ohlc_bar_key since each row
                # already deterministically represents one distinct bar).
                # The "same direction" branch above is what actually
                # prevents a stale-None-reference from being miscounted as
                # advancing on a later cycle.
                self._pending_signal[symbol] = current_dir
                self._pending_count[symbol] = 1
                self._pending_bar_key[symbol] = bar_key
            else:
                self._pending_signal.pop(symbol, None)
                self._pending_count.pop(symbol, None)
                self._pending_bar_key.pop(symbol, None)

        # Single confirm-and-fire check regardless of which branch above set
        # up the pending state — needed so signal_confirm_bars=1 fires right
        # on the fresh-cross bar instead of only being checked one branch up.
        if current_dir is not None and self._pending_count.get(symbol, 0) >= self.signal_confirm_bars:
            # Fixed 2026-08-21 (external review): ADX gate applied only
            # AFTER the crossover has already confirmed, matching the
            # review's own "2-BAR CONFIRMATION -> ADX>=18 OR ADX rising"
            # ordering -- passes at adx_entry_threshold OR a rising ADX
            # (an early, still-developing trend is exactly what a flat
            # threshold alone would discard). Missing adx14 doesn't block
            # here -- the engine's separate adx_valid check (still fail-
            # closed, unaffected by adx_checked_internally) is the real
            # data-availability gate downstream of this signal.
            adx_ok = adx is None
            if adx is not None:
                hist_adx = self._adx_history.get(symbol, [])
                adx_ok = adx >= self.adx_entry_threshold or (len(hist_adx) >= 2 and adx > hist_adx[-2])

            if adx_ok:
                logger.info(
                    f"[{self.name}] {symbol} {current_dir} confirmed after "
                    f"{self._pending_count[symbol]} bars (ADX={adx}) — firing."
                )
                signal = current_dir
                self._pending_signal.pop(symbol, None)
                self._pending_count.pop(symbol, None)
                self._pending_bar_key.pop(symbol, None)
            else:
                # Crossover confirmed but ADX hasn't caught up yet -- leave
                # the pending state intact (don't clear it) so a later bar
                # can still fire without needing a brand new crossover to
                # restart the confirmation count.
                logger.debug(
                    f"[{self.name}] {symbol} {current_dir} confirmed but ADX={adx} "
                    f"< {self.adx_entry_threshold} and not rising — holding, not firing yet."
                )
        elif current_dir is not None and symbol in self._pending_signal:
            logger.debug(
                f"[{self.name}] {symbol} {current_dir} crossover pending "
                f"({self._pending_count.get(symbol, 0)}/{self.signal_confirm_bars} bars)"
            )

        self.prev_fast_ema[symbol] = fast_ema
        self.prev_slow_ema[symbol] = slow_ema

        return signal

    def manage_position(self, current_position: Dict[str, Any], current_premium: float) -> Optional[str]:
        """
        Options position management based on option premium movement.

        current_position must contain:
          - avg_price      : entry premium paid
          - peak_premium   : highest premium seen since entry (tracked by engine)
          - current_ema_fast/current_ema_slow : underlying's current EMA20/50
                              (optional — feeds the EMA-reversal exit)
          - is_call        : True for a CE position, False for PE (optional, same)
          - entry_regime   : regime string at entry time (optional, informational only)
          - entry_underlying_price, entry_atr : optional, feed the
                              underlying-based stop/target added 2026-08-21;
                              skipped gracefully if either is missing.

        Exit conditions (in priority order):
          1. Underlying-based stop/target (added 2026-08-21) — mirrors
                                momentum_v1's round-2 addition, and checked
                                first for the same reason momentum_v1's own
                                docstring gives it: "your stop should be
                                based on the underlying's actual move, not
                                option premium." Underlying's close has
                                moved underlying_stop_atr_mult/
                                underlying_target_atr_mult ATRs against/in
                                favor of entry. Fixed 2026-08-21 (deep
                                review): reordered ahead of the EMA reversal
                                check below to match momentum_v1's ordering
                                — both strategies check the underlying-based
                                stop/target as the PRIMARY exit driver before
                                structural/EMA-reversal invalidation;
                                previously this strategy checked EMA
                                reversal first, the opposite order.
          2. EMA reversal (generalized 2026-08-21, external review sections
                                16-18) — the underlying's EMA20/50
                                relationship that justified entry has
                                flipped back. Previously scoped only to
                                VOLATILE-entered positions; the review's
                                point stands generally: "the thesis that
                                justified entry has reversed" isn't a
                                VOLATILE-specific concept. Toggle via
                                ema_reversal_exit.
          3. Hard stop loss  — premium fell >= stop_loss_pct (default 50%) from entry
          4. Profit target   — premium rose >= target_pct (default 100%, i.e. 2×) from entry
          5. Trailing stop   — premium fell >= trailing_stop_pct (default 25%) from its peak
          6. Breakeven stop  — once up >= breakeven_activation_pct (default 15%) from
                                entry, never allow a close below entry (see below)
        """
        entry_premium = float(current_position.get("avg_price") or 0)
        if entry_premium <= 0 or current_premium <= 0:
            return "HOLD"

        pnl_pct = (current_premium - entry_premium) / entry_premium

        # 1. Underlying-based stop/target (added 2026-08-21) -- see
        # docstring and momentum.py's identical pattern for the rationale.
        entry_underlying = current_position.get("entry_underlying_price")
        entry_atr        = current_position.get("entry_atr")
        current_close     = current_position.get("current_close")
        is_call_u         = current_position.get("is_call")
        if (
            (self.underlying_stop_atr_mult > 0 or self.underlying_target_atr_mult > 0)
            and entry_underlying and entry_atr and current_close
            and entry_underlying > 0 and entry_atr > 0 and is_call_u is not None
        ):
            if is_call_u:
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
                    f"{'-' if is_call_u else '+'} {self.underlying_stop_atr_mult}x "
                    f"ATR({entry_atr:.2f})) -- exiting."
                )
                return "EXIT"
            if hit_target:
                logger.info(
                    f"[{self.name}] Underlying-based target: close={current_close:.2f} "
                    f"past {target_level:.2f} (entry {entry_underlying:.2f} "
                    f"{'+' if is_call_u else '-'} {self.underlying_target_atr_mult}x "
                    f"ATR({entry_atr:.2f})) -- exiting."
                )
                return "EXIT"

        # 2. EMA reversal -- see docstring. Generalized 2026-08-21 from a
        # VOLATILE-only check to running for every position; entry_regime is
        # read only for the log line now, not to scope whether the check
        # runs at all.
        #
        # Fixed 2026-08-27 (trade review, Aug 24-26): two independent noise
        # guards, both mirroring how the ENTRY side already treats a
        # crossover -- confirmed live that without them, 12 of 27 exits
        # over 3 days were this check firing on an EMA20/50 gap of a few
        # hundredths of a point (once literally equal), each one an
        # automatic loss regardless of how the position was otherwise
        # doing:
        #   - min gap (ema_reversal_min_gap_pct): a literal tie or a
        #     hundredth-of-a-point cross no longer counts as "reversed."
        #   - bar-count confirmation (ema_reversal_confirm_bars): the gap-
        #     qualified reversal must hold across this many genuinely
        #     distinct bars (same bar_key debounce as generate_signal()'s
        #     entry confirmation) before it actually exits -- the entry
        #     side already required 2 confirming bars; the exit side
        #     required none, which was the real asymmetry.
        if self.ema_reversal_exit:
            ema_fast = current_position.get("current_ema_fast")
            ema_slow = current_position.get("current_ema_slow")
            is_call  = current_position.get("is_call")
            contract = current_position.get("contract")
            bar_key  = current_position.get("ohlc_bar_key")
            if ema_fast is not None and ema_slow is not None and is_call is not None and ema_slow:
                raw_reversed = (ema_fast <= ema_slow) if is_call else (ema_fast >= ema_slow)
                gap_pct = abs(ema_fast - ema_slow) / abs(ema_slow)
                qualifies = raw_reversed and gap_pct >= self.ema_reversal_min_gap_pct

                if contract is None:
                    # No position identifier to track confirmation state
                    # against (e.g. a caller/test that doesn't pass one) --
                    # degrade to gap-filtered-only rather than silently
                    # never firing.
                    if qualifies:
                        logger.info(
                            f"[{self.name}] EMA reversal exit: EMA20/50 relationship "
                            f"flipped back by {gap_pct:.3%} (fast={ema_fast:.2f} slow={ema_slow:.2f}, "
                            f"is_call={is_call}, entry_regime={current_position.get('entry_regime')}) "
                            f"— exiting regardless of premium P&L ({pnl_pct:+.1%})."
                        )
                        return "EXIT"
                elif qualifies:
                    if (
                        bar_key is not None
                        and self._reversal_pending_bar_key.get(contract) is not None
                        and bar_key != self._reversal_pending_bar_key.get(contract)
                    ):
                        self._reversal_pending_count[contract] = self._reversal_pending_count.get(contract, 0) + 1
                        self._reversal_pending_bar_key[contract] = bar_key
                    elif contract not in self._reversal_pending_count:
                        # First qualifying bar -- seed unconditionally (even
                        # if bar_key is unknown) so confirm_bars=1 would
                        # still fire on the qualifying bar itself, same
                        # convention as the entry side's fresh-crossover seed.
                        self._reversal_pending_count[contract] = 1
                        self._reversal_pending_bar_key[contract] = bar_key
                    elif bar_key is not None and self._reversal_pending_bar_key.get(contract) is None:
                        self._reversal_pending_bar_key[contract] = bar_key
                    # else: same bar as last cycle, or bar still unknown -- don't double-count

                    if self._reversal_pending_count.get(contract, 0) >= self.ema_reversal_confirm_bars:
                        logger.info(
                            f"[{self.name}] EMA reversal exit: EMA20/50 relationship flipped "
                            f"back by {gap_pct:.3%}, confirmed over "
                            f"{self._reversal_pending_count[contract]} bar(s) "
                            f"(fast={ema_fast:.2f} slow={ema_slow:.2f}, is_call={is_call}, "
                            f"entry_regime={current_position.get('entry_regime')}) "
                            f"— exiting regardless of premium P&L ({pnl_pct:+.1%})."
                        )
                        self._reversal_pending_count.pop(contract, None)
                        self._reversal_pending_bar_key.pop(contract, None)
                        return "EXIT"
                else:
                    # Gap closed back up, or relationship un-reversed --
                    # clear any partial confirmation so a later, genuinely
                    # fresh reversal starts counting from zero instead of
                    # inheriting stale progress.
                    self._reversal_pending_count.pop(contract, None)
                    self._reversal_pending_bar_key.pop(contract, None)

        # 3. Hard stop loss
        if pnl_pct <= -self.stop_loss_pct:
            logger.info(
                f"[{self.name}] Stop loss: entry=Rs{entry_premium:.2f} "
                f"current=Rs{current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        # 4. Profit target
        if pnl_pct >= self.target_pct:
            logger.info(
                f"[{self.name}] Target hit: entry=Rs{entry_premium:.2f} "
                f"current=Rs{current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        # 5. Trailing stop — only activates once we've been in profit
        peak = float(current_position.get("peak_premium") or entry_premium)
        if peak > entry_premium:
            trail_drawdown = (peak - current_premium) / peak
            if trail_drawdown >= self.trailing_stop_pct:
                logger.info(
                    f"[{self.name}] Trailing stop: peak=Rs{peak:.2f} "
                    f"current=Rs{current_premium:.2f} (drawdown {trail_drawdown:.1%})"
                )
                return "EXIT"

        # 6. Breakeven stop (added 2026-08-13) -- the trailing stop above is
        # scoped to the PEAK premium, not to locked-in profit. For a peak
        # gain below roughly trailing_stop_pct/(1-trailing_stop_pct) (~33%
        # at the default 25%), trailing_stop_pct off that peak lands BELOW
        # the entry price -- meaning a position that was genuinely
        # profitable at some point could still round-trip all the way into
        # a realized loss before the trailing stop ever fires. Confirmed
        # live 2026-08-11 on the sibling momentum_v1 strategy (same
        # structure): ULTRACEMCO peaked at +31.9% and only exited once it
        # had already fallen below its own entry price, a real loss.
        # Once a position has been up at least breakeven_activation_pct from
        # entry, it must never be allowed to close as a realized loss --
        # exit the moment it falls back to entry, regardless of whether the
        # trailing stop's own (looser) threshold has been reached yet.
        if peak >= entry_premium * (1 + self.breakeven_activation_pct) and current_premium <= entry_premium:
            logger.info(
                f"[{self.name}] Breakeven stop: was up to Rs{peak:.2f} "
                f"(entry Rs{entry_premium:.2f}), now back to Rs{current_premium:.2f} -- "
                "exiting to avoid a profitable trade becoming a loss."
            )
            return "EXIT"

        return "HOLD"

    def on_pause(self) -> None:
        """Clear the confirmation buffer so a stale crossover can't fire on resume.
        Per-symbol prev EMA values are kept — no need to re-warm the baseline."""
        if self._pending_signal:
            logger.info(
                f"[{self.name}] on_pause: clearing pending signals for "
                f"{list(self._pending_signal.keys())}"
            )
        self._pending_signal.clear()
        self._pending_count.clear()
        self._pending_bar_key.clear()
        # Fixed 2026-08-21 (external review): same staleness risk as
        # _pending_* above -- ADX history observed before a pause shouldn't
        # be trusted against bars that occurred while paused.
        self._adx_history.clear()
        self._history_bar_key.clear()

    def shutdown(self):
        logger.info(f"Shutting down EMA Crossover Strategy '{self.name}'")
