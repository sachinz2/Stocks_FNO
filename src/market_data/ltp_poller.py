"""
LTP Poller — computes indicators from Zerodha OHLC, blending baseline history
with live-tick intraday bars.

Runs every 60 s via APScheduler:
  1. Polls all 40 F&O symbols via kite.historical_data() (5-min OHLC, 10-day
     window) — used ONLY as the multi-day baseline for EMA/ATR continuity.
     Today's own bars are always built from live ticks instead (see below),
     never from this call.
  2. Computes EMA20, EMA50, ATR14, VWAP, session_vwap, ADX14, RVOL, prev_close
     for each symbol. RVOL and session_vwap depend on real per-bar volume,
     which requires ZerodhaTicker's MODE_QUOTE subscription (see
     zerodha_ticker.py and update_intraday_bar() in core/utils.py) — before
     2026-07-30 all live bars had volume=0, silently disabling RVOL and
     leaving no real session-scoped VWAP.
  3. Writes enriched tick to Redis (tick:SYMBOL)
  4. Scores all 40 symbols FOUR WAYS — one per strategy regime:
       - EMA Crossover pool  (nfo:top5)          : high ATR% + strong EMA trend, NEAR a cross
       - Credit Spread pool  (nfo:top5:spread)   : low ATR% (<1.2%) + EMA directional
       - Iron Condor pool    (nfo:top5:condor)   : low ATR% (<1.2%) + EMA flat (<0.1%)
       - Momentum pool       (nfo:top5:momentum) : high ADX (>=25) + wide EMA spread —
                                                    the mirror of the EMA crossover pool,
                                                    rewarding an ALREADY established trend
                                                    instead of penalizing it
  5. Trading engine reads the right pool for each strategy so the correct 5 stocks
     are always fed to the right strategy on any given day.
  6. Publishes market breadth (advancing/declining ratio) to Redis (market:breadth)
  7. Publishes market-wide avg ATR%/EMA-spread% to Redis (market:trend_stats) —
     the regime detector's proxy for "NIFTY ATR%" since no index tick is subscribed
  8. Fetches 15-min OHLC for multi-timeframe EMA confirmation (tick15:SYMBOL)

Why today's bars never come from historical_data() (2026-07-18 → 2026-07-27):
  Zerodha's historical_data() API was observed lagging same-day intraday candles
  by 5+ hours EVERY trading day from 2026-07-17 through 07-24 — confirmed via a
  direct, uncached call returning candles capped at 09:45 IST when queried at
  15:16 IST, repeating fresh every calendar day regardless of container uptime.
  Zerodha support confirmed in writing (2026-07-27) this is fundamental, not a
  bug: "it is not guaranteed that a minute candle will be available immediately
  ... delays can occur, and if one write is delayed, subsequent writes may also
  be delayed... we recommend generating the candles at your end using the live
  ticks received through Kite Ticker, rather than relying on the Historical Data
  API for the current trading session." Since the delay has no guaranteed upper
  bound, a staleness-threshold check (the original 2026-07-18 fix) could still
  misjudge a historical candle as "fresh enough" on a day the lag happens to be
  short. So today's bars are now ALWAYS built from live ticks (ZerodhaTicker
  WebSocket / ZerodhaLTPPoller REST — see update_intraday_bar() in
  core/utils.py) as real, growing 5-min OHLC bars, blended on top of the
  (still valid, unaffected) prior-day historical baseline. historical_data()'s
  role is now exactly what Zerodha says it's for: backtesting-grade baseline,
  never the current session. (An earlier version of this fix used a single
  synthetic "today" blob bar instead of a real per-bar series — too weak to
  move EMA20/50, since one bar only carries ~9.5%/3.9% weight against ~750
  baseline bars; confirmed via zero EMA crossover entries for two weeks despite
  the regime gate being open most of each session.)
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.core.constants import (
    ACTIVE_TRADING_SYMBOLS,
    FIVE_MIN_ATR_DAILY_SCALE,
    FNO_SYMBOLS,
    REDIS_TICK_PREFIX,
    REDIS_TOP_SYMBOLS_KEY,
    REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
    REDIS_TOP_SYMBOLS_IRON_CONDOR,
    REDIS_TOP_SYMBOLS_MOMENTUM,
    REDIS_TOP_SYMBOLS_EMA_BULL,
    REDIS_TOP_SYMBOLS_EMA_BEAR,
    REDIS_TOP_SYMBOLS_MOMENTUM_BULL,
    REDIS_TOP_SYMBOLS_MOMENTUM_BEAR,
)
from src.core.utils import now_ist

logger = logging.getLogger(__name__)

# Fixed 2026-09-15 (external review): a candidate pool key was previously
# either SET (candidates found) or DELETED (none found) -- both an actual
# empty pool and a poll() that crashed before reaching this symbol's score
# looked identical downstream (key absent). Each pool now also gets a
# companion ":status" key so a consumer (engine, dashboard, health check)
# can distinguish READY/EMPTY/ERROR instead of inferring from key absence.
_POOL_STATUS_SUFFIX = ":status"
_POOL_STATUS_TTL_SEC = 150  # generous vs the 60s poll cadence

HISTORY_REFRESH_SECONDS      = 300  # reload 5-min OHLC baseline every 5 min
_HISTORY_15M_REFRESH_SECONDS = 900  # reload 15-min OHLC baseline every 15 min

# ATR% thresholds that must match strategy parameters
_LOW_VOL_THRESHOLD = 1.2   # below = low volatility regime
_FLAT_EMA_THRESHOLD = 0.1  # EMA spread below = EMAs are flat (no direction)
# EMA crossover candidates should be NEAR a cross, not deep in an already-established
# trend — once EMA20/50 have diverged past this, the cross happened bars ago and the
# strategy's sign-change detection structurally cannot fire again without a reversal.
_EMA_PROXIMITY_CAP = 0.5

# Fixed 2026-09-15 (external review, "EMA event watchlist"): _get_active_symbols()
# reads whatever's in the top-N pool at the EXACT instant it's called, with no
# memory of who was a candidate a cycle ago. ema_score's proximity term
# (_EMA_PROXIMITY_CAP above) drives a stock's score toward its ATR-only floor
# within a cycle or two of actually crossing -- the spread widens past the
# proximity cap the moment the cross happens. A stock can therefore cross,
# then fall out of the top-N before the engine's next ~1-min signal cycle
# ever evaluates it -- the exact event the strategy exists to detect,
# disappearing from its own candidate pool as a side effect of detecting it.
# A symbol within this band gets watchlisted and kept in the published pool
# for _EMA_WATCHLIST_BARS cycles regardless of what its score does next.
_EMA_WATCHLIST_ENTRY_THRESHOLD = 0.20
_EMA_WATCHLIST_BARS = 4


class LTPPoller:
    """
    Fetches 5-min OHLC from Zerodha, computes indicators, writes to Redis.
    Scores all 40 symbols for three distinct trading regimes and publishes
    three separate top-N ranked lists so each strategy gets appropriate stocks.
    """

    def __init__(self, redis_client, symbols: List[str] = None,
                 kite=None, instrument_tokens: Dict[str, int] = None) -> None:
        self._redis   = redis_client
        self._kite    = kite
        self._tokens  = instrument_tokens or {}
        self.symbols  = symbols or FNO_SYMBOLS  # default: the full active universe
        # 2026-08-20: whether to auto-refresh self.symbols each poll() cycle
        # from the dynamically-recomputed active universe. Only when the
        # caller didn't pin an explicit symbols list at construction --
        # an explicit list means the caller wants exactly that list, not to
        # have it silently overwritten (matters for tests and any future
        # non-default caller).
        self._dynamic_symbols = symbols is None
        # Underlyings with a currently open position -- force-tracked
        # regardless of the active-universe membership above, so an exit
        # never loses market-data coverage just because its symbol's
        # liquidity later fell below the weekly recompute's floor. See
        # register_underlying()/unregister_underlying().
        self._must_track: set = set()
        # Fixed 2026-08-20 (code review): the liquidity-active subset of
        # self.symbols, tracked SEPARATELY from the must_track union above.
        # self.symbols itself (active | must_track) is what gets POLLED --
        # but only _active_set is eligible to compete for the top-N
        # entry-candidate pools published at the end of poll(). Without this
        # split, a force-tracked symbol (open position, but demoted below
        # the liquidity floor) still had its EMA/momentum/spread score
        # computed and could rank into a top-N pool, letting _process_signal
        # open a BRAND NEW reversal position on a symbol the weekly job
        # explicitly excluded -- turning a safety mechanism meant only to
        # preserve exit-management data into a backdoor into new-entry
        # eligibility. None means "no active/must-track distinction yet"
        # (before the first _refresh_active_symbols() call, or when
        # self._dynamic_symbols is False) -- treated as "don't filter" so
        # non-dynamic callers (tests, any future fixed-list caller) keep
        # their original all-symbols-eligible behavior.
        self._active_set: Optional[set] = None
        self._history: Dict[str, pd.DataFrame] = {}
        self._history_loaded_at: Dict[str, datetime] = {}
        self._history_15m: Dict[str, pd.DataFrame] = {}
        self._history_15m_loaded_at: Dict[str, datetime] = {}
        self._no_token_warned: set = set()    # suppress repeat "no token" warnings per symbol
        self._no_history_warned: set = set()  # suppress repeat "not enough history" warnings
        self._no_live_data_warned: set = set()  # suppress repeat "no live tick data yet" warnings
        # symbol -> poll cycles remaining on the EMA crossover watchlist --
        # see _EMA_WATCHLIST_ENTRY_THRESHOLD's docstring.
        self._ema_watchlist: Dict[str, int] = {}
        # Monotonic per-process poll counter -- see market:universe_health's
        # poll_seq/poll_epoch fields, published at the end of poll().
        self._poll_seq: int = 0

    def register_underlying(self, symbol: str) -> None:
        """
        Force-track this underlying regardless of the dynamically-recomputed
        active universe. Call when a new position opens on it -- an open
        position needs live indicators for exit management even if the
        symbol later drops below the weekly liquidity recompute's floor.
        """
        self._must_track.add(symbol)
        logger.info(f"LTPPoller: force-tracking {symbol} (open position) -- {len(self._must_track)} total")

    def unregister_underlying(self, symbol: str) -> None:
        """Stop force-tracking this underlying. Call only once ALL positions on it are closed."""
        self._must_track.discard(symbol)
        logger.info(f"LTPPoller: released force-track on {symbol} -- {len(self._must_track)} remaining")

    async def _refresh_active_symbols(self) -> None:
        """
        Pull the latest dynamically-recomputed active universe and union it
        with any force-tracked (open-position) underlyings, updating
        self.symbols in place. No-op if the caller pinned an explicit
        symbols list at construction (self._dynamic_symbols is False).

        self._active_set is stored separately (not just self.symbols) so
        poll() can tell "polled for market-data continuity only" apart from
        "genuinely eligible to compete for a new-entry candidate pool" --
        see self._active_set's docstring in __init__ for why that split
        matters.
        """
        if not self._dynamic_symbols:
            return
        from src.market_data.option_chain import get_active_fno_symbols
        active = await get_active_fno_symbols(self._redis)
        self._active_set = set(active)
        self.symbols = sorted(self._active_set | self._must_track)

    def set_kite(self, kite, instrument_tokens: Dict[str, int]) -> None:
        """
        (Re)attach a kite client + instrument tokens after construction.

        kite/instrument_tokens used to be a one-time constructor snapshot — if
        no valid Zerodha token existed in Redis at the exact moment this
        poller was created (e.g. a restart during a token-expiry window),
        self._kite stayed None for the process's entire lifetime with no way
        to recover, even once a fresh token showed up later. Confirmed this
        silently broke all indicator/regime data for 3 full trading days
        (2026-07-27 through 07-29) after one Sunday-evening restart. Called by
        the periodic self-healing job in api/main.py once a working kite
        client becomes available.
        """
        self._kite   = kite
        self._tokens = instrument_tokens or {}

    async def _publish_pool_error(self, reason: str) -> None:
        """Mark all four candidate pools ERROR -- called when poll() fails
        before it can even attempt per-symbol scoring. Deliberately does NOT
        touch the base list keys (nfo:top5*) -- leaving the last-known-good
        candidate list in place with a visible ERROR status is safer than
        engine callers suddenly seeing zero candidates from one bad cycle."""
        payload = json.dumps({
            "status": "ERROR", "symbols_count": 0,
            "generated_at": now_ist().replace(tzinfo=None).isoformat(),
            "reason": reason,
        })
        for key in (REDIS_TOP_SYMBOLS_KEY, REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
                    REDIS_TOP_SYMBOLS_IRON_CONDOR, REDIS_TOP_SYMBOLS_MOMENTUM):
            try:
                await self._redis.set(f"{key}{_POOL_STATUS_SUFFIX}", payload, ex=_POOL_STATUS_TTL_SEC)
            except Exception as exc:
                logger.error(f"LTPPoller: failed to publish ERROR pool status for {key}: {exc}")

    async def _publish_pool(self, key: str, scores: Dict[str, float], n: int,
                             empty_reason: str, force_include: Optional[set] = None) -> None:
        """Publish a candidate pool's top-N list (existing wire format,
        unchanged for backward compatibility) plus a companion ":status" key
        so a consumer can tell READY (n>0 candidates) apart from EMPTY (poll
        succeeded, genuinely nothing qualified today) without confusing
        either with ERROR (poll itself failed -- see _publish_pool_error).

        force_include (2026-09-15, external review "EMA event watchlist"):
        symbols that must stay in the published list even if they fell out
        of the natural top-N by score -- see self._ema_watchlist's docstring
        for why a stock that just crossed can otherwise vanish from the pool
        before the engine's next cycle evaluates it. Appended AFTER the
        natural top-N (not competing for rank), deduped, so this can never
        crowd out an unrelated stronger candidate."""
        generated_at = now_ist().replace(tzinfo=None).isoformat()
        if scores:
            top = sorted(scores, key=scores.__getitem__, reverse=True)[:n]
            if force_include:
                top = top + [s for s in force_include if s not in top and s in scores]
            await self._redis.set(key, json.dumps(top))
            status = {"status": "READY", "symbols_count": len(top), "generated_at": generated_at}
        else:
            await self._redis.delete(key)
            status = {"status": "EMPTY", "symbols_count": 0, "generated_at": generated_at, "reason": empty_reason}
        await self._redis.set(f"{key}{_POOL_STATUS_SUFFIX}", json.dumps(status), ex=_POOL_STATUS_TTL_SEC)
        return status.get("status")

    async def poll(self) -> None:
        """Called every 60 s by APScheduler."""
        from src.core.utils import is_market_open
        if not is_market_open():
            return

        loop = asyncio.get_running_loop()

        try:
            # Refresh self.symbols from the dynamically-recomputed active
            # universe (unioned with any open-position underlying) BEFORE the
            # OHLC prefetch below, so a symbol the weekly job just added starts
            # getting polled the same cycle instead of waiting for a restart.
            await self._refresh_active_symbols()

            # Warm both OHLC caches concurrently before the sequential per-symbol
            # loop below -- see _prefetch_stale_histories()'s docstring. By the
            # time the loop calls _get_history()/_get_history_15m(), any symbol
            # prefetched here just returns the now-warm cache, no blocking I/O.
            await self._prefetch_stale_histories(
                self.symbols, loop, self._fetch_kite_ohlc,
                self._history_loaded_at, self._history, HISTORY_REFRESH_SECONDS,
            )
            await self._prefetch_stale_histories(
                self.symbols, loop, self._fetch_kite_ohlc_15m,
                self._history_15m_loaded_at, self._history_15m, _HISTORY_15M_REFRESH_SECONDS,
            )
        except Exception as exc:
            # Fixed 2026-09-15 (external review): a failure here used to
            # propagate up uncaught (or, depending on caller, silently abort
            # the whole poll with no trace of WHY every pool then went stale/
            # empty). Now explicitly marks every pool ERROR with the real
            # reason before re-raising, so a consumer sees "poller broke",
            # not "market has zero candidates today".
            logger.error(f"LTPPoller: poll() setup failed, marking all pools ERROR: {exc}")
            await self._publish_pool_error(str(exc))
            raise

        # Age out the EMA watchlist one cycle before this cycle's scoring
        # re-populates it -- see _EMA_WATCHLIST_ENTRY_THRESHOLD's docstring.
        self._ema_watchlist = {
            sym: bars - 1 for sym, bars in self._ema_watchlist.items() if bars - 1 > 0
        }

        ema_scores: Dict[str, float] = {}
        spread_scores: Dict[str, float] = {}
        condor_scores: Dict[str, float] = {}
        momentum_scores: Dict[str, float] = {}
        # Fixed 2026-09-15 (external review, "separate bull/bear candidate
        # pools"): bucketed by CURRENT EMA20-vs-EMA50 relationship alongside
        # (not instead of) the combined dicts above -- see
        # REDIS_TOP_SYMBOLS_EMA_BULL's docstring in core/constants.py.
        ema_bull_scores: Dict[str, float] = {}
        ema_bear_scores: Dict[str, float] = {}
        momentum_bull_scores: Dict[str, float] = {}
        momentum_bear_scores: Dict[str, float] = {}
        all_ticks: list = []  # collected for market-breadth computation after the loop
        # Fixed 2026-09-15 (external review, "universe/candidate visibility"):
        # counts how many of self.symbols actually had enough history to be
        # scored this cycle -- without this, "scanning 40 stocks" could
        # silently mean 5 in practice (a widespread history-fetch outage)
        # with zero visible signal. See market:universe_health below.
        _history_valid_count = 0

        for symbol in self.symbols:
            try:
                df = await self._get_history(symbol, loop)
                if df is None or len(df) < 50:
                    if symbol not in self._no_history_warned:
                        logger.warning(f"Not enough history for {symbol} (need 50 bars), skipping (won't repeat).")
                        self._no_history_warned.add(symbol)
                    continue
                _history_valid_count += 1

                ltp = float(df["close"].iloc[-1])

                # Live tick-derived day range + intraday bars (written by ZerodhaTicker /
                # ZerodhaLTPPoller). This is now the ONLY source used for today's own
                # bars — historical_data() only supplies the prior-day baseline (see
                # module docstring for why). Read every cycle so it survives this
                # poll's full tick overwrite below instead of resetting every 60s.
                day_range = await self._read_day_range(symbol)

                if day_range and day_range.get("close"):
                    ltp = float(day_range["close"])
                elif symbol not in self._no_live_data_warned:
                    # Bootstrap edge case only (e.g. the first few seconds after
                    # market open before any tick has arrived yet) — falls back to
                    # the historical close so indicators are still defined.
                    self._no_live_data_warned.add(symbol)
                    logger.warning(
                        f"LTPPoller: {symbol} has no live tick data yet — using "
                        f"historical_data() close as a bootstrap fallback (won't repeat)."
                    )
                if day_range and symbol in self._no_live_data_warned:
                    self._no_live_data_warned.discard(symbol)

                tick = self._enrich(symbol, df, ltp, live_range=day_range)
                if day_range:
                    # Carry the live day-range + intraday-bar fields forward across this
                    # overwrite — otherwise bars_today would be wiped every 60s instead
                    # of accumulating through the day (see update_intraday_bar()).
                    # cur_bar_volume/_last_cum_volume MUST be included here too: they're
                    # exactly what update_intraday_bar() needs to keep accumulating real
                    # per-tick volume into the still-forming bar. Dropping them made the
                    # next WebSocket tick see _last_cum_volume=None (delta computed as 0)
                    # and cur_bar_volume reset to 0, so most 5-min bars ended up recording
                    # only the last <60s of volume before finalization instead of the true
                    # 5-minute total -- silently corrupting RVOL (momentum_v1) and
                    # session_vwap (credit_spread_v1). Fixed 2026-08-21.
                    for k in ("day_open", "day_high", "day_low", "day_range_date",
                              "bars_today", "cur_bar_key", "cur_bar_open",
                              "cur_bar_high", "cur_bar_low", "cur_bar_close",
                              "cur_bar_volume", "_last_cum_volume"):
                        if k in day_range:
                            tick[k] = day_range[k]
                await self._redis.set(f"{REDIS_TICK_PREFIX}{symbol}", json.dumps(tick))
                all_ticks.append(tick)

                # 15-min OHLC for multi-timeframe EMA confirmation (MTF feature).
                # Same live-ticks-for-today approach as the 5-min feed above, reusing
                # the same day_range (today's range doesn't depend on bar granularity).
                df15 = await self._get_history_15m(symbol, loop)
                if df15 is not None and len(df15) >= 50:
                    tick15 = self._enrich_15m(symbol, df15, live_range=day_range)
                    await self._redis.set(f"tick15:{symbol}", json.dumps(tick15), ex=1800)

                e, s, c, m = self._score_all(tick)
                # Fixed 2026-08-20 (code review): a symbol only polled because
                # it's force-tracked (open position, demoted below the
                # liquidity floor -- see _active_set's docstring in __init__)
                # must not compete for a new-entry candidate pool. Market
                # data (the Redis tick/tick15 writes above) is still kept
                # current for it either way, for exit management.
                if self._active_set is None or symbol in self._active_set:
                    ema_scores[symbol] = e
                    if s > 0:
                        spread_scores[symbol] = s
                    if c > 0:
                        condor_scores[symbol] = c
                    if m > 0:
                        momentum_scores[symbol] = m
                    # Fixed 2026-09-15 (external review, "separate bull/bear
                    # candidate pools") -- current EMA20-vs-EMA50 sign, same
                    # classifier both strategies' pools use.
                    _is_bullish = tick.get("ema20", 0) > tick.get("ema50", 0)
                    (ema_bull_scores if _is_bullish else ema_bear_scores)[symbol] = e
                    if m > 0:
                        (momentum_bull_scores if _is_bullish else momentum_bear_scores)[symbol] = m
                    # Fixed 2026-09-15 (external review, "EMA event
                    # watchlist"): refresh (not just add) on every cycle the
                    # symbol stays within the entry band -- a stock hovering
                    # near a cross for several bars keeps its full window
                    # each time, rather than the clock silently running out
                    # while it's still genuinely close.
                    if tick.get("ema_spread_pct", 999) < _EMA_WATCHLIST_ENTRY_THRESHOLD:
                        self._ema_watchlist[symbol] = _EMA_WATCHLIST_BARS

                logger.debug(
                    f"Tick: {symbol} ltp={ltp:.2f} "
                    f"ema_score={e:.3f} spread_score={s:.3f} condor_score={c:.3f} "
                    f"momentum_score={m:.3f} adx={tick.get('adx14', 0):.1f} rvol={tick.get('rvol', 0):.2f}"
                )
            except Exception as exc:
                logger.error(f"LTP poll failed for {symbol}: {exc}")

        # Market breadth — advancing/declining ratio across all polled symbols.
        # Uses day_prev_close (real previous-trading-day close), NOT the
        # bar-to-bar `prev_close` -- see _enrich()'s day_prev_close comment
        # for why the old bar-to-bar version made this swing wildly within
        # single trading sessions despite being described as day-level
        # sentiment everywhere it's consumed (credit_spread_v1/iron_condor_v1
        # breadth gates in live_trading_engine.py).
        if all_ticks:
            _adv = sum(1 for t in all_ticks if t.get("close", 0) > t.get("day_prev_close", 0))
            _dec = sum(1 for t in all_ticks if t.get("close", 0) < t.get("day_prev_close", 0))
            _tot = _adv + _dec
            _breadth = round(_adv / _tot, 4) if _tot > 0 else 0.5
            await self._redis.set(
                "market:breadth",
                json.dumps({
                    "breadth": _breadth, "advancing": _adv,
                    "declining": _dec,   "total": _tot,
                    "timestamp": datetime.now().isoformat(),
                }),
                ex=120,  # 2-min TTL — poll runs every 60 s
            )
            logger.info(f"[Breadth] {_breadth:.1%} advancing ({_adv}/{_tot})")

        if self._no_live_data_warned:
            logger.warning(
                f"LTPPoller: {len(self._no_live_data_warned)}/{len(self.symbols)} symbols "
                f"still have no live tick data (bootstrap fallback to historical_data() "
                f"close in effect): {sorted(self._no_live_data_warned)}"
            )

        # Market-wide trend stats — regime detector's proxy for "NIFTY ATR%/EMA spread%"
        # since no NIFTY50 index tick is subscribed. atr_pct here is raw 5-min-bar ATR%,
        # scaled to a daily-equivalent figure so it's comparable to a daily-ATR% threshold
        # (see FIVE_MIN_ATR_DAILY_SCALE). ema_spread_pct is already a price-level stat and
        # needs no such scaling.
        if all_ticks:
            _atrs = [t["atr_pct"] for t in all_ticks if t.get("atr_pct") is not None]
            _emas = [t["ema_spread_pct"] for t in all_ticks if t.get("ema_spread_pct") is not None]
            if _atrs and _emas:
                _avg_atr_pct_daily  = round((sum(_atrs) / len(_atrs)) * FIVE_MIN_ATR_DAILY_SCALE, 4)
                _avg_ema_spread_pct = round(sum(_emas) / len(_emas), 4)
                await self._redis.set(
                    "market:trend_stats",
                    json.dumps({
                        "avg_atr_pct_daily":         _avg_atr_pct_daily,
                        "avg_ema_spread_pct":        _avg_ema_spread_pct,
                        "n_symbols":                 len(_atrs),
                        "symbols_without_live_data": len(self._no_live_data_warned),
                        "all_symbols_live":          len(self._no_live_data_warned) == 0,
                        "timestamp":                 datetime.now().isoformat(),
                    }),
                    ex=120,  # 2-min TTL — poll runs every 60 s
                )

        # Fixed 2026-09-15 (external review, "universe/candidate
        # visibility"): "the strategy is scanning 40 stocks" could
        # previously silently mean far fewer in practice (a widespread
        # history-fetch outage, or most of the universe stuck on the
        # bootstrap fallback) with no visible signal short of grepping logs
        # for the per-symbol warnings above. candidate_count is
        # len(ema_scores) -- EMA always scores every symbol with valid
        # history (no floor gate, see _score_all's docstring), so it's the
        # accurate "how many symbols were even considered this cycle" count.
        # Fixed 2026-09-16 (external review round 2, "LTP -> signal-cycle
        # dependency ordering"): poll_seq/poll_epoch mark the exact moment
        # THIS cycle's tick:{symbol} writes are all complete -- LTPPoller
        # and LiveTradingEngine run on independent scheduler timers with no
        # hard dependency between them, so the signal cycle could otherwise
        # read Redis mid-poll (some symbols this cycle's data, some the
        # prior cycle's) or read a poll that stalled minutes ago with no
        # way to tell. See LiveTradingEngine._maybe_skip_entries_for_stale_
        # market_data() for the consumer side (a staleness check only --
        # NOT a strict "must be a newer poll_seq than last time" gate; see
        # that method's docstring for why the stricter version was
        # deliberately not built). poll_epoch is a plain time.time() float,
        # not an ISO string -- avoids any IST/local-time ambiguity for a
        # same-process age comparison.
        self._poll_seq += 1
        await self._redis.set(
            "market:universe_health",
            json.dumps({
                "universe_size":   len(self.symbols),
                "history_valid":   _history_valid_count,
                "live_data_valid": len(self.symbols) - len(self._no_live_data_warned),
                "candidate_count": len(ema_scores),
                "poll_seq":        self._poll_seq,
                "poll_epoch":      time.time(),
                "timestamp":       datetime.now().isoformat(),
            }),
            ex=120,  # 2-min TTL — poll runs every 60 s
        )

        n = ACTIVE_TRADING_SYMBOLS

        # Fixed 2026-09-15 (external review): each pool now publishes a
        # companion ":status" key (READY/EMPTY/ERROR, see _publish_pool) so a
        # consumer can distinguish "genuinely nothing qualified today" from
        # "poller broke" instead of both looking like a missing/absent key.
        # EMA previously had NO empty-case handling at all (no else branch,
        # no TTL on the base key) -- if ema_scores were ever empty the key
        # would silently keep serving an arbitrarily stale list forever;
        # unified onto the same _publish_pool() path as the other three pools.
        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_KEY, ema_scores, n,
            empty_reason="No symbol scored (should not normally happen -- ema_score has no floor gate)",
            force_include=set(self._ema_watchlist),
        )
        logger.info(f"EMA pool: {status} (watchlist: {sorted(self._ema_watchlist)})")

        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_CREDIT_SPREAD, spread_scores, n,
            empty_reason="No symbols in low-vol directional regime today (ATR% all >= 1.2%)",
        )
        logger.info(f"Credit spread pool: {status}")

        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_IRON_CONDOR, condor_scores, n,
            empty_reason="No symbols eligible today (all have directional EMA or high ATR%)",
        )
        logger.info(f"Iron condor pool: {status}")

        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_MOMENTUM, momentum_scores, n,
            empty_reason="No symbols eligible today (ADX all < 25)",
        )
        logger.info(f"Momentum pool: {status}")

        # Fixed 2026-09-15 (external review, "separate bull/bear candidate
        # pools"): each side gets its OWN full top-N, so a day dominated by
        # one direction can't have its candidates crowded out of a single
        # shared top-N by unrelated noise on the other side -- see
        # REDIS_TOP_SYMBOLS_EMA_BULL's docstring in core/constants.py. The
        # engine takes the union of both (LiveTradingEngine._get_active_symbols()).
        # force_include passes the SAME watchlist set to both EMA sides --
        # _publish_pool's own `s in scores` guard means a bear-side
        # watchlisted symbol can never spuriously appear in the bull pool.
        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_EMA_BULL, ema_bull_scores, n,
            empty_reason="No bullish-structured symbol scored today",
            force_include=set(self._ema_watchlist),
        )
        logger.info(f"EMA bull pool: {status}")
        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_EMA_BEAR, ema_bear_scores, n,
            empty_reason="No bearish-structured symbol scored today",
            force_include=set(self._ema_watchlist),
        )
        logger.info(f"EMA bear pool: {status}")
        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_MOMENTUM_BULL, momentum_bull_scores, n,
            empty_reason="No bullish-structured symbol with ADX >= 25 today",
        )
        logger.info(f"Momentum bull pool: {status}")
        status = await self._publish_pool(
            REDIS_TOP_SYMBOLS_MOMENTUM_BEAR, momentum_bear_scores, n,
            empty_reason="No bearish-structured symbol with ADX >= 25 today",
        )
        logger.info(f"Momentum bear pool: {status}")

    async def _read_day_range(self, symbol: str) -> Optional[dict]:
        """
        Read today's tick (written by ZerodhaTicker / ZerodhaLTPPoller), which
        carries the live day_open/day_high/day_low/day_range_date fields plus
        the real intraday bar series (bars_today, cur_bar_*) maintained by
        update_intraday_bar() in core/utils.py — the only source used for
        today's own bars (see module docstring). Returns None if no live tick
        exists yet or it's not from today (bootstrap edge case only).
        """
        try:
            raw = await self._redis.get(f"{REDIS_TICK_PREFIX}{symbol}")
            if not raw:
                return None
            data = json.loads(raw)
            if data.get("day_range_date") != now_ist().date().isoformat():
                return None
            if "day_high" not in data or "day_low" not in data:
                return None
            return data
        except Exception as e:
            logger.debug(f"LTPPoller: day-range read failed for {symbol}: {e}")
            return None

    # Bounded concurrency for the OHLC prefetch below -- NOT full-parallel
    # across every symbol needing a refresh in the same cycle, to stay well
    # under Zerodha's request-rate limits rather than trading a cold-start
    # timing risk for a rate-limit risk. Diagnostic run (scripts/
    # diagnostic_universe_timing.py, 2026-08-20) completed 208 SEQUENTIAL
    # kite.historical_data() calls with zero errors at ~0.23s/call, so 5
    # concurrent requests has a wide safety margin either way.
    _MAX_CONCURRENT_OHLC_FETCHES = 5

    async def _prefetch_stale_histories(
        self, symbols: List[str], loop, fetch_fn, loaded_at: Dict[str, datetime],
        history: Dict[str, pd.DataFrame], refresh_seconds: int,
    ) -> None:
        """
        Fetch OHLC concurrently (bounded) for every symbol whose cache is
        stale, instead of poll()'s per-symbol loop hitting
        kite.historical_data() one at a time.

        Added 2026-08-20: at the 41-symbol universe, the sequential version
        comfortably fit the 60s signal-cycle budget (measured ~9s for a full
        cold-start burst). Scaling toward the real 208-symbol NSE F&O
        universe (see FNO_SECTORS' 2026-08-20 comment), a cold start (every
        container restart) hits ~48s sequentially for the 5-min OHLC alone --
        close enough to the 60s budget, before the 15-min OHLC fetch and
        indicator computation are even added, that one slow Zerodha response
        could push a scheduled cycle past its misfire_grace_time=30s and get
        it skipped. This is called once per refresh kind (5-min, 15-min) at
        the top of poll(), before the main per-symbol loop -- by the time
        that loop calls _get_history()/_get_history_15m(), the caches it
        needs are already warm and those calls return instantly.
        """
        now = datetime.now()
        stale = [
            s for s in symbols
            if self._kite and s in self._tokens
            and (loaded_at.get(s) is None or (now - loaded_at[s]).total_seconds() > refresh_seconds)
        ]
        if not stale:
            return

        semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_OHLC_FETCHES)

        async def _fetch_one(symbol: str) -> None:
            async with semaphore:
                try:
                    df = await loop.run_in_executor(None, fetch_fn, symbol)
                except Exception as e:
                    logger.warning(f"LTPPoller: concurrent OHLC prefetch failed for {symbol}: {e}")
                    df = None
                loaded_at[symbol] = datetime.now()
                if df is not None and not df.empty:
                    history[symbol] = df

        await asyncio.gather(*(_fetch_one(s) for s in stale))

    async def _get_history(self, symbol: str, loop) -> Optional[pd.DataFrame]:
        """Return Zerodha 5-min OHLC, refreshing cache every 5 minutes.
        On fetch failure the timestamp is still updated so the symbol is not
        retried every 60 s — it waits the full HISTORY_REFRESH_SECONDS before retry."""
        now = datetime.now()
        last = self._history_loaded_at.get(symbol)
        stale = last is None or (now - last).total_seconds() > HISTORY_REFRESH_SECONDS

        if stale:
            if self._kite and symbol in self._tokens:
                df = await loop.run_in_executor(None, self._fetch_kite_ohlc, symbol)
            else:
                if symbol not in self._no_token_warned:
                    logger.warning(f"LTPPoller: no kite/token for {symbol} — skipping OHLC fetch (won't repeat).")
                    self._no_token_warned.add(symbol)
                df = None
            self._history_loaded_at[symbol] = now
            if df is not None and not df.empty:
                self._history[symbol] = df

        return self._history.get(symbol)

    def _fetch_kite_ohlc(self, symbol: str) -> Optional[pd.DataFrame]:
        """Blocking — runs in thread executor. Fetches 10 days of 5-min candles via kite."""
        from datetime import timedelta
        token    = self._tokens[symbol]
        to_date  = datetime.now()
        from_date = to_date - timedelta(days=10)
        try:
            records = self._kite.historical_data(
                token, from_date, to_date, "5minute", continuous=False, oi=False
            )
            if not records:
                return None
            df = pd.DataFrame(records)
            # Keep "date" so _enrich can produce ohlc_bar_key for true-bar confirmation
            cols = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
            df = df[cols].dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
            # Logs historical_data()'s actual last-candle timestamp on every refetch —
            # this is what originally surfaced the staleness (confirmed 2026-07-17,
            # later confirmed by Zerodha support as fundamental, not a bug — see
            # module docstring). Kept for visibility even though today's bars no
            # longer come from this call at all.
            if not df.empty and "date" in df.columns:
                logger.info(f"OHLC refresh: {symbol} bars={len(df)} last_bar={df['date'].iloc[-1]}")
            return df
        except Exception as e:
            logger.warning(f"kite.historical_data failed for {symbol}: {e}")
            return None

    @staticmethod
    def _enrich(symbol: str, df: pd.DataFrame, ltp: float, live_range: Optional[dict] = None) -> dict:
        """
        Compute EMA20, EMA50, ATR14, ADX14, RVOL, VWAP, prev_close from OHLC.

        live_range carries today's real, growing 5-min bar series (bars_today,
        built by update_intraday_bar() in core/utils.py from live ticks) plus
        the still-forming current bar. It's used whenever available — see
        module docstring for why today's bars never come from historical_data().
        Any bars historical_data() happens to have for today are dropped first
        so the same price action isn't double-counted from two sources. When
        live_range is None (bootstrap edge case only — e.g. the first few
        seconds after market open before any tick has arrived), falls back to
        the raw historical df as-is.
        """
        # Real previous-TRADING-DAY close, for market breadth (see refresh()
        # below) -- captured from the historical baseline with today's rows
        # excluded, before today's live rows get merged in. Distinct from
        # `prev_close` further down, which is bar-to-bar (previous 5-min
        # candle) and stays that way for its existing indicator-continuity
        # use -- this is specifically for "is this stock up or down TODAY",
        # which needs yesterday's actual close, not 5 minutes ago.
        #
        # Fixed 2026-08-06: market breadth was computed from bar-to-bar
        # prev_close, not day-over-day -- despite being described everywhere
        # ("broad market advancing/declining") as day-level sentiment.
        # Confirmed live: breadth swung 18.4% -> 68.4% -> 35.9% within ~20
        # minutes on 2026-08-06, which is implausible for genuine "% of
        # stocks up on the day" but exactly what 5-min-bar noise looks like.
        if "date" in df.columns:
            _today_str_dpc = now_ist().date().isoformat()
            _hist_only = df[df["date"].apply(lambda d: d.date().isoformat() != _today_str_dpc)]
            day_prev_close = float(_hist_only["close"].iloc[-1]) if len(_hist_only) > 0 else ltp
        else:
            day_prev_close = ltp

        has_live_data = live_range is not None
        if has_live_data:
            if "date" in df.columns:
                today_str = now_ist().date().isoformat()
                df = df[df["date"].apply(lambda d: d.date().isoformat() != today_str)]

            live_rows = [
                {
                    "date":   bar["date"],
                    "open":   bar["open"],
                    "high":   bar["high"],
                    "low":    bar["low"],
                    "close":  bar["close"],
                    # Real per-bar volume since the MODE_QUOTE switch (2026-07-30,
                    # see update_intraday_bar() in core/utils.py) — falls back to 0
                    # for any bar finalized before that change was deployed.
                    "volume": bar.get("volume", 0),
                }
                for bar in (live_range.get("bars_today") or [])
            ]
            if live_range.get("cur_bar_open") is not None:
                # Still-forming bar — latest partial candle, always included.
                live_rows.append({
                    "date":   now_ist(),
                    "open":   live_range.get("cur_bar_open", ltp),
                    "high":   live_range.get("cur_bar_high", ltp),
                    "low":    live_range.get("cur_bar_low", ltp),
                    "close":  ltp,
                    "volume": live_range.get("cur_bar_volume", 0),
                })
            if not live_rows:
                # No bars accumulated yet (e.g. moments after market open) —
                # fall back to a single blob bar from the day range so indicators
                # are still defined rather than computed on the historical df alone.
                live_rows = [{
                    "date":   now_ist(),
                    "open":   live_range.get("day_open", ltp),
                    "high":   live_range.get("day_high", ltp),
                    "low":    live_range.get("day_low", ltp),
                    "close":  ltp,
                    "volume": 0,
                }]
            # session_vwap: a genuine intraday VWAP built from ONLY today's live
            # rows (real volume as of the MODE_QUOTE switch, see above) — kept
            # separate from the existing multi-day `vwap` below rather than
            # replacing it, since callers already treat that one as a medium-term
            # (10-day) trend anchor (see live_trading_engine.py credit-spread
            # VWAP check). Computed here, before live_rows is merged into the
            # multi-day df, so it's unambiguous which bars fed it.
            _sess_typical = pd.Series([(r["high"] + r["low"] + r["close"]) / 3 for r in live_rows])
            _sess_vol = pd.Series([r["volume"] for r in live_rows]).replace(0, np.nan)
            _sess_cum_vol = _sess_vol.sum()
            session_vwap = (
                float((_sess_typical * _sess_vol).sum() / _sess_cum_vol)
                if _sess_cum_vol and _sess_cum_vol > 0 else ltp
            )
            df = pd.concat([df, pd.DataFrame(live_rows)], ignore_index=True)
        else:
            session_vwap = ltp

        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        volume = df["volume"]

        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr14 = float(tr.ewm(alpha=1.0/14, adjust=False).mean().iloc[-1])

        # ADX14 — Wilder's smoothed average directional index
        _alpha     = 1.0 / 14
        _hdiff     = high.diff()
        _ldiff     = -low.diff()    # prev_low - low
        _dm_plus   = pd.Series(
            np.where((_hdiff > _ldiff) & (_hdiff > 0), _hdiff, 0.0),
            index=high.index, dtype=float,
        )
        _dm_minus  = pd.Series(
            np.where((_ldiff > _hdiff) & (_ldiff > 0), _ldiff, 0.0),
            index=low.index, dtype=float,
        )
        _tr_w  = tr.ewm(alpha=_alpha, adjust=False).mean()
        _dmp_w = _dm_plus.ewm(alpha=_alpha, adjust=False).mean()
        _dmm_w = _dm_minus.ewm(alpha=_alpha, adjust=False).mean()
        _di_p  = 100.0 * _dmp_w / _tr_w.replace(0, np.nan)
        _di_m  = 100.0 * _dmm_w / _tr_w.replace(0, np.nan)
        _dx    = 100.0 * (_di_p - _di_m).abs() / (_di_p + _di_m).replace(0, np.nan)
        _adx_raw = _dx.ewm(alpha=_alpha, adjust=False).mean().iloc[-1]
        # adx_valid distinguishes "insufficient history for a real ADX yet"
        # (NaN -- e.g. before 14 bars of high/low history exist) from a
        # genuinely-computed low value. Both used to collapse to the same
        # sentinel (adx14=0.0), and every ADX gate in live_trading_engine.py
        # checked `if adx > 0` before applying its threshold -- so a missing
        # value silently passed every ADX-gated entry check instead of being
        # treated as "can't confirm, don't enter." Fixed 2026-08-06 alongside
        # the identical RVOL issue below.
        _adx_valid = not np.isnan(float(_adx_raw))
        adx14  = round(float(_adx_raw), 2) if _adx_valid else 0.0

        # RVOL — current bar volume relative to a 20-bar average of TODAY's
        # own bars.
        #
        # Fixed 2026-08-28 (code review): this used to run rolling(20) over
        # `volume` (the multi-day historical+live concatenated series, same
        # one ATR/EMA/ADX use), not just today's bars. That series has
        # ~750+ historical rows sitting before today's live rows, so the
        # rolling window is NEVER short enough to produce NaN -- meaning
        # rvol_valid was virtually always True, including on the very first
        # tick of a session, directly contradicting this comment's own
        # stated intent ("NaN until 20 real-volume bars exist, roughly the
        # first ~100 minutes"). Worse than just "the guard never fires":
        # during that real early-session window the average was silently
        # computed against a baseline dominated by PRIOR DAYS' final bars
        # -- typically the highest-volume bars of the whole session
        # (closing auction activity) -- systematically understating RVOL
        # for genuinely high early-session volume. Every caller relying on
        # rvol_valid to mean "can't confirm, don't enter" (momentum.py's
        # flat floor and two-tier pullback/breakout confirmation, the
        # shared RVOL gate in live_trading_engine.py) was instead getting a
        # confidently-labeled-valid number computed against the wrong
        # population for the first ~1.5-2 hours of every single trading
        # day. Fixed: use ONLY today's own bars (`live_rows`, already built
        # above for session_vwap) as the RVOL baseline -- correctly NaN/
        # unavailable until 20 of today's own bars genuinely exist.
        if has_live_data and len(live_rows) >= 20:
            _today_vol = pd.Series([r["volume"] for r in live_rows])
            _vol_avg20 = _today_vol.rolling(20).mean().iloc[-1]
            _rvol_valid = bool(_vol_avg20 and _vol_avg20 > 0 and not pd.isna(_vol_avg20))
            rvol = round(float(_today_vol.iloc[-1] / _vol_avg20), 2) if _rvol_valid else 0.0
        else:
            _rvol_valid = False
            rvol = 0.0

        # RVOL of the LAST COMPLETED 5-min bar, as opposed to `rvol` above
        # (the still-forming current bar's volume-so-far).
        #
        # Fixed 2026-09-04 (live incident): momentum.py's pullback+breakout
        # confirmation only evaluates on the `is_new_bar` transition -- the
        # exact instant a new 5-min bucket starts and `cur_bar_volume` resets
        # to 0 (see update_intraday_bar() in core/utils.py). `rvol` at that
        # instant is this brand-new bar's few-seconds-old volume compared
        # against a rolling average dominated by full, completed bars --
        # structurally deflated regardless of how strong the actual breakout
        # is. Confirmed live: every one of 41 breakout RVOL rejections logged
        # over 2+ weeks read 0.0-0.96, never once near the 0.8/1.3
        # thresholds, across many different stocks/dates/times -- not "volume
        # happened to be weak," a fixed measurement bias. momentum_v1's
        # pullback model (default since 2026-08-21) fired zero live trades in
        # that entire window as a result. This uses ONLY `bars_today`
        # (finalized bars with real, full-bar volume) so both the numerator
        # and the rolling-average population are on equal footing.
        if live_range and len(live_range.get("bars_today") or []) >= 20:
            _closed_vol = pd.Series([b.get("volume", 0) for b in live_range["bars_today"]])
            _closed_avg20 = _closed_vol.rolling(20).mean().iloc[-1]
            _rvol_closed_valid = bool(_closed_avg20 and _closed_avg20 > 0 and not pd.isna(_closed_avg20))
            rvol_closed_bar = round(float(_closed_vol.iloc[-1] / _closed_avg20), 2) if _rvol_closed_valid else 0.0
        else:
            _rvol_closed_valid = False
            rvol_closed_bar = 0.0

        # `vwap` is deliberately a multi-day cumulative figure (~750 historical
        # bars plus today's live bars) — a medium-term (10-day) trend anchor, see
        # live_trading_engine.py's credit-spread VWAP check. `session_vwap`
        # (computed above, today's live rows only) is the genuine intraday
        # counterpart for callers that actually want "today's session VWAP".
        typical = (high + low + close) / 3
        vol_nonzero = volume.replace(0, np.nan)
        cum_vol = vol_nonzero.sum()
        vwap = float((typical * vol_nonzero).sum() / cum_vol) if cum_vol > 0 else ltp

        atr_pct        = round((atr14 / ltp * 100) if ltp > 0 else 0, 4)
        ema_spread_pct = round((abs(ema20 - ema50) / ema50 * 100) if ema50 > 0 else 0, 4)
        prev_close     = round(float(close.iloc[-2]), 4) if len(close) > 1 else ltp

        # ohlc_bar_key — changes once per 5-min bar; strategies use this for true-bar
        # confirmation so that `signal_confirm_bars=2` means 2 distinct candles, not
        # 2 engine cycles that may both fall inside the same unfinished bar. With
        # live-tick data, the synthetic row's "date" is now_ist() (changes every
        # poll, i.e. every 60s) so it's bucketed to the current 5-min window
        # instead — keeps the "2 distinct candles" semantics intact.
        ohlc_bar_key: Optional[str] = None
        if has_live_data:
            _bucket = now_ist().replace(minute=(now_ist().minute // 5) * 5, second=0, microsecond=0)
            ohlc_bar_key = f"live:{_bucket.isoformat()}"
        elif "date" in df.columns:
            last_date = df["date"].iloc[-1]
            ohlc_bar_key = str(last_date)

        return {
            "symbol":         symbol,
            "close":          ltp,
            "prev_close":     prev_close,
            "day_prev_close": round(day_prev_close, 4),
            "ema20":          round(ema20, 4),
            "ema50":          round(ema50, 4),
            "atr14":          round(atr14, 4),
            "atr_pct":        atr_pct,
            "adx14":          adx14,
            "adx_valid":      _adx_valid,
            "rvol":           rvol,
            "rvol_valid":     _rvol_valid,
            "rvol_closed_bar":       rvol_closed_bar,
            "rvol_closed_bar_valid": _rvol_closed_valid,
            "ema_spread_pct": ema_spread_pct,
            "vwap":           round(vwap, 4),
            "session_vwap":   round(session_vwap, 4),
            "ohlc_bar_key":   ohlc_bar_key,
            "timestamp":      datetime.now().isoformat(),
            "ltp_source":     "zerodha_live_ticks" if has_live_data else "zerodha_historical",
        }

    @staticmethod
    def _score_all(tick: dict) -> tuple:
        """
        Score a symbol for all three strategy regimes.
        Returns (ema_score, spread_score, condor_score).

        EMA Crossover score (high = NEAR a crossover, moving enough to matter):
          ATR% × 0.3 + max(0, EMA_PROXIMITY_CAP - EMA_spread%) × 0.7
          → rewards stocks close to EMA20/50 crossing, moving enough to matter —
            NOT stocks already deep in an established trend. The strategy fires on
            the sign change between adjacent cycles; once the gap has widened past
            EMA_PROXIMITY_CAP, that already happened several bars ago and can't
            recur without a reversal, so wide-spread stocks score ~0 on this term
            regardless of how much they're moving.
            Fixed 2026-08-21 (external review of ema_crossover_v1): weights
            flipped from ATR×0.6/proximity×0.4 to proximity×0.7/ATR×0.3 --
            the review's point: "the strategy doesn't need stocks with high
            ATR, it needs stocks that are actually approaching a crossover."
            The old weighting let a high-ATR, far-from-crossing stock
            outscore a genuinely close-to-crossing, lower-ATR one.

        Credit Spread score (high = low-vol + directional, 0 if ATR% >= 1.2%):
          (1.2 - ATR%) × 0.4 + EMA_spread% × 0.6
          → rewards stocks that are trending gently without explosive moves
          → EMA spread weighted higher so we enter in the clearer direction

        Iron Condor score (high = low-vol + flat EMA, 0 if ATR% >= 1.2% or EMA spread >= 0.1%):
          (1.2 - ATR%) × 0.6 + (0.1 - EMA_spread%) × 0.4
          → rewards the most range-bound, stable stocks — ideal for both sides to expire

        Momentum score (high = strong, ESTABLISHED trend, 0 if ADX < 25):
          ADX × 0.7 + min(EMA_spread%, 1.0) × 0.3
          → the mirror image of the EMA crossover score: rewards stocks ALREADY
            deep in a strong trend (high ADX, wide EMA separation) instead of
            penalizing them. ADX weighted higher since it's the primary
            "is this trend for real" signal; EMA spread is capped at 1.0 so one
            extreme outlier doesn't dominate over genuinely high-ADX candidates.
        """
        close = tick.get("close", 0)
        if close <= 0:
            return 0.0, 0.0, 0.0, 0.0

        atr = tick.get("atr14", 0)
        ema20 = tick.get("ema20", close)
        ema50 = tick.get("ema50", close)
        adx = tick.get("adx14", 0)

        atr_pct = (atr / close) * 100
        ema_spread_pct = abs(ema20 - ema50) / ema50 * 100 if ema50 > 0 else 0.0

        # Regime 1: EMA crossover — always gets a score
        # Fixed 2026-08-21 (external review): weights flipped, proximity now
        # dominant -- see docstring above.
        ema_score = round(
            atr_pct * 0.3 + max(0.0, _EMA_PROXIMITY_CAP - ema_spread_pct) * 0.7, 4
        )

        # Regime 2: Credit spread — only when low vol
        if atr_pct < _LOW_VOL_THRESHOLD:
            spread_score = round(
                ((_LOW_VOL_THRESHOLD - atr_pct) * 0.4) + (ema_spread_pct * 0.6), 4
            )
        else:
            spread_score = 0.0

        # Regime 3: Iron condor — only when low vol AND flat EMA
        if atr_pct < _LOW_VOL_THRESHOLD and ema_spread_pct < _FLAT_EMA_THRESHOLD:
            condor_score = round(
                ((_LOW_VOL_THRESHOLD - atr_pct) * 0.6)
                + ((_FLAT_EMA_THRESHOLD - ema_spread_pct) * 0.4), 4
            )
        else:
            condor_score = 0.0

        # Regime 4: Momentum — only when trend is already strong
        if adx >= 25:
            momentum_score = round(adx * 0.7 + min(ema_spread_pct, 1.0) * 0.3, 4)
        else:
            momentum_score = 0.0

        return ema_score, spread_score, condor_score, momentum_score

    # ── 15-min multi-timeframe helpers ────────────────────────────────────────

    async def _get_history_15m(self, symbol: str, loop) -> Optional[pd.DataFrame]:
        """Return Zerodha 15-min OHLC, refreshing cache every 15 minutes."""
        now  = datetime.now()
        last = self._history_15m_loaded_at.get(symbol)
        stale = last is None or (now - last).total_seconds() > _HISTORY_15M_REFRESH_SECONDS

        if stale:
            if self._kite and symbol in self._tokens:
                df = await loop.run_in_executor(None, self._fetch_kite_ohlc_15m, symbol)
            else:
                df = None
            self._history_15m_loaded_at[symbol] = now
            if df is not None and not df.empty:
                self._history_15m[symbol] = df

        return self._history_15m.get(symbol)

    def _fetch_kite_ohlc_15m(self, symbol: str) -> Optional[pd.DataFrame]:
        """Blocking — runs in thread executor. Fetches 30 days of 15-min candles."""
        from datetime import timedelta
        token     = self._tokens[symbol]
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=30)
        try:
            records = self._kite.historical_data(
                token, from_date, to_date, "15minute", continuous=False, oi=False
            )
            if not records:
                return None
            df = pd.DataFrame(records)
            # Keep "date" so _enrich_15m can drop any of today's bars before
            # blending in the live-tick series (see module docstring).
            cols = [c for c in ["date", "open", "high", "low", "close"] if c in df.columns]
            return df[cols].dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        except Exception as e:
            logger.warning(f"kite.historical_data(15m) failed for {symbol}: {e}")
            return None

    @staticmethod
    def _enrich_15m(symbol: str, df: pd.DataFrame, live_range: Optional[dict] = None) -> dict:
        """
        Compute EMA20 and EMA50 on 15-min candles for multi-timeframe confirmation.

        live_range, when provided, carries today's live-tick bar series (same as
        _enrich()'s 5-min path — see that method's and the module's docstrings
        for why today's bars always come from live ticks), resampled into
        15-min groups (3 bars each) rather than the 5-min granularity. This
        feeds a hard gate in live_trading_engine.py (15-min EMA must agree with
        the 5-min signal), so it needs the same real per-bar treatment or it
        would keep blocking EMA crossover entries independently of the 5-min fix.
        """
        has_live_data = live_range is not None
        if has_live_data:
            if "date" in df.columns:
                today_str = now_ist().date().isoformat()
                df = df[df["date"].apply(lambda d: d.date().isoformat() != today_str)]

            live_rows = []
            bars5 = live_range.get("bars_today") or []
            if bars5:
                b5 = pd.DataFrame(bars5)
                b5["date"] = pd.to_datetime(b5["date"])
                b5["bucket15"] = b5["date"].apply(
                    lambda d: d.replace(minute=(d.minute // 15) * 15, second=0, microsecond=0)
                )
                grouped = b5.groupby("bucket15").agg(
                    open=("open", "first"), high=("high", "max"),
                    low=("low", "min"), close=("close", "last"),
                ).reset_index().rename(columns={"bucket15": "date"})
                live_rows = grouped.to_dict("records")

            if live_range.get("cur_bar_open") is not None:
                live_rows.append({
                    "date":  now_ist(),
                    "open":  live_range.get("cur_bar_open", 0),
                    "high":  live_range.get("cur_bar_high", 0),
                    "low":   live_range.get("cur_bar_low", 0),
                    "close": live_range.get("close", 0),
                })
            if not live_rows:
                live_rows = [{
                    "date":  now_ist(),
                    "open":  live_range.get("day_open", 0),
                    "high":  live_range.get("day_high", 0),
                    "low":   live_range.get("day_low", 0),
                    "close": live_range.get("close", 0),
                }]
            df = pd.concat([df, pd.DataFrame(live_rows)], ignore_index=True)

        close = df["close"]
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        return {
            "symbol":     symbol,
            "ema20":      round(ema20, 4),
            "ema50":      round(ema50, 4),
            "tf":         "15m",
            "ltp_source": "zerodha_live_ticks" if has_live_data else "zerodha_historical",
        }
