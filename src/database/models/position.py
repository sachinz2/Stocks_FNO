from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Integer, Index, text
from src.database.base import Base

class Position(Base):
    __tablename__ = 'positions'
    __table_args__ = (
        Index('idx_positions_deleted', 'deleted_at'),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    avg_price = Column(Numeric(18, 4))
    market_price = Column(Numeric(18, 4))
    unrealized_pnl = Column(Numeric(18, 4))
    realized_pnl = Column(Numeric(18, 4))
    created_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    deleted_at = Column(TIMESTAMP, nullable=True)
