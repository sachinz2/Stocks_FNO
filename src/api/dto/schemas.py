from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

# Common
class StatusResponse(BaseModel):
    status: str

# Stocks
class StockResponse(BaseModel):
    symbol: str
    sector: Optional[str] = None
    lot_size: Optional[int] = None

class StockDetailResponse(StockResponse):
    name: str
    active: bool

# Market Data
class MarketDataResponse(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

class MarketDataLoadRequest(BaseModel):
    symbol: str
    from_date: str
    to_date: str

# Indicators
class IndicatorResponse(BaseModel):
    ema20: Optional[float]
    ema50: Optional[float]
    rsi14: Optional[float]
    atr14: Optional[float]
    vwap: Optional[float]

# Signals
class SignalResponse(BaseModel):
    symbol: str
    signal: str
    confidence: float

class SignalGenerateRequest(BaseModel):
    strategy: str

class SignalGenerateResponse(BaseModel):
    generated: int

# Strategy
class StrategyResponse(BaseModel):
    name: str
    active: bool

class StrategyActionRequest(BaseModel):
    strategy: str

# Backtest
class BacktestRunRequest(BaseModel):
    strategy: str
    symbol: str
    start_date: str
    end_date: str

class BacktestRunResponse(BaseModel):
    run_id: int

class BacktestResultResponse(BaseModel):
    status: str
    profit_factor: float
    drawdown: float
    win_rate: float

# Orders
class OrderRequest(BaseModel):
    symbol: str
    side: str
    # Fixed 2026-08-21 (deep review): RiskManager.validate_trade() didn't
    # reject non-positive quantity/price either (fixed separately) -- a
    # negative quantity makes trade_value = quantity * price negative,
    # trivially passing the max-exposure check. Reject at the API boundary
    # too, in addition to the server-side risk check (defense in depth).
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)

class OrderResponse(BaseModel):
    order_id: str

# Positions
class PositionResponse(BaseModel):
    symbol: str
    quantity: int
    avg_price: float
    pnl: float

