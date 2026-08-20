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

        logger.info(
            f"Initialized EMA Crossover '{self.name}' ({self.fast_period}/{self.slow_period}) | "
            f"SL={self.stop_loss_pct:.0%} TP={self.target_pct:.0%} Trail={self.trailing_stop_pct:.0%} "
            f"Breakeven activates at +{self.breakeven_activation_pct:.0%} | "
            f"ConfirmBars={self.signal_confirm_bars}"
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

        if fast_ema is None or slow_ema is None:
            logger.warning(f"Strategy {self.name}: Missing EMA data.")
            return "HOLD"

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
            if bar_key is not None and bar_key != self._pending_bar_key.get(symbol):
                self._pending_count[symbol] = self._pending_count.get(symbol, 0) + 1
                self._pending_bar_key[symbol] = bar_key
            # else: same bar as last cycle (or bar unknown) — don't double-count
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
            logger.info(
                f"[{self.name}] {symbol} {current_dir} confirmed after "
                f"{self._pending_count[symbol]} bars — firing."
            )
            signal = current_dir
            self._pending_signal.pop(symbol, None)
            self._pending_count.pop(symbol, None)
            self._pending_bar_key.pop(symbol, None)
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
                              (optional — only the VOLATILE reversal exit uses them)
          - is_call        : True for a CE position, False for PE (optional, same)
          - entry_regime   : regime string at entry time (optional, same)

        Exit conditions (in priority order):
          1. Hard stop loss  — premium fell >= stop_loss_pct (default 50%) from entry
          2. Profit target   — premium rose >= target_pct (default 100%, i.e. 2×) from entry
          3. Trailing stop   — premium fell >= trailing_stop_pct (default 25%) from its peak
          4. Breakeven stop  — once up >= breakeven_activation_pct (default 15%) from
                                entry, never allow a close below entry (see below)
          5. EMA reversal    — VOLATILE-entered positions only (see below)
        """
        entry_premium = float(current_position.get("avg_price") or 0)
        if entry_premium <= 0 or current_premium <= 0:
            return "HOLD"

        pnl_pct = (current_premium - entry_premium) / entry_premium

        # 1. Hard stop loss
        if pnl_pct <= -self.stop_loss_pct:
            logger.info(
                f"[{self.name}] Stop loss: entry=Rs{entry_premium:.2f} "
                f"current=Rs{current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        # 2. Profit target
        if pnl_pct >= self.target_pct:
            logger.info(
                f"[{self.name}] Target hit: entry=Rs{entry_premium:.2f} "
                f"current=Rs{current_premium:.2f} ({pnl_pct:.1%})"
            )
            return "EXIT"

        # 3. Trailing stop — only activates once we've been in profit
        peak = float(current_position.get("peak_premium") or entry_premium)
        if peak > entry_premium:
            trail_drawdown = (peak - current_premium) / peak
            if trail_drawdown >= self.trailing_stop_pct:
                logger.info(
                    f"[{self.name}] Trailing stop: peak=Rs{peak:.2f} "
                    f"current=Rs{current_premium:.2f} (drawdown {trail_drawdown:.1%})"
                )
                return "EXIT"

        # 4. Breakeven stop (added 2026-08-13) -- the trailing stop above is
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

        # 5. EMA reversal — VOLATILE-entered positions only (added 2026-07-31).
        # These are crash-catching PE entries (see REGIME_STRATEGY_MAP/
        # live_trading_engine.py._process_signal's VOLATILE gate) opened
        # specifically because EMA20 crossed below EMA50 during a VIX spike.
        # A genuine panic can V-reverse fast; waiting for the normal SL/TP/
        # trailing-stop thresholds (tuned for ordinary trending days) risks
        # riding the reversal most of the way back. If the same EMA
        # relationship that justified entry has already flipped, exit
        # immediately regardless of premium P&L — smaller profit is fine,
        # getting caught in the reversal is not. Not applied to positions
        # entered outside VOLATILE — this is intentionally scoped, not a
        # general new exit rule for every EMA crossover trade.
        if current_position.get("entry_regime") == "VOLATILE":
            ema_fast = current_position.get("current_ema_fast")
            ema_slow = current_position.get("current_ema_slow")
            is_call  = current_position.get("is_call")
            if ema_fast is not None and ema_slow is not None and is_call is not None:
                reversed_ = (ema_fast <= ema_slow) if is_call else (ema_fast >= ema_slow)
                if reversed_:
                    logger.info(
                        f"[{self.name}] VOLATILE reversal exit: EMA20/50 relationship "
                        f"flipped back (fast={ema_fast:.2f} slow={ema_slow:.2f}, "
                        f"is_call={is_call}) — exiting regardless of premium P&L "
                        f"({pnl_pct:+.1%})."
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

    def shutdown(self):
        logger.info(f"Shutting down EMA Crossover Strategy '{self.name}'")
