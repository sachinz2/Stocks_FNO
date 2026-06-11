from fastapi import APIRouter, Depends
from typing import List
from src.api.dto.schemas import StrategyResponse, StrategyActionRequest, StatusResponse

router = APIRouter(prefix="/strategies", tags=["Strategies"])

@router.get("", response_model=List[StrategyResponse])
async def get_strategies():
    return [
        {
            "name": "VWAP_REVERSION",
            "active": True
        }
    ]

@router.post("/activate")
async def activate_strategy(request: StrategyActionRequest):
    return {"status": "activated"}

@router.post("/deactivate")
async def deactivate_strategy(request: StrategyActionRequest):
    return {"status": "deactivated"}
