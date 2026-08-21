"""
Risk rules API.

Fixed 2026-08-21 (deep review): every endpoint here used to be a stub --
GET always returned [], POST/PUT/DELETE did nothing and persisted nothing,
silently pretending to succeed. If any operator or future dashboard work
assumed editing risk rules through this API had effect, it silently
didn't -- a false sense of control over real risk limits. GET now reports
RiskManager's actual live limits (read-only); the mutating endpoints
return 501 instead of a fake 200, since wiring live risk-limit *mutation*
through this API is a bigger change than this pass takes on -- an honest
"not implemented" can't be mistaken for a real edit, which is the actual
defect being fixed here.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.services.auth import require_admin_token

router = APIRouter(prefix="/risk", tags=["Risk"])


@router.get("/rules")
async def get_risk_rules(request: Request):
    engine = getattr(request.app.state, "trading_engine", None)
    rm = getattr(engine, "risk_manager", None)
    if rm is None:
        return []
    return [{"rule": name, "value": value} for name, value in rm.rules.items()]


@router.post("/rules", dependencies=[Depends(require_admin_token)])
async def create_risk_rule():
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Risk rule mutation via API is not implemented — edit RiskManager limits via .env / code.",
    )


@router.put("/rules/{id}", dependencies=[Depends(require_admin_token)])
async def update_risk_rule(id: int):
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Risk rule mutation via API is not implemented — edit RiskManager limits via .env / code.",
    )


@router.delete("/rules/{id}", dependencies=[Depends(require_admin_token)])
async def delete_risk_rule(id: int):
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail="Risk rule mutation via API is not implemented — edit RiskManager limits via .env / code.",
    )
