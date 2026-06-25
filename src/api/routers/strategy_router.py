from fastapi import APIRouter, HTTPException
from typing import List, Optional
from pydantic import BaseModel

from src.strategies.base import StrategyRegistry

router = APIRouter(prefix="/strategies", tags=["Strategies"])


class StrategyDetail(BaseModel):
    id: str
    name: str
    is_active: bool
    paused_reason: Optional[str] = None


class StrategyActionRequest(BaseModel):
    strategy_id: str


@router.get("", response_model=List[StrategyDetail])
async def get_strategies():
    """Return all registered strategy instances with live active/paused status."""
    instances = StrategyRegistry.get_active_strategies()
    return [
        StrategyDetail(
            id=sid,
            name=inst.name,
            is_active=inst.is_active,
            paused_reason=getattr(inst, "paused_reason", None),
        )
        for sid, inst in instances.items()
    ]


@router.post("/activate")
async def activate_strategy(request: StrategyActionRequest):
    ok = StrategyRegistry.resume_strategy(request.strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Strategy '{request.strategy_id}' not found.")
    return {"status": "activated", "strategy_id": request.strategy_id}


@router.post("/deactivate")
async def deactivate_strategy(request: StrategyActionRequest):
    ok = StrategyRegistry.pause_strategy(request.strategy_id, reason="Manual pause via dashboard")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Strategy '{request.strategy_id}' not found.")
    return {"status": "deactivated", "strategy_id": request.strategy_id}
