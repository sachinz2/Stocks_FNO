import pandas as pd
import numpy as np
import logging
from typing import Dict, Any, List
from src.database.repositories.base import BaseRepository
from src.database.models.indicator import Indicator

logger = logging.getLogger(__name__)

class IndicatorEngine:
    def __init__(self, indicator_repo: BaseRepository[Indicator]):
        self.repo = indicator_repo

    def calculate_ema(self, series: pd.Series, period: int) -> pd.Series:
        """Calculate Exponential Moving Average."""
        return series.ewm(span=period, adjust=False).mean()

    def calculate_rsi(self, series: pd.Series, period: int = 14) -> pd.Series:
        """Calculate Relative Strength Index."""
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def calculate_atr(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """Calculate Average True Range."""
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()

    def calculate_vwap(self, high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
        """Calculate Volume Weighted Average Price."""
        typical_price = (high + low + close) / 3
        return (typical_price * volume).cumsum() / volume.cumsum()

    def calculate_macd(self, series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
        """Calculate MACD and Signal Line."""
        ema_fast = self.calculate_ema(series, fast)
        ema_slow = self.calculate_ema(series, slow)
        macd_line = ema_fast - ema_slow
        signal_line = self.calculate_ema(macd_line, signal)
        return pd.DataFrame({
            "macd": macd_line,
            "macd_signal": signal_line
        })

    async def update_indicators(self, symbol: str, df: pd.DataFrame):
        """
        Calculate and persist all indicators for a given dataframe of OHLC data.
        Assumes dataframe has columns: timestamp, open, high, low, close, volume
        """
        try:
            df = df.copy()
            df['ema20'] = self.calculate_ema(df['close'], 20)
            df['ema50'] = self.calculate_ema(df['close'], 50)
            df['ema200'] = self.calculate_ema(df['close'], 200)
            df['rsi14'] = self.calculate_rsi(df['close'], 14)
            df['atr14'] = self.calculate_atr(df['high'], df['low'], df['close'], 14)
            df['vwap'] = self.calculate_vwap(df['high'], df['low'], df['close'], df['volume'])
            
            macd_df = self.calculate_macd(df['close'])
            df['macd'] = macd_df['macd']
            df['macd_signal'] = macd_df['macd_signal']
            
            # Get the latest row to persist incrementally
            latest = df.iloc[-1]
            
            # Database persistence
            obj_in = {
                "symbol": symbol,
                "timestamp": latest['timestamp'],
                "ema20": float(latest['ema20']) if not pd.isna(latest['ema20']) else None,
                "ema50": float(latest['ema50']) if not pd.isna(latest['ema50']) else None,
                "ema200": float(latest['ema200']) if not pd.isna(latest['ema200']) else None,
                "rsi14": float(latest['rsi14']) if not pd.isna(latest['rsi14']) else None,
                "atr14": float(latest['atr14']) if not pd.isna(latest['atr14']) else None,
                "vwap": float(latest['vwap']) if not pd.isna(latest['vwap']) else None,
                "macd": float(latest['macd']) if not pd.isna(latest['macd']) else None,
                "macd_signal": float(latest['macd_signal']) if not pd.isna(latest['macd_signal']) else None
            }
            
            await self.repo.create(obj_in)
            logger.info(f"Indicators updated and persisted for {symbol} at {latest['timestamp']}")
            
        except Exception as e:
            logger.error(f"Failed to update indicators for {symbol}: {e}")
            raise