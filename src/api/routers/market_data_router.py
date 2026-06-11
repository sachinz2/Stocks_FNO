from fastapi import APIRouter, Depends
from typing import List
from src.api.dto.schemas import MarketDataResponse, MarketDataLoadRequest, StatusResponse

router = APIRouter(prefix="/market-data", tags=["Market Data"])

@router.get("/{symbol}", response_model=List[MarketDataResponse])
async def get_market_data(symbol: str, timeframe: str = "5m", from_date: str = None, to_date: str = None):
    return [
        {
            "timestamp": "2026-06-01T09:15:00",
            "open": 810.5,
            "high": 812.0,
            "low": 809.8,
            "close": 811.4,
            "volume": 15000
        }
    ]

@router.post("/load", response_model=StatusResponse)
async def load_historical_data(request: MarketDataLoadRequest):
    return {"status": "accepted"}
