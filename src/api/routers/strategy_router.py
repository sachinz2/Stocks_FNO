from fastapi import APIRouter, Depends, HTTPException, Request
from typing import List, Optional
from pydantic import BaseModel

from src.api.services.auth import require_admin_token
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


@router.post("/activate", dependencies=[Depends(require_admin_token)])
async def activate_strategy(body: StrategyActionRequest, request: Request):
    ok = StrategyRegistry.resume_strategy(body.strategy_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Strategy '{body.strategy_id}' not found.")
    # Fixed 2026-08-27 (trade review follow-up): a manual resume through
    # this endpoint used to be immediately undone by the very next
    # evaluate_all() cycle, which re-reads the same rolling window that
    # justified the original pause -- the strategy could never close a
    # single new trade to start displacing it. mark_resumed() scopes the
    # next auto-pause evaluation to trades closed after this point instead.
    engine = getattr(request.app.state, "trading_engine", None)
    monitor = getattr(engine, "strategy_monitor", None) if engine else None
    if monitor:
        monitor.mark_resumed(body.strategy_id)
    return {"status": "activated", "strategy_id": body.strategy_id}


@router.post("/deactivate", dependencies=[Depends(require_admin_token)])
async def deactivate_strategy(request: StrategyActionRequest):
    ok = StrategyRegistry.pause_strategy(request.strategy_id, reason="Manual pause via dashboard", source="manual")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Strategy '{request.strategy_id}' not found.")
    return {"status": "deactivated", "strategy_id": request.strategy_id}
