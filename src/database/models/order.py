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
    # Actually-filled quantity, as reported by the broker -- distinct from
    # `quantity` (the requested amount). NULL until a fill is confirmed.
    # Added 2026-08-21 (deep review): a resting LIMIT order can be partially
    # filled while still status=OPEN at Zerodha; without this, a stale-order
    # retry had no way to know how much was already filled and resubmitted
    # the full original quantity every time.
    filled_quantity = Column(Integer, nullable=True)
    price = Column(Numeric(18, 4))            # expected price (engine estimate)
    fill_price = Column(Numeric(18, 4), nullable=True)  # actual fill after bid-ask slippage
    slippage = Column(Numeric(18, 4), nullable=True)    # fill_price - price (per unit)
    order_status = Column(String(50)) # OPEN, COMPLETED, CANCELLED, REJECTED
    created_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    deleted_at = Column(TIMESTAMP, nullable=True)
