"""
LTP Poller — fetches prices and computes indicators using yfinance.
No Zerodha market data permission required (Personal plan only covers orders).

Runs every 60 s via APScheduler:
  1. Polls all 40 F&O symbols via yfinance (5-min OHLC, 10-day window)
  2. Computes EMA20, EMA50, ATR14, VWAP for each symbol
  3. Writes enriched tick to Redis (tick:SYMBOL)
  4. Scores all 40 symbols THREE WAYS — one per strategy regime:
       - EMA Crossover pool  (nfo:top5)         : high ATR% + strong EMA trend
       - Credit Spread pool  (nfo:top5:spread)   : low ATR% (<1.2%) + EMA directional
       - Iron Condor pool    (nfo:top5:condor)   : low ATR% (<1.2%) + EMA flat (<0.1%)
  5. Trading engine reads the right pool for each strategy so the correct 5 stocks
     are always fed to the right strategy on any given day.
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
    FNO_SYMBOLS,
    REDIS_TICK_PREFIX,
    REDIS_TOP_SYMBOLS_KEY,
    REDIS_TOP_SYMBOLS_CREDIT_SPREAD,
    REDIS_TOP_SYMBOLS_IRON_CONDOR,
)

logger = logging.getLogger(__name__)

HISTORY_REFRESH_SECONDS = 300  # reload yfinance every 5 min

# ATR% thresholds that must match strategy parameters
_LOW_VOL_THRESHOLD = 1.2   # below = low volatility regime
_FLAT_EMA_THRESHOLD = 0.1  # EMA spread below = EMAs are flat (no direction)


class LTPPoller:
    """
    Fetches 5-min OHLC from yfinance, computes indicators, writes to Redis.
    Scores all 40 symbols for three distinct trading regimes and publishes
    three separate top-N ranked lists so each strategy gets appropriate stocks.
    """

    def __init__(self, redis_client, symbols: List[str] = None) -> None:
        self._redis = redis_client
        self.symbols = symbols or FNO_SYMBOLS  # default: all 40
        self._history: Dict[str, pd.DataFrame] = {}
        self._history_loaded_at: Dict[str, datetime] = {}

    async def poll(self) -> None:
        """Called every 60 s by APScheduler."""
        loop = asyncio.get_event_loop()

        ema_scores: Dict[str, float] = {}
        spread_scores: Dict[str, float] = {}
        condor_scores: Dict[str, float] = {}

        for symbol in self.symbols:
            try:
                df = await self._get_history(symbol, loop)
                if df is None or len(df) < 50:
                    logger.warning(f"Not enough history for {symbol} (need 50 bars), skipping.")
                    continue

                ltp = float(df["close"].iloc[-1])
                tick = self._enrich(symbol, df, ltp)
                await self._redis.set(f"{REDIS_TICK_PREFIX}{symbol}", json.dumps(tick))

                e, s, c = self._score_all(tick)
                ema_scores[symbol] = e
                if s > 0:
                    spread_scores[symbol] = s
                if c > 0:
                    condor_scores[symbol] = c

                logger.debug(
                    f"Tick: {symbol} ltp={ltp:.2f} "
                    f"ema_score={e:.3f} spread_score={s:.3f} condor_score={c:.3f}"
                )
            except Exception as exc:
                logger.error(f"LTP poll failed for {symbol}: {exc}")

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

    async def _get_history(self, symbol: str, loop) -> Optional[pd.DataFrame]:
        """Return yfinance OHLC, refreshing cache every 5 minutes."""
        now = datetime.now()
        last = self._history_loaded_at.get(symbol)
        stale = last is None or (now - last).total_seconds() > HISTORY_REFRESH_SECONDS

        if stale:
            df = await loop.run_in_executor(None, self._fetch_yfinance, symbol)
            if df is not None and not df.empty:
                self._history[symbol] = df
                self._history_loaded_at[symbol] = now

        return self._history.get(symbol)

    @staticmethod
    def _fetch_yfinance(symbol: str) -> Optional[pd.DataFrame]:
        """Blocking — runs in thread executor. Fetches 10 days of 5-min candles.
        Tries NSE (.NS) first, falls back to BSE (.BO) if NSE returns no data."""
        import yfinance as yf
        for suffix in (".NS", ".BO"):
            try:
                df = yf.Ticker(f"{symbol}{suffix}").history(period="10d", interval="5m")
                if not df.empty:
                    df = df.rename(columns={
                        "Open": "open", "High": "high",
                        "Low": "low", "Close": "close", "Volume": "volume",
                    })
                    return df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
            except Exception:
                pass
        logger.warning(f"yfinance: no data for {symbol} (tried .NS and .BO)")
        return None

    @staticmethod
    def _enrich(symbol: str, df: pd.DataFrame, ltp: float) -> dict:
        """Compute EMA20, EMA50, ATR14, VWAP from OHLC dataframe."""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr14 = float(tr.rolling(14).mean().iloc[-1])

        typical = (high + low + close) / 3
        vol_nonzero = volume.replace(0, np.nan)
        cum_vol = vol_nonzero.sum()
        vwap = float((typical * vol_nonzero).sum() / cum_vol) if cum_vol > 0 else ltp

        return {
            "symbol":     symbol,
            "close":      ltp,
            "ema20":      round(ema20, 4),
            "ema50":      round(ema50, 4),
            "atr14":      round(atr14, 4),
            "vwap":       round(vwap, 4),
            "timestamp":  datetime.now().isoformat(),
            "ltp_source": "yfinance",
        }

    @staticmethod
    def _score_all(tick: dict) -> tuple:
        """
        Score a symbol for all three strategy regimes.
        Returns (ema_score, spread_score, condor_score).

        EMA Crossover score (high = volatile + trending):
          ATR% × 0.6 + EMA_spread% × 0.4
          → rewards momentum stocks making big directional moves

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
        ema_spread_pct = abs(ema20 - ema50) / close * 100

        # Regime 1: EMA crossover — always gets a score
        ema_score = round(atr_pct * 0.6 + ema_spread_pct * 0.4, 4)

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
