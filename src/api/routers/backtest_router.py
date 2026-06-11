from fastapi import APIRouter, Depends
from src.api.dto.schemas import BacktestRunRequest, BacktestRunResponse, BacktestResultResponse

router = APIRouter(prefix="/backtest", tags=["Backtest"])

@router.post("/run", response_model=BacktestRunResponse)
async def run_backtest(request: BacktestRunRequest):
    return {"run_id": 12345}

@router.get("/{run_id}", response_model=BacktestResultResponse)
async def get_backtest_result(run_id: int):
    return {
        "status": "COMPLETED",
        "profit_factor": 1.78,
        "drawdown": 8.2,
        "win_rate": 56.1
    }
