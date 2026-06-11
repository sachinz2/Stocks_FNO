from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Integer, Index, text
from src.database.base import Base

class Order(Base):
    __tablename__ = 'orders'
    __table_args__ = (
        Index('idx_orders_broker_id', 'broker_order_id'),
        Index('idx_orders_status', 'order_status'),
        Index('idx_orders_deleted', 'deleted_at'),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    broker_order_id = Column(String(100))
    symbol = Column(String(30), nullable=False)
    side = Column(String(10), nullable=False) # BUY, SELL
    quantity = Column(Integer, nullable=False)
    price = Column(Numeric(18, 4))
    order_status = Column(String(50)) # OPEN, COMPLETED, CANCELLED, REJECTED
    created_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    deleted_at = Column(TIMESTAMP, nullable=True)
