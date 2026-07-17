"""
LTP Poller — fetches 5-min OHLC from Zerodha and computes indicators.

Runs every 60 s via APScheduler:
  1. Polls all 40 F&O symbols via kite.historical_data() (5-min OHLC, 10-day window)
     — this is the PRIMARY data source.
  2. Computes EMA20, EMA50, ATR14, VWAP, ADX14, RVOL, prev_close for each symbol
  3. Writes enriched tick to Redis (tick:SYMBOL)
  4. Scores all 40 symbols THREE WAYS — one per strategy regime:
       - EMA Crossover pool  (nfo:top5)         : high ATR% + strong EMA trend
       - Credit Spread pool  (nfo:top5:spread)   : low ATR% (<1.2%) + EMA directional
       - Iron Condor pool    (nfo:top5:condor)   : low ATR% (<1.2%) + EMA flat (<0.1%)
  5. Trading engine reads the right pool for each strategy so the correct 5 stocks
     are always fed to the right strategy on any given day.
  6. Publishes market breadth (advancing/declining ratio) to Redis (market:breadth)
  7. Publishes market-wide avg ATR%/EMA-spread% to Redis (market:trend_stats) —
     the regime detector's proxy for "NIFTY ATR%" since no index tick is subscribed
  8. Fetches 15-min OHLC for multi-timeframe EMA confirmation (tick15:SYMBOL)

PRIMARY/SECONDARY data source fallback (added 2026-07-18):
  Zerodha's historical_data() API (PRIMARY, used above) has been observed lagging
  same-day intraday candles by 5+ hours even with the Historical Data API
  subscription active — confirmed via a direct, uncached call returning candles
  capped at 09:45 IST when queried at 15:16 IST. That's a live characteristic of
  Zerodha's historical pipeline, not something our own caching can fix.
  When a symbol's last historical candle is older than PRIMARY_STALE_THRESHOLD_SECONDS,
  _enrich() falls back to a SECONDARY source: today's running high/low/close tracked
  in real time from live ticks (ZerodhaTicker WebSocket / ZerodhaLTPPoller REST —
  see update_live_day_range() in core/utils.py), appended as one synthetic "today"
  bar on top of the (still valid) prior-day historical candles. Under normal
  conditions the PRIMARY feed is fresh and this fallback never engages.
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
)
from src.core.utils import now_ist

logger = logging.getLogger(__name__)

HISTORY_REFRESH_SECONDS      = 300  # reload 5-min OHLC every 5 min
_HISTORY_15M_REFRESH_SECONDS = 900  # reload 15-min OHLC every 15 min
# PRIMARY (historical_data) is considered stale beyond this age. Normal worst-case
# under healthy conditions is ~10 min (5-min refetch cadence + up to one 5-min bar
# not yet closed) — 20 min gives a comfortable margin so this never false-triggers
# during ordinary operation, while catching genuine multi-hour degradation fast.
PRIMARY_STALE_THRESHOLD_SECONDS = 1200
# Same idea for the 15-min MTF feed — larger normal worst-case (15-min refetch
# cadence + up to one unclosed 15-min bar), so a longer threshold.
PRIMARY_STALE_THRESHOLD_15M_SECONDS = 2400

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
        self.symbols  = symbols or FNO_SYMBOLS  # default: all 40
        self._history: Dict[str, pd.DataFrame] = {}
        self._history_loaded_at: Dict[str, datetime] = {}
        self._history_15m: Dict[str, pd.DataFrame] = {}
        self._history_15m_loaded_at: Dict[str, datetime] = {}
        self._no_token_warned: set = set()    # suppress repeat "no token" warnings per symbol
        self._no_history_warned: set = set()  # suppress repeat "not enough history" warnings
        self._fallback_active: set = set()    # symbols currently on the SECONDARY live-tick source

    async def poll(self) -> None:
        """Called every 60 s by APScheduler."""
        from src.core.utils import is_market_open
        if not is_market_open():
            return

        loop = asyncio.get_running_loop()

        ema_scores: Dict[str, float] = {}
        spread_scores: Dict[str, float] = {}
        condor_scores: Dict[str, float] = {}
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

                # Read the SECONDARY live-tick day range (written by ZerodhaTicker /
                # ZerodhaLTPPoller). Always read it — not just when falling back —
                # so it survives this poll's full tick overwrite below instead of
                # resetting to a single tick every 60s.
                day_range = await self._read_day_range(symbol)

                last_bar_date = df["date"].iloc[-1] if "date" in df.columns else None
                is_fallback = (
                    last_bar_date is not None
                    and (now_ist() - last_bar_date).total_seconds() > PRIMARY_STALE_THRESHOLD_SECONDS
                )

                if is_fallback:
                    if symbol not in self._fallback_active:
                        self._fallback_active.add(symbol)
                        age_min = (now_ist() - last_bar_date).total_seconds() / 60
                        logger.warning(
                            f"LTPPoller: {symbol} PRIMARY (historical_data) feed stale "
                            f"(last bar {age_min:.0f} min old) — switching to SECONDARY "
                            f"live-tick fallback for today's range."
                        )
                    if day_range and day_range.get("close"):
                        ltp = float(day_range["close"])
                elif symbol in self._fallback_active:
                    self._fallback_active.discard(symbol)
                    logger.info(f"LTPPoller: {symbol} PRIMARY feed recovered — back off SECONDARY fallback.")

                tick = self._enrich(symbol, df, ltp, live_range=day_range if is_fallback else None)
                if day_range:
                    # Carry the live day-range fields forward across this overwrite.
                    for k in ("day_open", "day_high", "day_low", "day_range_date"):
                        if k in day_range:
                            tick[k] = day_range[k]
                await self._redis.set(f"{REDIS_TICK_PREFIX}{symbol}", json.dumps(tick))
                all_ticks.append(tick)

                # 15-min OHLC for multi-timeframe EMA confirmation (MTF feature).
                # Same PRIMARY staleness check/SECONDARY fallback as the 5-min feed
                # above, reusing the same live day_range (today's range doesn't
                # depend on bar granularity).
                df15 = await self._get_history_15m(symbol, loop)
                if df15 is not None and len(df15) >= 50:
                    last_bar_15m = df15["date"].iloc[-1] if "date" in df15.columns else None
                    is_fallback_15m = (
                        last_bar_15m is not None
                        and (now_ist() - last_bar_15m).total_seconds() > PRIMARY_STALE_THRESHOLD_15M_SECONDS
                    )
                    live_range_15m = day_range if (is_fallback_15m and day_range and day_range.get("close")) else None
                    tick15 = self._enrich_15m(symbol, df15, live_range=live_range_15m)
                    await self._redis.set(f"tick15:{symbol}", json.dumps(tick15), ex=1800)

                e, s, c = self._score_all(tick)
                ema_scores[symbol] = e
                if s > 0:
                    spread_scores[symbol] = s
                if c > 0:
                    condor_scores[symbol] = c

                logger.debug(
                    f"Tick: {symbol} ltp={ltp:.2f} "
                    f"ema_score={e:.3f} spread_score={s:.3f} condor_score={c:.3f} "
                    f"adx={tick.get('adx14', 0):.1f} rvol={tick.get('rvol', 0):.2f}"
                )
            except Exception as exc:
                logger.error(f"LTP poll failed for {symbol}: {exc}")

        # Market breadth — advancing/declining ratio across all polled symbols
        if all_ticks:
            _adv = sum(1 for t in all_ticks if t.get("close", 0) > t.get("prev_close", 0))
            _dec = sum(1 for t in all_ticks if t.get("close", 0) < t.get("prev_close", 0))
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

        if self._fallback_active:
            logger.warning(
                f"LTPPoller: {len(self._fallback_active)}/{len(self.symbols)} symbols on "
                f"SECONDARY live-tick fallback (PRIMARY historical API stale): "
                f"{sorted(self._fallback_active)}"
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
                        "avg_atr_pct_daily":      _avg_atr_pct_daily,
                        "avg_ema_spread_pct":     _avg_ema_spread_pct,
                        "n_symbols":              len(_atrs),
                        "fallback_symbols":       len(self._fallback_active),
                        "primary_source_healthy": len(self._fallback_active) == 0,
                        "timestamp":              datetime.now().isoformat(),
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

    async def _read_day_range(self, symbol: str) -> Optional[dict]:
        """
        Read today's tick (written by ZerodhaTicker / ZerodhaLTPPoller), which
        carries live day_open/day_high/day_low/day_range_date fields maintained
        by update_live_day_range() in core/utils.py — the SECONDARY price-range
        source. Returns None if no live tick exists yet or it's not from today.
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
            # Logs the PRIMARY feed's actual last-candle timestamp on every refetch —
            # this is what surfaced the historical_data() staleness (confirmed
            # 2026-07-17) that the SECONDARY live-tick fallback below now handles.
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

        live_range, when provided, means PRIMARY (historical_data) was detected
        stale for `symbol` (see PRIMARY_STALE_THRESHOLD_SECONDS in poll()). A
        synthetic "today" bar is appended from the SECONDARY live-tick-derived
        day range (day_open/day_high/day_low + current ltp as close) so
        indicators reflect today's real price action instead of a frozen
        historical candle. Prior-day historical bars (still valid — only the
        current day lags) are kept as the base for EMA/ATR continuity.
        """
        is_fallback = live_range is not None
        if is_fallback:
            synthetic_row = pd.DataFrame([{
                "date":   now_ist(),
                "open":   live_range.get("day_open", ltp),
                "high":   live_range.get("day_high", ltp),
                "low":    live_range.get("day_low", ltp),
                "close":  ltp,
                "volume": 0,
            }])
            df = pd.concat([df, synthetic_row], ignore_index=True)

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
        adx14  = round(float(_adx_raw), 2) if not np.isnan(float(_adx_raw)) else 0.0

        # RVOL — current bar volume relative to 20-period average
        _vol_avg20 = volume.rolling(20).mean().iloc[-1]
        rvol = round(float(volume.iloc[-1] / _vol_avg20), 2) if (_vol_avg20 and _vol_avg20 > 0) else 0.0

        typical = (high + low + close) / 3
        vol_nonzero = volume.replace(0, np.nan)
        cum_vol = vol_nonzero.sum()
        vwap = float((typical * vol_nonzero).sum() / cum_vol) if cum_vol > 0 else ltp

        atr_pct        = round((atr14 / ltp * 100) if ltp > 0 else 0, 4)
        ema_spread_pct = round((abs(ema20 - ema50) / ema50 * 100) if ema50 > 0 else 0, 4)
        prev_close     = round(float(close.iloc[-2]), 4) if len(close) > 1 else ltp

        # ohlc_bar_key — changes once per 5-min bar; strategies use this for true-bar
        # confirmation so that `signal_confirm_bars=2` means 2 distinct candles, not
        # 2 engine cycles that may both fall inside the same unfinished bar. In
        # fallback mode the synthetic row's "date" is now_ist() (changes every poll,
        # i.e. every 60s) so it's bucketed to the current 5-min window instead —
        # keeps the "2 distinct candles" semantics intact under the SECONDARY source.
        ohlc_bar_key: Optional[str] = None
        if is_fallback:
            _bucket = now_ist().replace(minute=(now_ist().minute // 5) * 5, second=0, microsecond=0)
            ohlc_bar_key = f"fallback:{_bucket.isoformat()}"
        elif "date" in df.columns:
            last_date = df["date"].iloc[-1]
            ohlc_bar_key = str(last_date)

        return {
            "symbol":         symbol,
            "close":          ltp,
            "prev_close":     prev_close,
            "ema20":          round(ema20, 4),
            "ema50":          round(ema50, 4),
            "atr14":          round(atr14, 4),
            "atr_pct":        atr_pct,
            "adx14":          adx14,
            "rvol":           rvol,
            "ema_spread_pct": ema_spread_pct,
            "vwap":           round(vwap, 4),
            "ohlc_bar_key":   ohlc_bar_key,
            "timestamp":      datetime.now().isoformat(),
            "ltp_source":     "live_fallback_today" if is_fallback else "zerodha_historical",
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
        """
        close = tick.get("close", 0)
        if close <= 0:
            return 0.0, 0.0, 0.0

        atr = tick.get("atr14", 0)
        ema20 = tick.get("ema20", close)
        ema50 = tick.get("ema50", close)

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

        return ema_score, spread_score, condor_score

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
            # Keep "date" so poll() can detect PRIMARY staleness the same way it does
            # for the 5-min feed (see PRIMARY_STALE_THRESHOLD_SECONDS).
            cols = [c for c in ["date", "open", "high", "low", "close"] if c in df.columns]
            return df[cols].dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
        except Exception as e:
            logger.warning(f"kite.historical_data(15m) failed for {symbol}: {e}")
            return None

    @staticmethod
    def _enrich_15m(symbol: str, df: pd.DataFrame, live_range: Optional[dict] = None) -> dict:
        """
        Compute EMA20 and EMA50 on 15-min candles for multi-timeframe confirmation.

        live_range, when provided, means PRIMARY (historical_data, 15-min) was
        detected stale for `symbol` — same SECONDARY live-tick fallback as
        _enrich()'s 5-min path (see that method's docstring). This feeds a hard
        gate in live_trading_engine.py (15-min EMA must agree with the 5-min
        signal), so leaving it stale would keep silently blocking EMA crossover
        entries even after the 5-min fix.
        """
        is_fallback = live_range is not None
        if is_fallback:
            synthetic_row = pd.DataFrame([{
                "date":  now_ist(),
                "open":  live_range.get("day_open", 0),
                "high":  live_range.get("day_high", 0),
                "low":   live_range.get("day_low", 0),
                "close": live_range.get("close", 0),
            }])
            df = pd.concat([df, synthetic_row], ignore_index=True)

        close = df["close"]
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        return {
            "symbol":     symbol,
            "ema20":      round(ema20, 4),
            "ema50":      round(ema50, 4),
            "tf":         "15m",
            "ltp_source": "live_fallback_today" if is_fallback else "zerodha_historical",
        }
