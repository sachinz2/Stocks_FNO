from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Integer, Index, text
from src.database.base import Base

class Trade(Base):
    __tablename__ = 'trades'
    __table_args__ = (
        Index('idx_trades_symbol', 'symbol'),
        Index('idx_trades_strategy', 'strategy_name'),
        Index('idx_trades_entry_time', 'entry_time'),
        Index('idx_trades_deleted', 'deleted_at'),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name = Column(String(100))
    symbol = Column(String(30), nullable=False)
    entry_price = Column(Numeric(18, 4))
    exit_price = Column(Numeric(18, 4))
    quantity = Column(Integer, nullable=False)
    pnl = Column(Numeric(18, 4))
    entry_time = Column(TIMESTAMP, nullable=False)
    exit_time = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    deleted_at = Column(TIMESTAMP, nullable=True)
