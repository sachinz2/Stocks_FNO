from typing import Generic, TypeVar, Type, List, Optional, Any, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update as sa_update
from src.database.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """
    Each method opens its own session and closes it when done.
    This avoids shared-session corruption across concurrent async operations.
    """

    def __init__(self, model: Type[ModelType], session_factory):
        self.model = model
        self._factory = session_factory

    async def create(self, obj_in: Dict[str, Any]) -> ModelType:
        async with self._factory() as session:
            db_obj = self.model(**obj_in)
            session.add(db_obj)
            await session.commit()
            await session.refresh(db_obj)
            return db_obj

    async def update(self, db_obj: ModelType, obj_in: Dict[str, Any]) -> ModelType:
        # Writes only the given fields via a targeted UPDATE, never via
        # session.merge(db_obj) -- merge() pushes EVERY loaded attribute of
        # db_obj onto the row, so if db_obj was fetched earlier and some
        # other column changed at the DB since (a concurrent writer in a
        # different session -- routine in this codebase, e.g. order sync
        # and order expiry racing on the same "OPEN" orders), merge()
        # would silently revert that column back to its stale value.
        async with self._factory() as session:
            await session.execute(
                sa_update(self.model)
                .where(self.model.id == db_obj.id)
                .values(**obj_in)
            )
            await session.commit()
            result = await session.execute(
                select(self.model).where(self.model.id == db_obj.id)
            )
            return result.scalars().first()

    async def delete(self, id: int) -> bool:
        async with self._factory() as session:
            result = await session.execute(
                delete(self.model).where(self.model.id == id)
            )
            await session.commit()
            return result.rowcount > 0

    async def get_by_id(self, id: int) -> Optional[ModelType]:
        async with self._factory() as session:
            result = await session.execute(
                select(self.model).where(self.model.id == id)
            )
            return result.scalars().first()

    async def get_all(self) -> List[ModelType]:
        async with self._factory() as session:
            result = await session.execute(select(self.model))
            return result.scalars().all()

    async def filter(self, limit: Optional[int] = None, order_by: Optional[str] = None, **kwargs) -> List[ModelType]:
        async with self._factory() as session:
            stmt = select(self.model).filter_by(**kwargs)
            if order_by:
                col_name, _, direction = order_by.partition(" ")
                col = getattr(self.model, col_name, None)
                if col is not None:
                    from sqlalchemy import desc, asc
                    stmt = stmt.order_by(desc(col) if direction.upper() == "DESC" else asc(col))
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            return result.scalars().all()

    async def paginate(self, skip: int = 0, limit: int = 100) -> List[ModelType]:
        async with self._factory() as session:
            result = await session.execute(
                select(self.model).offset(skip).limit(limit)
            )
            return result.scalars().all()
