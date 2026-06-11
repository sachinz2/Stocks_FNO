from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.database.connection import get_db_session
from src.database.repositories.base import BaseRepository
from src.database.models.stock import Stock
from src.api.services.auth import verify_token

def get_stock_repository(session: AsyncSession = Depends(get_db_session)) -> BaseRepository[Stock]:
    return BaseRepository(Stock, session)

# Generic dependency for currently authenticated user
def get_current_user(payload: dict = Depends(verify_token)):
    return payload.get("sub")
