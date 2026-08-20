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
)
from src.core.utils import now_ist

logger = logging.getLogger(__name__)

HISTORY_REFRESH_SECONDS      = 300  # reload 5-min OHLC baseline every 5 min
_HISTORY_15M_REFRESH_SECONDS = 900  # reload 15-min OHLC baseline every 15 min

# ATR% thresholds that must match strategy parameters
_LOW_VOL_THRESHOLD = 1.2   # below = low volatility regime
_FLAT_EMA_THRESHOLD = 0.1  # EMA spread below = EMAs are flat (no direction)
# EMA crossover candidates should be NEAR a cross, not deep in an already-established
# trend — once EMA20/50 have diverged past this, the cross happened bars ago and the
# strategy's sign-change detection structurally cannot fire again without a reversal.
_EMA_PROXIMITY_CAP = 0.5


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

    async def poll(self) -> None:
        """Called every 60 s by APScheduler."""
        from src.core.utils import is_market_open
        if not is_market_open():
            return

        loop = asyncio.get_running_loop()

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

        ema_scores: Dict[str, float] = {}
        spread_scores: Dict[str, float] = {}
        condor_scores: Dict[str, float] = {}
        momentum_scores: Dict[str, float] = {}
        all_ticks: list = []  # collected for market-breadth computation after the loop

        for symbol in self.symbols:
            try:
                df = await self._get_history(symbol, loop)
                if df is None or len(df) < 50:
                    if symbol not in self._no_history_warned:
                        logger.warning(f"Not enough history for {symbol} (need 50 bars), skipping (won't repeat).")
                        self._no_history_warned.add(symbol)
                    continue

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
                    for k in ("day_open", "day_high", "day_low", "day_range_date",
                              "bars_today", "cur_bar_key", "cur_bar_open",
                              "cur_bar_high", "cur_bar_low", "cur_bar_close"):
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

        n = ACTIVE_TRADING_SYMBOLS

        # Publish EMA crossover pool (existing key — high ATR + strong trend)
        if ema_scores:
            top_ema = sorted(ema_scores, key=ema_scores.__getitem__, reverse=True)[:n]
            await self._redis.set(REDIS_TOP_SYMBOLS_KEY, json.dumps(top_ema))
            logger.info(f"EMA pool top-{n}: {top_ema}")

        # Publish credit spread pool (low ATR + EMA directional)
        if spread_scores:
            top_spread = sorted(spread_scores, key=spread_scores.__getitem__, reverse=True)[:n]
            await self._redis.set(REDIS_TOP_SYMBOLS_CREDIT_SPREAD, json.dumps(top_spread))
            logger.info(f"Credit spread pool top-{n}: {top_spread}")
        else:
            # No symbols in low-vol directional regime today — clear the key
            await self._redis.delete(REDIS_TOP_SYMBOLS_CREDIT_SPREAD)
            logger.info("Credit spread pool: no eligible symbols today (ATR% all >= 1.2%)")

        # Publish iron condor pool (low ATR + flat EMA)
        if condor_scores:
            top_condor = sorted(condor_scores, key=condor_scores.__getitem__, reverse=True)[:n]
            await self._redis.set(REDIS_TOP_SYMBOLS_IRON_CONDOR, json.dumps(top_condor))
            logger.info(f"Iron condor pool top-{n}: {top_condor}")
        else:
            await self._redis.delete(REDIS_TOP_SYMBOLS_IRON_CONDOR)
            logger.info("Iron condor pool: no eligible symbols today (all have directional EMA or high ATR%)")

        # Publish momentum pool (high ADX + wide EMA spread — established trend)
        if momentum_scores:
            top_momentum = sorted(momentum_scores, key=momentum_scores.__getitem__, reverse=True)[:n]
            await self._redis.set(REDIS_TOP_SYMBOLS_MOMENTUM, json.dumps(top_momentum))
            logger.info(f"Momentum pool top-{n}: {top_momentum}")
        else:
            await self._redis.delete(REDIS_TOP_SYMBOLS_MOMENTUM)
            logger.info("Momentum pool: no eligible symbols today (ADX all < 25)")

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

        # RVOL — current bar volume relative to 20-period average.
        # rvol_valid: same "insufficient data vs genuinely computed" split as
        # adx_valid above -- _vol_avg20 is NaN until 20 real-volume bars
        # exist (roughly the first ~100 minutes of every session), during
        # which the RVOL entry gate was silently disabled rather than
        # blocking (see live_trading_engine.py's momentum RVOL check).
        _vol_avg20 = volume.rolling(20).mean().iloc[-1]
        _rvol_valid = bool(_vol_avg20 and _vol_avg20 > 0)
        rvol = round(float(volume.iloc[-1] / _vol_avg20), 2) if _rvol_valid else 0.0

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

        EMA Crossover score (high = volatile + NEAR a crossover):
          ATR% × 0.6 + max(0, EMA_PROXIMITY_CAP - EMA_spread%) × 0.4
          → rewards stocks moving enough to matter AND close to EMA20/50 crossing —
            NOT stocks already deep in an established trend. The strategy fires on
            the sign change between adjacent cycles; once the gap has widened past
            EMA_PROXIMITY_CAP, that already happened several bars ago and can't
            recur without a reversal, so wide-spread stocks score ~0 on this term
            regardless of how much they're moving.

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
        ema_score = round(
            atr_pct * 0.6 + max(0.0, _EMA_PROXIMITY_CAP - ema_spread_pct) * 0.4, 4
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
