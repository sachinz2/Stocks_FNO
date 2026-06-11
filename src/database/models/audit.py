from sqlalchemy import Column, BigInteger, String, TIMESTAMP, JSON
from src.database.base import Base

class AuditLog(Base):
    __tablename__ = 'audit_logs'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    service_name = Column(String(100), nullable=False)
    action = Column(String(100), nullable=False)
    payload = Column(JSON)
    timestamp = Column(TIMESTAMP, nullable=False)
