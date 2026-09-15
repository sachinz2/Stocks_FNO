# Fixed 2026-09-15 (deep review): both endpoints used to return fixed,
# fabricated data -- GET /market-data/{symbol} always returned one
# hardcoded bar dated 2026-06-01T09:15:00 regardless of symbol/timeframe/
# date-range; POST /market-data/load was a no-op that unconditionally
# returned {"status": "accepted"} without loading anything. Indistinguishable
# from a real response to any caller. Same defect class already fixed this
# session in risk_router.py/backtest_router.py/stocks_router.py (fake 200 ->
# honest 501); real OHLC data lives in the `indicators`/`ohlc` tables and
# Redis via the live poller/ticker, not a path this stub router ever read.
from fastapi import APIRouter, HTTPException, status
from src.api.dto.schemas import MarketDataLoadRequest

router = APIRouter(prefix="/market-data", tags=["Market Data"])


@router.get("/{symbol}")
async def get_market_data(symbol: str, timeframe: str = "5m", from_date: str = None, to_date: str = None):
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Historical market-data retrieval via this endpoint is not implemented — it previously returned fabricated data.",
    )


@router.post("/load")
async def load_historical_data(request: MarketDataLoadRequest):
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Historical market-data load via this endpoint is not implemented — it previously silently no-opped.",
    )
