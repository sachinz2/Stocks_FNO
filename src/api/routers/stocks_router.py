from fastapi import APIRouter, Depends
from typing import List
from src.api.dto.schemas import StockResponse, StockDetailResponse

router = APIRouter(prefix="/stocks", tags=["Stocks"])

@router.get("", response_model=List[StockResponse])
async def get_stocks():
    """Returns all active F&O stocks."""
    return [
        {"symbol": "SBIN", "sector": "BANKING", "lot_size": 750}
    ]

@router.get("/{symbol}", response_model=StockDetailResponse)
async def get_stock(symbol: str):
    """Returns details for a specific stock."""
    return {
        "symbol": symbol,
        "name": "State Bank of India",
        "lot_size": 750,
        "active": True
    }
