import pytest
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.sql.dml import Update
from src.database.repositories.base import BaseRepository
from src.database.models.stock import Stock
from src.database.models.order import Order


@pytest.fixture
def mock_session():
    return AsyncMock()


@pytest.fixture
def mock_session_factory(mock_session):
    # Fixed 2026-08-07: BaseRepository calls `async with self._factory() as
    # session:` -- the factory itself must be a plain callable that RETURNS
    # an async context manager, not the session object directly. Passing
    # mock_session straight in (as this fixture used to) made
    # self._factory() return another AsyncMock with no real __aenter__/
    # __aexit__ wiring, raising "'coroutine' object does not support the
    # asynchronous context manager protocol" on every repo method. This
    # mirrors the real AsyncSessionLocal shape (a sessionmaker callable).
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.fixture
def repo(mock_session_factory):
    return BaseRepository(Stock, mock_session_factory)


@pytest.mark.asyncio
async def test_create(repo, mock_session):
    mock_session.add = MagicMock()

    obj_in = {"symbol": "RELIANCE", "company_name": "Reliance Ind"}
    result = await repo.create(obj_in)

    assert result.symbol == "RELIANCE"
    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()
    mock_session.refresh.assert_called_once()


@pytest.mark.asyncio
async def test_get_by_id(repo, mock_session):
    mock_result = MagicMock()
    mock_stock = Stock(id=1, symbol="RELIANCE")
    mock_result.scalars().first.return_value = mock_stock
    mock_session.execute.return_value = mock_result

    result = await repo.get_by_id(1)

    assert result is not None
    assert result.id == 1
    assert result.symbol == "RELIANCE"
    mock_session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_update_issues_targeted_update_not_full_object_merge(mock_session_factory, mock_session):
    # Fixed 2026-09-15 (deep review): update() used to do
    # `session.merge(db_obj)` then set only the obj_in fields on top. merge()
    # pushes EVERY loaded attribute of the passed-in db_obj onto the row --
    # so if db_obj was fetched earlier by the caller and some other column
    # (e.g. filled_quantity/fill_price written by a concurrent sync job) was
    # updated at the DB since, merge() would silently stomp it back to the
    # stale value. update() must now issue a targeted UPDATE ... SET
    # touching only the given fields, and never call session.merge at all.
    repo = BaseRepository(Order, mock_session_factory)
    mock_result = MagicMock()
    fresh_row = Order(id=1, symbol="RELIANCE", order_status="COMPLETED", filled_quantity=10)
    mock_result.scalars().first.return_value = fresh_row
    mock_session.execute.return_value = mock_result
    mock_session.merge = AsyncMock(side_effect=AssertionError("must not call session.merge"))

    # Simulates a stale, earlier-fetched db_obj: some other column
    # (filled_quantity) may since have changed in the DB via a concurrent
    # writer, but this caller only intends to touch order_status.
    stale_db_obj = Order(id=1, symbol="RELIANCE", order_status="OPEN", filled_quantity=0)

    result = await repo.update(stale_db_obj, {"order_status": "EXPIRED"})

    assert mock_session.merge.await_count == 0
    update_calls = [
        c for c in mock_session.execute.call_args_list
        if isinstance(c.args[0], Update)
    ]
    assert len(update_calls) == 1
    stmt = update_calls[0].args[0]
    compiled = stmt.compile()
    # The SET clause must carry only the explicitly-passed field -- the
    # stale filled_quantity=0 on stale_db_obj must never be written.
    assert "filled_quantity" not in compiled.params
    assert compiled.params["order_status"] == "EXPIRED"
    assert result is fresh_row


@pytest.mark.asyncio
async def test_delete(repo, mock_session):
    mock_result = MagicMock()
    mock_result.rowcount = 1
    mock_session.execute.return_value = mock_result

    result = await repo.delete(1)

    assert result is True
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()
