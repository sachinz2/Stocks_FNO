"""
LTP Poller — fetches prices and computes indicators using yfinance.
No Zerodha market data permission required (Personal plan only covers orders).
Runs every 60 s via APScheduler. Writes enriched tick to Redis for the engine.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.core.constants import REDIS_TICK_PREFIX

logger = logging.getLogger(__name__)

HISTORY_REFRESH_SECONDS = 300  # reload yfinance every 5 min


class LTPPoller:
    """
    Fetches 5-min OHLC from yfinance every 5 minutes, computes indicators,
    and writes enriched tick data to Redis for the trading engine.
    """

    def __init__(self, redis_client, symbols: List[str]) -> None:
        self._redis = redis_client
        self.symbols = symbols
        self._history: Dict[str, pd.DataFrame] = {}
        self._history_loaded_at: Dict[str, datetime] = {}

    async def poll(self) -> None:
        """Called every 60 s by APScheduler."""
        loop = asyncio.get_event_loop()
        for symbol in self.symbols:
            try:
                df = await self._get_history(symbol, loop)
                if df is None or len(df) < 20:
                    logger.warning(f"Not enough history for {symbol}, skipping.")
                    continue

                ltp = float(df["close"].iloc[-1])
                tick = self._enrich(symbol, df, ltp)
                await self._redis.set(f"{REDIS_TICK_PREFIX}{symbol}", json.dumps(tick))
                logger.debug(f"Tick: {symbol} ltp={ltp:.2f} ema20={tick.get('ema20')}")
            except Exception as exc:
                logger.error(f"LTP poll failed for {symbol}: {exc}")

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
        """Blocking — runs in thread executor. Fetches 10 days of 5-min candles."""
        try:
            import yfinance as yf
            df = yf.Ticker(f"{symbol}.NS").history(period="10d", interval="5m")
            if df.empty:
                logger.warning(f"yfinance: no data for {symbol}")
                return None
            df = df.rename(columns={
                "Open": "open", "High": "high",
                "Low": "low", "Close": "close", "Volume": "volume",
            })
            return df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
        except Exception as exc:
            logger.error(f"yfinance fetch failed for {symbol}: {exc}")
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
            "symbol": symbol,
            "close": ltp,
            "ema20": round(ema20, 4),
            "ema50": round(ema50, 4),
            "atr14": round(atr14, 4),
            "vwap": round(vwap, 4),
            "timestamp": datetime.now().isoformat(),
        }
