# Fixed 2026-09-15 (deep review): both endpoints used to return fixed,
# fabricated data regardless of what was requested -- GET /stocks always
# returned a single hardcoded SBIN row; GET /stocks/{symbol} returned
# "State Bank of India" for ANY symbol requested, e.g. GET /stocks/RELIANCE
# still came back "State Bank of India" with a 200. Indistinguishable from
# a real response to any caller -- an operator checking "what does the
# system know about symbol X" via Swagger/direct API call would be silently
# misled by static data with no indication it's fake. Same defect class
# already fixed this session in risk_router.py/backtest_router.py (fake 200
# -> honest 501); the real active-F&O-universe list lives in Redis via
# fno_universe.py's weekly recompute, not a queryable DB table this router
# has a simple path to yet.
from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/stocks", tags=["Stocks"])


@router.get("")
async def get_stocks():
    """Returns all active F&O stocks."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Stock listing via this endpoint is not implemented — it previously returned fabricated data.",
    )


@router.get("/{symbol}")
async def get_stock(symbol: str):
    """Returns details for a specific stock."""
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Stock detail lookup via this endpoint is not implemented — it previously returned fabricated data.",
    )
