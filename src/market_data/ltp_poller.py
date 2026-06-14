"""
LTP Poller — fetches current prices from Zerodha REST API every minute.
Uses yfinance for historical OHLC to compute indicators without WebSocket.
Compatible with Zerodha Personal (free) API plan.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.core.config import settings
from src.core.constants import REDIS_TICK_PREFIX

logger = logging.getLogger(__name__)

REDIS_KEY_TOKEN = "zerodha:access_token"
HISTORY_REFRESH_SECONDS = 1800  # reload yfinance every 30 min


class LTPPoller:
    """
    Polls Zerodha LTP every minute and writes indicator-enriched ticks to Redis.
    The trading engine reads from the same Redis keys (tick:<SYMBOL>).
    """

    def __init__(self, redis_client, symbols: List[str]) -> None:
        self._redis = redis_client
        self.symbols = symbols
        self._history: Dict[str, pd.DataFrame] = {}
        self._history_loaded_at: Dict[str, datetime] = {}

    async def poll(self) -> None:
        """Called every 60 s by APScheduler. Fetches LTP and updates Redis."""
        from kiteconnect import KiteConnect

        token = await self._redis.get(REDIS_KEY_TOKEN)
        if not token:
            logger.warning("LTP poll skipped — no Zerodha access token in Redis.")
            return

        kite = KiteConnect(api_key=settings.ZERODHA_API_KEY)
        kite.set_access_token(token)

        instruments = [f"NSE:{s}" for s in self.symbols]
        try:
            ltp_data = kite.ltp(instruments)
        except Exception as exc:
            logger.error(f"kite.ltp() failed: {exc}")
            return

        for instrument, data in ltp_data.items():
            symbol = instrument.split(":")[1]
            ltp = float(data.get("last_price", 0))
            if ltp <= 0:
                continue

            df = await self._get_history(symbol, ltp)
            if df is not None and len(df) >= 20:
                tick = self._enrich(symbol, df, ltp)
            else:
                tick = {
                    "symbol": symbol, "close": ltp,
                    "ema20": None, "ema50": None, "atr14": None, "vwap": None,
                    "timestamp": datetime.now().isoformat(),
                }

            await self._redis.set(f"{REDIS_TICK_PREFIX}{symbol}", json.dumps(tick))
            logger.debug(f"Tick: {symbol} ltp={ltp} ema20={tick.get('ema20')}")

    async def _get_history(self, symbol: str, ltp: float) -> Optional[pd.DataFrame]:
        """Return cached yfinance history with current LTP appended as the latest row."""
        now = datetime.now()
        last = self._history_loaded_at.get(symbol)
        stale = last is None or (now - last).total_seconds() > HISTORY_REFRESH_SECONDS

        if stale:
            loop = asyncio.get_event_loop()
            df = await loop.run_in_executor(None, self._fetch_yfinance, symbol)
            if df is not None and not df.empty:
                self._history[symbol] = df
                self._history_loaded_at[symbol] = now

        df = self._history.get(symbol)
        if df is None or df.empty:
            return None

        df = df.copy()
        new_row = pd.DataFrame([{"open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": 1}])
        df = pd.concat([df, new_row], ignore_index=True)
        return df.tail(210)

    @staticmethod
    def _fetch_yfinance(symbol: str) -> Optional[pd.DataFrame]:
        """Blocking — called from a thread executor."""
        try:
            import yfinance as yf
            ticker = yf.Ticker(f"{symbol}.NS")
            df = ticker.history(period="10d", interval="5m")
            if df.empty:
                logger.warning(f"yfinance returned empty data for {symbol}")
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
