# NSE Market Hours (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30
SQUARE_OFF_HOUR = 15
SQUARE_OFF_MINUTE = 20  # Auto square-off 10 mins before close

# NSE F&O Stocks — Phase 1 (top liquid futures)
FNO_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "BAJFINANCE", "HINDUNILVR", "ITC", "KOTAKBANK",
    "AXISBANK", "LT", "WIPRO", "HCLTECH", "MARUTI",
    "SUNPHARMA", "ONGC", "NTPC", "POWERGRID", "ULTRACEMCO",
]

# Indicator defaults
EMA_FAST = 20
EMA_SLOW = 50
EMA_LONG = 200
RSI_PERIOD = 14
ATR_PERIOD = 14

# Risk defaults
MAX_DAILY_LOSS_PCT = 0.05
MAX_OPEN_POSITIONS = 5
MAX_EXPOSURE_PCT = 0.20
DEFAULT_CAPITAL = 300_000.0

# Backtest cost assumptions
BROKERAGE_PCT = 0.0003   # 0.03% per leg (Zerodha approx)
SLIPPAGE_PCT = 0.0002    # 0.02% slippage

# Scheduler job IDs
JOB_SIGNAL_GENERATION = "signal_generation"
JOB_ORDER_SYNC = "order_sync"
JOB_POSITION_SYNC = "position_sync"
JOB_DAILY_PNL = "daily_pnl_report"
JOB_MARKET_OPEN = "market_open"
JOB_MARKET_CLOSE = "market_close"

# Redis key prefixes
REDIS_TICK_PREFIX = "tick:"
REDIS_POSITION_KEY = "positions:all"
REDIS_SIGNAL_PREFIX = "signal:"
