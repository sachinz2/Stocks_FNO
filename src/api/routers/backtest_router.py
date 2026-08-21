from fastapi import APIRouter, HTTPException, status
from src.api.dto.schemas import BacktestRunRequest

router = APIRouter(prefix="/backtest", tags=["Backtest"])

# Fixed 2026-08-21 (deep review): both endpoints used to return fixed,
# fabricated numbers (run_id always 12345; profit_factor/drawdown/win_rate
# always the same hardcoded values) regardless of what was requested,
# indistinguishable from a real backtest result to any caller -- an
# operator hitting these expecting real output could be misled into acting
# on numbers nothing ever computed. Wiring this to BacktestEngine properly
# needs real historical-data loading by symbol/date-range this router
# doesn't have a simple path to yet -- an honest 501 (this endpoint isn't
# implemented) can't be mistaken for a real result, which is the actual
# defect being fixed here; use POST /analytics/walk-forward or
# /analytics/robustness for real strategy backtesting in the meantime.


@router.post("/run")
async def run_backtest(request: BacktestRunRequest):
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Backtest run is not implemented via this endpoint. "
            "Use POST /api/v1/analytics/walk-forward or /api/v1/analytics/robustness instead."
        ),
    )


@router.get("/{run_id}")
async def get_backtest_result(run_id: int):
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Backtest result retrieval is not implemented via this endpoint. "
            "Use GET /api/v1/analytics/walk-forward-results instead."
        ),
    )
