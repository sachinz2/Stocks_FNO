# NSE Market Hours (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30
SQUARE_OFF_HOUR = 15
SQUARE_OFF_MINUTE = 20  # Auto square-off 10 mins before close

# NSE F&O option-eligible stocks (40 most liquid)
# Lot sizes and strike intervals are hardcoded here. NOTE: despite what this
# comment used to claim, there is no live daily refresh from
# kite.instruments("NFO") anywhere in this codebase (grepped and confirmed
# 2026-08-06) -- this hardcoded table is the ONLY source of truth. Verify at
# nseindia.com/market-data/fo-equity-securities (or against a fresh
# kite.instruments("NFO") pull) periodically, since NSE revises these when a
# stock's price moves into a different band.
FNO_SYMBOLS = [
    # Tier 1 — highest liquidity
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "BAJFINANCE", "KOTAKBANK", "AXISBANK", "LT",
    # Tier 2 — high liquidity
    "HINDUNILVR", "ITC", "WIPRO", "HCLTECH", "MARUTI",
    "SUNPHARMA", "M&M", "BHARTIARTL", "ADANIPORTS", "ASIANPAINT",
    # Tier 3 — good liquidity
    "TITAN", "BAJAJ-AUTO", "EICHERMOT", "INDUSINDBK", "DRREDDY",
    "CIPLA", "DIVISLAB", "JSWSTEEL", "HINDALCO", "GRASIM",
    # Tier 4 — moderate liquidity
    "TATACONSUM", "APOLLOHOSP", "NESTLEIND", "TECHM", "BPCL",
    "ONGC", "NTPC", "POWERGRID", "ULTRACEMCO", "TATASTEEL",
    # Tier 5 — added
    "COALINDIA",
]

# Hardcoded lot sizes -- fallback ONLY. Unlike FNO_STRIKE_INTERVALS above
# (confirmed no live refresh exists anywhere), _get_lot_size() in
# live_trading_engine.py DOES check Redis first (REDIS_LOT_SIZE_PREFIX,
# populated daily by scripts/zerodha_auto_auth.py's "Lot sizes cached for
# 41 F&O symbols" step right after the 08:30 token refresh) and only falls
# back to this table if that key is missing.
#
# Fixed 2026-08-07: audited every symbol here against a live
# kite.instruments("NFO") pull, same methodology as the strike-interval
# audit -- 36 of 39 symbols were wrong (some by a wide margin: KOTAKBANK
# 400 vs real 2000, NESTLEIND 40 vs real 500). Under normal operation this
# was masked by the Redis cache above, so it wasn't corrupting live trades
# -- but on any day the daily auth job fails or is delayed (already
# observed this week, e.g. the 2026-08-07 dead-WebSocket-reactor incident),
# the system would silently fall back to this table and submit wildly
# wrong order quantities right at the moment things are already going
# wrong -- for a real broker, a near-certain exchange rejection (order
# quantity must be an exact multiple of the real lot size); in paper mode,
# a silently mispriced position with no rejection to catch it.
# Verify at nseindia.com/market-data/fo-equity-securities if issues arise.
FNO_LOT_SIZES = {
    "RELIANCE":    500,   "TCS":         225,   "INFY":        400,
    "HDFCBANK":    650,   "ICICIBANK":   700,   "SBIN":        750,
    "BAJFINANCE":  750,   "KOTAKBANK":  2000,   "AXISBANK":    625,
    "LT":          175,   "HINDUNILVR":  300,   "ITC":        1725,
    "WIPRO":      3000,   "HCLTECH":     400,   "MARUTI":       50,
    "SUNPHARMA":   350,   "M&M":         200,   "BHARTIARTL":  475,
    "ADANIPORTS":  475,   "ASIANPAINT":  250,   "TITAN":       175,
    "BAJAJ-AUTO":   75,   "EICHERMOT":   100,   "INDUSINDBK":  700,
    "DRREDDY":     625,   "CIPLA":       425,   "DIVISLAB":    100,
    "JSWSTEEL":    675,   "HINDALCO":    700,   "GRASIM":      250,
    "TATACONSUM":  550,   "APOLLOHOSP":  125,   "NESTLEIND":   500,
    "TECHM":       600,   "BPCL":       1975,   "ONGC":       2250,
    "NTPC":       1500,   "POWERGRID":  1900,   "ULTRACEMCO":   50,
    "TATASTEEL":  2750,   "COALINDIA":  1350,
}

# Gap between consecutive strikes on NSE for each symbol.
#
# Fixed 2026-08-06: audited every symbol here against a live
# kite.instruments("NFO") pull (nearest expiry, most-populated strike chain)
# after discovering TITAN's entry here (25) doesn't match the real exchange
# value (50) -- confirmed live: the strategy had built and traded
# "TITAN26AUG4975CE", a strike that doesn't exist on Zerodha (real strikes
# are ...4900, 4950, 5000, 5050...). Since get_option_quote() can only find a
# contract that actually exists, this silently forced every single-leg trade
# on an affected symbol to run on the synthetic ATR-estimate fallback for its
# entire life (entry AND every exit check) instead of ever touching a real
# quote -- 27 of the 39 symbols below had a wrong value, not just TITAN.
FNO_STRIKE_INTERVALS = {
    "RELIANCE":    10,   "TCS":         20,   "INFY":        15,
    "HDFCBANK":    10,   "ICICIBANK":   10,   "SBIN":        10,
    "BAJFINANCE":  10,   "KOTAKBANK":    5,   "AXISBANK":    20,
    "LT":          50,   "HINDUNILVR":  20,   "ITC":        2.5,
    "WIPRO":      2.5,   "HCLTECH":     10,   "MARUTI":     100,
    "SUNPHARMA":   20,   "M&M":          50,  "BHARTIARTL":  20,
    "ADANIPORTS":  20,   "ASIANPAINT":  20,   "TITAN":       50,
    "BAJAJ-AUTO": 100,   "EICHERMOT":  100,   "INDUSINDBK":  10,
    "DRREDDY":     10,   "CIPLA":       10,   "DIVISLAB":   100,
    "JSWSTEEL":    20,   "HINDALCO":    20,   "GRASIM":      20,
    "TATACONSUM":  10,   "APOLLOHOSP": 100,   "NESTLEIND":   10,
    "TECHM":       16,   "BPCL":         5,   "ONGC":       2.5,
    "NTPC":         5,   "POWERGRID":  2.5,   "ULTRACEMCO":  40,
    "TATASTEEL":  2.5,   "COALINDIA":    5,
}

# How many stocks to trade at a time per strategy regime
ACTIVE_TRADING_SYMBOLS = 5

# ATR from LTPPoller is computed from 5-minute candles (ATR14 over 14 five-min bars).
# A single 5-min bar's range is much smaller than a full day's range, so raw 5-min
# ATR% is not comparable to thresholds calibrated in daily-ATR% terms. To convert:
#   daily_ATR_proxy = 5min_ATR x sqrt(bars_per_day)
#   NSE session = 375 min / 5 = 75 bars/day -> scale factor = sqrt(75) ~= 8.66
# Shared by live_trading_engine.py (sigma/strike calcs) and regime_detector.py
# (TRENDING classification) — both must use the same factor to stay consistent.
FIVE_MIN_ATR_DAILY_SCALE: float = 75 ** 0.5

# Sector classification for concentration check (max 2 open structures per sector)
FNO_SECTORS = {
    "RELIANCE":    "Energy",
    "TCS":         "IT",
    "INFY":        "IT",
    "HDFCBANK":    "Banking",
    "ICICIBANK":   "Banking",
    "SBIN":        "Banking",
    "BAJFINANCE":  "NBFC",
    "KOTAKBANK":   "Banking",
    "AXISBANK":    "Banking",
    "LT":          "Infrastructure",
    "HINDUNILVR":  "FMCG",
    "ITC":         "FMCG",
    "WIPRO":       "IT",
    "HCLTECH":     "IT",
    "MARUTI":      "Auto",
    "SUNPHARMA":   "Pharma",
    "M&M":         "Auto",
    "BHARTIARTL":  "Telecom",
    "ADANIPORTS":  "Infrastructure",
    "ASIANPAINT":  "Chemicals",
    "TITAN":       "Consumer",
    "BAJAJ-AUTO":  "Auto",
    "EICHERMOT":   "Auto",
    "INDUSINDBK":  "Banking",
    "DRREDDY":     "Pharma",
    "CIPLA":       "Pharma",
    "DIVISLAB":    "Pharma",
    "JSWSTEEL":    "Metals",
    "HINDALCO":    "Metals",
    "GRASIM":      "Chemicals",
    "TATACONSUM":  "FMCG",
    "APOLLOHOSP":  "Healthcare",
    "NESTLEIND":   "FMCG",
    "TECHM":       "IT",
    "BPCL":        "Energy",
    "ONGC":        "Energy",
    "NTPC":        "Power",
    "POWERGRID":   "Power",
    "ULTRACEMCO":  "Cement",
    "TATASTEEL":   "Metals",
    "COALINDIA":   "Mining",
}

# Capital fraction allocated to each strategy (must sum to <= 1.0).
# Keys must match the instance_id StrategyRegistry.load_strategy() is called with in
# api/main.py (e.g. "ema_crossover_v1") — that's the exact string RiskManager sees as
# strategy_name at every order call, not the uppercase registry name used to look up
# the strategy CLASS. A mismatch here makes this budget check silently never fire.
STRATEGY_CAPITAL_ALLOCATION = {
    # Rebalanced 2026-07-30 to make room for momentum_v1 (new — see
    # strategies/momentum.py) while keeping the total at 100%. Trimmed
    # ema_crossover_v1 and iron_condor_v1 more than credit_spread_v1 (the
    # proven highest performer to date) since momentum_v1 is unproven and
    # covers similar "strong trend" ground to ema_crossover_v1.
    "ema_crossover_v1": 0.30,   # ₹90,000 at ₹3L capital (was 0.40)
    "credit_spread_v1": 0.35,   # ₹1,05,000 (was 0.40)
    "iron_condor_v1":   0.15,   # ₹45,000 (was 0.20)
    "momentum_v1":      0.20,   # ₹60,000 (new)
}

# Max open structures per sector (prevents correlated blow-ups)
MAX_SECTOR_POSITIONS = 2

# Each strategy regime gets its own ranked symbol pool written by LTPPoller.
# Engine reads the right key based on which strategy is generating the signal.
REDIS_TOP_SYMBOLS_KEY = "nfo:top5"                        # EMA crossover: high ATR + strong trend
REDIS_TOP_SYMBOLS_CREDIT_SPREAD = "nfo:top5:spread"       # Credit spread: low ATR + EMA directional
REDIS_TOP_SYMBOLS_IRON_CONDOR = "nfo:top5:condor"         # Iron condor: low ATR + EMA flat
REDIS_TOP_SYMBOLS_MOMENTUM = "nfo:top5:momentum"          # Momentum: high ADX + wide EMA spread (established trend)

REDIS_LOT_SIZE_PREFIX = "nfo:lot:"

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
JOB_DAILY_PNL = "daily_pnl_report"
JOB_MARKET_OPEN = "market_open"
JOB_MARKET_CLOSE = "market_close"

# Redis key prefixes
REDIS_TICK_PREFIX = "tick:"
REDIS_POSITION_KEY = "positions:all"
REDIS_SIGNAL_PREFIX = "signal:"
