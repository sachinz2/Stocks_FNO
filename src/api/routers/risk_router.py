from fastapi import APIRouter

router = APIRouter(prefix="/risk", tags=["Risk"])

@router.get("/rules")
async def get_risk_rules():
    return []

@router.post("/rules")
async def create_risk_rule():
    return {}

@router.put("/rules/{id}")
async def update_risk_rule(id: int):
    return {}

@router.delete("/rules/{id}")
async def delete_risk_rule(id: int):
    return {"status": "deleted"}
