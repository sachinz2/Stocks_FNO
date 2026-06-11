import pytest
from unittest.mock import AsyncMock, MagicMock
from src.database.repositories.base import BaseRepository
from src.database.models.stock import Stock

@pytest.fixture
def mock_session():
    return AsyncMock()

@pytest.fixture
def repo(mock_session):
    return BaseRepository(Stock, mock_session)

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
async def test_delete(repo, mock_session):
    mock_result = MagicMock()
    mock_result.rowcount = 1
    mock_session.execute.return_value = mock_result
    
    result = await repo.delete(1)
    
    assert result is True
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()
