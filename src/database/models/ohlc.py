from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Index
from src.database.base import Base

class OHLCData(Base):
    """
    CRITICAL TABLE: OHLC Data (Partitioned by Range in DDL)
    """
    __tablename__ = 'ohlc_data'
    __table_args__ = (
        Index('idx_ohlc_symbol_time', 'symbol', 'candle_timestamp'),
        Index('idx_ohlc_time', 'candle_timestamp')
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False)
    timeframe = Column(String(10))
    candle_timestamp = Column(TIMESTAMP, nullable=False)
    open = Column(Numeric(18, 4))
    high = Column(Numeric(18, 4))
    low = Column(Numeric(18, 4))
    close = Column(Numeric(18, 4))
    volume = Column(BigInteger)
    open_interest = Column(BigInteger)
