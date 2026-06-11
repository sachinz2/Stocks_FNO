from sqlalchemy import Column, BigInteger, String, Boolean, TIMESTAMP, text, Index
from src.database.base import Base

class Stock(Base):
    __tablename__ = 'stocks'
    __table_args__ = (
        Index('idx_stocks_deleted', 'deleted_at'),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(30), unique=True, nullable=False, index=True)
    company_name = Column(String(200))
    sector = Column(String(100))
    exchange = Column(String(20))
    fno_enabled = Column(Boolean, server_default=text('false'))
    active = Column(Boolean, server_default=text('true'))
    created_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    deleted_at = Column(TIMESTAMP, nullable=True)
