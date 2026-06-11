from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Index, text
from src.database.base import Base

class Signal(Base):
    __tablename__ = 'signals'
    __table_args__ = (
        Index('idx_signals_deleted', 'deleted_at'),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name = Column(String(100), nullable=False)
    symbol = Column(String(30), nullable=False)
    signal_type = Column(String(20), nullable=False) # BUY, SELL, EXIT, HOLD
    confidence = Column(Numeric(6, 4))
    generated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    status = Column(String(20)) # PENDING, PROCESSED, REJECTED
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    deleted_at = Column(TIMESTAMP, nullable=True)
