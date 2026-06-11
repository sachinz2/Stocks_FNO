from sqlalchemy import Column, BigInteger, String, Date, Numeric, Integer, ForeignKey, TIMESTAMP, Index, text
from sqlalchemy.orm import relationship
from src.database.base import Base

class Instrument(Base):
    __tablename__ = 'instruments'
    __table_args__ = (
        Index('idx_instruments_deleted', 'deleted_at'),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    stock_id = Column(BigInteger, ForeignKey('stocks.id', ondelete='RESTRICT'))
    instrument_token = Column(BigInteger, index=True)
    expiry_date = Column(Date)
    lot_size = Column(Integer)
    instrument_type = Column(String(20))
    strike = Column(Numeric(18, 4))
    option_type = Column(String(10))
    created_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    deleted_at = Column(TIMESTAMP, nullable=True)

    stock = relationship("Stock", backref="instruments")
