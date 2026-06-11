from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Index
from src.database.base import Base

class Indicator(Base):
    __tablename__ = 'indicators'
    __table_args__ = (
        Index('idx_indicators_symbol_time', 'symbol', 'timestamp'),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False)
    timestamp = Column(TIMESTAMP, nullable=False)
    ema20 = Column(Numeric(18, 4))
    ema50 = Column(Numeric(18, 4))
    ema200 = Column(Numeric(18, 4))
    rsi14 = Column(Numeric(18, 4))
    atr14 = Column(Numeric(18, 4))
    vwap = Column(Numeric(18, 4))
    macd = Column(Numeric(18, 4))
    macd_signal = Column(Numeric(18, 4))
