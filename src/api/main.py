from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.api.middleware.error_handler import global_exception_handler

from src.api.routers import (
    stocks_router, 
    market_data_router, 
    orders_router, 
    positions_router, 
    signals_router, 
    risk_router, 
    backtest_router, 
    strategy_router
)

app = FastAPI(
    title="Falcon Quant Platform API",
    version="1.0",
    description="Automated algorithmic trading platform API",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Exception Handlers
app.add_exception_handler(Exception, global_exception_handler)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/v1/health")
async def health_check():
    return {
        "status": "UP",
        "database": "UP",
        "redis": "UP"
    }

# Include Routers
app.include_router(stocks_router.router, prefix="/api/v1")
app.include_router(market_data_router.router, prefix="/api/v1")
app.include_router(orders_router.router, prefix="/api/v1")
app.include_router(positions_router.router, prefix="/api/v1")
app.include_router(signals_router.router, prefix="/api/v1")
app.include_router(risk_router.router, prefix="/api/v1")
app.include_router(backtest_router.router, prefix="/api/v1")
app.include_router(strategy_router.router, prefix="/api/v1")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)