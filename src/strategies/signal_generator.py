import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from src.database.models.signal import Signal
from src.database.models.indicator import Indicator
from src.database.models.ohlc import OHLCData
from src.database.repositories.base import BaseRepository

logger = logging.getLogger(__name__)

class SignalGenerator:
    """
    Generates trading signals based on technical indicators and strategy rules.
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.signal_repo = BaseRepository(Signal, session)
        self.indicator_repo = BaseRepository(Indicator, session)
        self.ohlc_repo = BaseRepository(OHLCData, session)
    
    async def generate_ema_crossover_signals(self, strategy_name: str, symbols: List[str]) -> int:
        """
        EMA Crossover Strategy:
        - BUY: EMA20 crosses above EMA50
        - SELL: EMA20 crosses below EMA50
        """
        generated = 0
        
        for symbol in symbols:
            try:
                # Get latest indicators for the symbol
                indicators = await self.indicator_repo.filter(symbol=symbol)
                if not indicators:
                    logger.warning(f"No indicators found for {symbol}")
                    continue
                
                latest_indicator = indicators[-1]  # Most recent
                
                # EMA Crossover logic
                ema20 = float(latest_indicator.ema20) if latest_indicator.ema20 else None
                ema50 = float(latest_indicator.ema50) if latest_indicator.ema50 else None
                
                if not ema20 or not ema50:
                    logger.warning(f"Missing EMA values for {symbol}")
                    continue
                
                signal_type = None
                confidence = 0.0
                
                if ema20 > ema50:
                    signal_type = "BUY"
                    # Confidence based on EMA spread
                    spread_pct = ((ema20 - ema50) / ema50) * 100
                    confidence = min(0.95, 0.5 + (spread_pct * 0.01))
                elif ema20 < ema50:
                    signal_type = "SELL"
                    spread_pct = ((ema50 - ema20) / ema50) * 100
                    confidence = min(0.95, 0.5 + (spread_pct * 0.01))
                else:
                    signal_type = "HOLD"
                    confidence = 0.5
                
                # Create signal
                signal_data = {
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "signal_type": signal_type,
                    "confidence": confidence,
                    "generated_at": datetime.utcnow(),
                    "status": "PENDING"
                }
                
                await self.signal_repo.create(signal_data)
                generated += 1
                logger.info(f"Generated {signal_type} signal for {symbol} with confidence {confidence:.2f}")
                
            except Exception as e:
                logger.error(f"Error generating signal for {symbol}: {e}")
                continue
        
        return generated
    
    async def generate_rsi_signals(self, strategy_name: str, symbols: List[str]) -> int:
        """
        RSI Strategy:
        - BUY: RSI < 30 (oversold)
        - SELL: RSI > 70 (overbought)
        """
        generated = 0
        
        for symbol in symbols:
            try:
                indicators = await self.indicator_repo.filter(symbol=symbol)
                if not indicators:
                    continue
                
                latest_indicator = indicators[-1]
                rsi14 = float(latest_indicator.rsi14) if latest_indicator.rsi14 else None
                
                if not rsi14:
                    continue
                
                signal_type = None
                confidence = 0.0
                
                if rsi14 < 30:
                    signal_type = "BUY"
                    confidence = min(0.90, 0.5 + (30 - rsi14) * 0.02)
                elif rsi14 > 70:
                    signal_type = "SELL"
                    confidence = min(0.90, 0.5 + (rsi14 - 70) * 0.02)
                else:
                    signal_type = "HOLD"
                    confidence = 0.5
                
                signal_data = {
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "signal_type": signal_type,
                    "confidence": confidence,
                    "generated_at": datetime.utcnow(),
                    "status": "PENDING"
                }
                
                await self.signal_repo.create(signal_data)
                generated += 1
                
            except Exception as e:
                logger.error(f"Error generating RSI signal for {symbol}: {e}")
                continue
        
        return generated
    
    async def generate_atr_volatility_signals(self, strategy_name: str, symbols: List[str]) -> int:
        """
        ATR Volatility Strategy:
        - BUY: High volatility (ATR > 20-period average) + uptrend
        - SELL: High volatility + downtrend
        """
        generated = 0
        
        for symbol in symbols:
            try:
                indicators = await self.indicator_repo.filter(symbol=symbol)
                if not indicators or len(indicators) < 20:
                    continue
                
                latest_indicator = indicators[-1]
                atr14 = float(latest_indicator.atr14) if latest_indicator.atr14 else None
                
                if not atr14:
                    continue
                
                # Calculate average ATR
                atr_values = [float(ind.atr14) if ind.atr14 else 0 for ind in indicators[-20:]]
                avg_atr = sum(atr_values) / len(atr_values)
                
                # Check trend from OHLC
                ohlc_data = await self.ohlc_repo.filter(symbol=symbol)
                if not ohlc_data:
                    continue
                
                latest_ohlc = ohlc_data[-1]
                close = float(latest_ohlc.close)
                open_price = float(latest_ohlc.open)
                
                signal_type = None
                confidence = 0.0
                
                if atr14 > avg_atr:
                    if close > open_price:
                        signal_type = "BUY"
                    else:
                        signal_type = "SELL"
                    volatility_ratio = atr14 / avg_atr
                    confidence = min(0.85, 0.5 + (volatility_ratio - 1) * 0.1)
                else:
                    signal_type = "HOLD"
                    confidence = 0.5
                
                signal_data = {
                    "strategy_name": strategy_name,
                    "symbol": symbol,
                    "signal_type": signal_type,
                    "confidence": confidence,
                    "generated_at": datetime.utcnow(),
                    "status": "PENDING"
                }
                
                await self.signal_repo.create(signal_data)
                generated += 1
                
            except Exception as e:
                logger.error(f"Error generating ATR signal for {symbol}: {e}")
                continue
        
        return generated
