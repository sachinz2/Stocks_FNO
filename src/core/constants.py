# NSE Market Hours (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30
SQUARE_OFF_HOUR = 15
SQUARE_OFF_MINUTE = 20  # Auto square-off 10 mins before close

# NSE F&O option-eligible stocks.
# Lot sizes and strike intervals for these are dynamically refreshed daily
# from a live kite.instruments("NFO") pull (see scripts/zerodha_auto_auth.py's
# fetch_and_cache_lot_sizes()/fetch_and_cache_real_contracts(), both filtered
# to this exact list) -- the FNO_LOT_SIZES/FNO_STRIKE_INTERVALS tables below
# are fallback-only, used solely on a cache miss.
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

    # Tier 6 — added 2026-08-20: expanded from the real 208-symbol NSE F&O
    # universe (confirmed live via a real NFO instrument-dump pull, see
    # scripts/diagnostic_universe_timing.py) to every symbol whose measured
    # 20-day average daily turnover (scripts/diagnostic_universe_liquidity.py
    # -- volume x close, not just NSE's minimum F&O-eligibility bar) is at
    # least as high as TATACONSUM's, the least-liquid symbol already traded
    # above. Purely additive -- nothing above was removed or reordered.
    # Prerequisites already in place before this: FNO_SECTORS covers all 208
    # (sector-concentration check), get_real_strike_interval() derives
    # strike spacing from real listed contracts (no per-symbol manual
    # verification needed), and LTPPoller's OHLC prefetch is concurrent
    # (cold-start cycle time stays well under the 60s budget at this size).
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPOWER",
    "AMBER", "ASHOKLEY", "AUBANK", "BAJAJFINSV", "BANDHANBNK",
    "BANKBARODA", "BDL", "BEL", "BHARATFORG", "BHEL",
    "BOSCHLTD", "BRITANNIA", "BSE", "CANBK", "CGPOWER",
    "CHOLAFIN", "COFORGE", "CUMMINSIND", "DELHIVERY", "DIXON",
    "DLF", "DMART", "ETERNAL", "FORCEMOT", "GAIL",
    "GODREJCP", "GVT&D", "HAL", "HDFCAMC", "HDFCLIFE",
    "HEROMOTOCO", "HINDPETRO", "HINDZINC", "HYUNDAI", "IDEA",
    "IDFCFIRSTB", "INDIGO", "INDUSTOWER", "JIOFIN", "JUBLFOOD",
    "KALYANKJIL", "KAYNES", "KEI", "KPITTECH", "LAURUSLABS",
    "LICI", "LODHA", "LTF", "LTM", "LUPIN",
    "MANAPPURAM", "MAXHEALTH", "MAZDOCK", "MCX", "MOTHERSON",
    "MUTHOOTFIN", "NATIONALUM", "NAUKRI", "NYKAA", "OFSS",
    "PATANJALI", "PAYTM", "PERSISTENT", "PFC", "PIDILITIND",
    "POLICYBZR", "POLYCAB", "POWERINDIA", "RADICO", "RECLTD",
    "SAIL", "SBILIFE", "SHRIRAMFIN", "SOLARINDS", "SONACOMS",
    "SUZLON", "SWIGGY", "TMPV", "TORNTPHARM", "TRENT",
    "TVSMOTOR", "UNIONBANK", "VBL", "VEDL", "WAAREEENER",
    "ZYDUSLIFE",
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
#
# Fallback ONLY as of 2026-08-20: LiveTradingEngine._get_strike_interval()
# now derives the real interval from the same daily-refreshed real-contract
# cache _resolve_contract() validates against (option_chain.
# get_real_strike_interval()) and only falls back to this static table on a
# cache miss. This table can still drift out of sync with NSE (a strike band
# change moves the interval); the derived value can't, since it's read from
# strikes Zerodha has actually listed for the exact expiry being traded.
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

    # Added 2026-08-20 -- covers the full 208-symbol real F&O stock universe
    # (confirmed live via kite.instruments("NFO"), see scripts/
    # diagnostic_universe_timing.py), as prep for a future FNO_SYMBOLS
    # expansion. Not yet used (FNO_SYMBOLS is still the 41-symbol list) --
    # these sit unused until that expansion happens, at which point every
    # symbol already has real sector-concentration coverage instead of the
    # check silently no-op'ing for anything not yet mapped. Verified against
    # live sources (web search), not guessed from memory, for every symbol
    # whose business wasn't unambiguous from the name alone (recent listings
    # in particular: TMPV, PREMIERENE, VMM, LTM, GVT&D, WAAREEENER, PGEL).
    "360ONE":      "Financial Services",
    "ABB":         "Capital Goods",
    "ABCAPITAL":   "NBFC",
    "ADANIENSOL":  "Power",
    "ADANIENT":    "Infrastructure",
    "ADANIGREEN":  "Power",
    "ADANIPOWER":  "Power",
    "ALKEM":       "Pharma",
    "AMBER":       "Consumer Durables",
    "AMBUJACEM":   "Cement",
    "ANGELONE":    "Financial Services",
    "APLAPOLLO":   "Metals",
    "ASHOKLEY":    "Auto",
    "ASTRAL":      "Chemicals",
    "AUBANK":      "Banking",
    "AUROPHARMA":  "Pharma",
    "BAJAJFINSV":  "NBFC",
    "BAJAJHLDNG":  "NBFC",
    "BANDHANBNK":  "Banking",
    "BANKBARODA":  "Banking",
    "BANKINDIA":   "Banking",
    "BDL":         "Defence",
    "BEL":         "Defence",
    "BHARATFORG":  "Auto Ancillary",
    "BHEL":        "Capital Goods",
    "BIOCON":      "Pharma",
    "BLUESTARCO":  "Consumer Durables",
    "BOSCHLTD":    "Auto Ancillary",
    "BRITANNIA":   "FMCG",
    "BSE":         "Financial Services",
    "CAMS":        "Financial Services",
    "CANBK":       "Banking",
    "CDSL":        "Financial Services",
    "CGPOWER":     "Capital Goods",
    "CHOLAFIN":    "NBFC",
    "COCHINSHIP":  "Defence",
    "COFORGE":     "IT",
    "COLPAL":      "FMCG",
    "CONCOR":      "Infrastructure",
    "CROMPTON":    "Consumer Durables",
    "CUMMINSIND":  "Capital Goods",
    "DABUR":       "FMCG",
    "DALBHARAT":   "Cement",
    "DELHIVERY":   "Logistics",
    "DIXON":       "Consumer Durables",
    "DLF":         "Realty",
    "DMART":       "Retail",
    "ETERNAL":     "Consumer",
    "FEDERALBNK":  "Banking",
    "FORCEMOT":    "Auto",
    "FORTIS":      "Healthcare",
    "GAIL":        "Energy",
    "GLENMARK":    "Pharma",
    "GMRAIRPORT":  "Infrastructure",
    "GODFRYPHLP":  "FMCG",
    "GODREJCP":    "FMCG",
    "GODREJPROP":  "Realty",
    "GVT&D":       "Capital Goods",
    "HAL":         "Defence",
    "HAVELLS":     "Consumer Durables",
    "HDFCAMC":     "Financial Services",
    "HDFCLIFE":    "Insurance",
    "HEROMOTOCO":  "Auto",
    "HINDPETRO":   "Energy",
    "HINDZINC":    "Metals",
    "HYUNDAI":     "Auto",
    "ICICIGI":     "Insurance",
    "ICICIPRULI":  "Insurance",
    "IDEA":        "Telecom",
    "IDFCFIRSTB":  "Banking",
    "IEX":         "Power",
    "INDHOTEL":    "Hospitality",
    "INDIANB":     "Banking",
    "INDIGO":      "Aviation",
    "INDUSTOWER":  "Telecom",
    "INOXWIND":    "Power",
    "IOC":         "Energy",
    "IREDA":       "NBFC",
    "IRFC":        "NBFC",
    "JINDALSTEL":  "Metals",
    "JIOFIN":      "NBFC",
    "JSWENERGY":   "Power",
    "JUBLFOOD":    "FMCG",
    "KALYANKJIL":  "Consumer",
    "KAYNES":      "Consumer Durables",
    "KEI":         "Capital Goods",
    "KFINTECH":    "Financial Services",
    "KPITTECH":    "IT",
    "LAURUSLABS":  "Pharma",
    "LICHSGFIN":   "NBFC",
    "LICI":        "Insurance",
    "LODHA":       "Realty",
    "LTF":         "NBFC",
    "LTM":         "IT",
    "LUPIN":       "Pharma",
    "MANAPPURAM":  "NBFC",
    "MANKIND":     "Pharma",
    "MARICO":      "FMCG",
    "MAXHEALTH":   "Healthcare",
    "MAZDOCK":     "Defence",
    "MCX":         "Financial Services",
    "MFSL":        "Insurance",
    "MOTHERSON":   "Auto Ancillary",
    "MOTILALOFS":  "Financial Services",
    "MPHASIS":     "IT",
    "MUTHOOTFIN":  "NBFC",
    "NAM-INDIA":   "Financial Services",
    "NATIONALUM":  "Metals",
    "NAUKRI":      "IT",
    "NBCC":        "Infrastructure",
    "NHPC":        "Power",
    "NMDC":        "Mining",
    "NYKAA":       "Consumer",
    "OBEROIRLTY":  "Realty",
    "OFSS":        "IT",
    "OIL":         "Energy",
    "PAGEIND":     "Consumer",
    "PATANJALI":   "FMCG",
    "PAYTM":       "Financial Services",
    "PERSISTENT":  "IT",
    "PETRONET":    "Energy",
    "PFC":         "NBFC",
    "PGEL":        "Consumer Durables",
    "PHOENIXLTD":  "Realty",
    "PIDILITIND":  "Chemicals",
    "PIIND":       "Chemicals",
    "PNB":         "Banking",
    "PNBHOUSING":  "NBFC",
    "POLICYBZR":   "Financial Services",
    "POLYCAB":     "Capital Goods",
    "POWERINDIA":  "Capital Goods",
    "PREMIERENE":  "Power",
    "PRESTIGE":    "Realty",
    "RADICO":      "FMCG",
    "RBLBANK":     "Banking",
    "RECLTD":      "NBFC",
    "RVNL":        "Infrastructure",
    "SAIL":        "Metals",
    "SBICARD":     "Financial Services",
    "SBILIFE":     "Insurance",
    "SHREECEM":    "Cement",
    "SHRIRAMFIN":  "NBFC",
    "SIEMENS":     "Capital Goods",
    "SOLARINDS":   "Chemicals",
    "SONACOMS":    "Auto Ancillary",
    "SRF":         "Chemicals",
    "SUPREMEIND":  "Chemicals",
    "SUZLON":      "Power",
    "SWIGGY":      "Consumer",
    "TATAELXSI":   "IT",
    "TATAPOWER":   "Power",
    "TIINDIA":     "Auto Ancillary",
    "TMPV":        "Auto",
    "TORNTPHARM":  "Pharma",
    "TRENT":       "Retail",
    "TVSMOTOR":    "Auto",
    "UNIONBANK":   "Banking",
    "UNITDSPR":    "FMCG",
    "UNOMINDA":    "Auto Ancillary",
    "UPL":         "Chemicals",
    "VBL":         "FMCG",
    "VEDL":        "Metals",
    "VMM":         "Retail",
    "VOLTAS":      "Consumer Durables",
    "WAAREEENER":  "Power",
    "YESBANK":     "Banking",
    "ZYDUSLIFE":   "Pharma",
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

# Strategies whose positions are intraday-only by design (single-leg long
# options, always closed at 15:20 -- see _square_off_all()'s docstring:
# "ALWAYS close (overnight gap risk)"). credit_spread_v1/iron_condor_v1 are
# deliberately NOT here -- they're held for days/weeks to collect theta
# decay, closing them nightly would destroy the strategy's whole edge.
#
# Added 2026-08-07: used to select Zerodha's MIS product type for these
# two specifically (see ZerodhaBroker._product_for()) instead of NRML for
# everything -- MIS gives a real exchange-level auto-square-off backstop
# for the exact overnight-gap risk the docstring above calls out, in case
# our own scheduler fails to run _square_off_all() that day (already
# observed multiple scheduler/watchdog failure modes this session). No
# margin-efficiency downside for these two: they only ever BUY options
# (never write/sell), and option-buying margin is premium-only regardless
# of MIS vs NRML -- the usual MIS leverage benefit is for futures/short
# options, which these strategies don't use.
INTRADAY_PRODUCT_STRATEGIES = {"ema_crossover_v1", "momentum_v1"}

# Max open structures per sector (prevents correlated blow-ups)
MAX_SECTOR_POSITIONS = 2

# Each strategy regime gets its own ranked symbol pool written by LTPPoller.
# Engine reads the right key based on which strategy is generating the signal.
REDIS_TOP_SYMBOLS_KEY = "nfo:top5"                        # EMA crossover: high ATR + strong trend
REDIS_TOP_SYMBOLS_CREDIT_SPREAD = "nfo:top5:spread"       # Credit spread: low ATR + EMA directional
REDIS_TOP_SYMBOLS_IRON_CONDOR = "nfo:top5:condor"         # Iron condor: low ATR + EMA flat
REDIS_TOP_SYMBOLS_MOMENTUM = "nfo:top5:momentum"          # Momentum: high ADX + wide EMA spread (established trend)

REDIS_LOT_SIZE_PREFIX = "nfo:lot:"

# Real per-symbol contract data (expiry -> strike -> {CE/PE: real tradingsymbol}),
# refreshed daily from kite.instruments("NFO") alongside lot sizes -- see
# scripts/zerodha_auto_auth.py's fetch_and_cache_real_contracts() and
# src/market_data/option_chain.py's get_real_contract(). Used to validate/
# correct our own computed strike + build_option_symbol() string against
# what's actually listed, instead of trusting our own expiry-date and
# strike-interval arithmetic never drifts from reality.
REDIS_CONTRACT_PREFIX = "nfo:contracts:"

# Indicator defaults
EMA_FAST = 20
EMA_SLOW = 50
EMA_LONG = 200
RSI_PERIOD = 14
ATR_PERIOD = 14

# Risk defaults
MAX_DAILY_LOSS_PCT = 0.05
MAX_OPEN_POSITIONS = 5
DEFAULT_CAPITAL = 300_000.0
# Fixed 2026-08-13: removed a dead MAX_EXPOSURE_PCT = 0.20 that was never
# imported/read anywhere -- the real, enforced value lives in
# settings.MAX_EXPOSURE_PCT (core/config.py, currently 0.30) and had
# silently diverged from this one, which could have misled a future reader
# into using the wrong figure.

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
