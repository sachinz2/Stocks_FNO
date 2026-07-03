# Falcon Quant Platform — Complete Technical Documentation

**Version:** 3.0  
**Last Updated:** July 2026  
**Mode:** Paper Trading (live trading target: ~July 29, 2026)

---

## Table of Contents

1. [Platform Overview](#1-platform-overview)
2. [System Architecture](#2-system-architecture)
3. [Infrastructure & Deployment](#3-infrastructure--deployment)
4. [Configuration & Environment](#4-configuration--environment)
5. [Database Design](#5-database-design)
6. [API Reference](#6-api-reference)
7. [Trading Engine](#7-trading-engine)
8. [Trading Strategies](#8-trading-strategies)
9. [Risk Management](#9-risk-management)
10. [Order Management System](#10-order-management-system)
11. [Market Data Pipeline](#11-market-data-pipeline)
12. [Paper Broker](#12-paper-broker)
13. [Live Broker (Zerodha)](#13-live-broker-zerodha)
14. [Portfolio Management](#14-portfolio-management)
15. [Scheduler & Automation](#15-scheduler--automation)
16. [Dashboard](#16-dashboard)
17. [Notifications](#17-notifications)
18. [Backtesting & Research](#18-backtesting--research)
19. [Machine Learning Module](#19-machine-learning-module)
20. [Logging & Monitoring](#20-logging--monitoring)
21. [Security Model](#21-security-model)
22. [End-to-End Trade Lifecycle](#22-end-to-end-trade-lifecycle)
23. [Known Issues & Operational Notes](#23-known-issues--operational-notes)
24. [F&O Trading Reference](#24-fo-trading-reference)

---

## 1. Platform Overview

Falcon Quant Platform is an automated algorithmic trading system built for NSE F&O (Futures & Options) markets. It runs three complementary strategies simultaneously, allocating capital based on the current market regime detected from live indicators.

### 1.1 Goals

| Goal | Status |
|------|--------|
| Automated intraday F&O trading | Live (paper mode) |
| Three strategies with no overlap | Done |
| Realistic fee + slippage simulation | Done |
| Risk gating before every order | Done |
| Live Zerodha integration (Connect plan) | Done — WebSocket, REST LTP, Historical OHLC all active |
| Automated daily Zerodha auth (headless) | Done — Playwright handles React SPA authorize page |
| Real-time WebSocket LTP | Done — ZerodhaTicker via KiteTicker |
| Historical OHLC via Zerodha API | Done — LTPPoller + RSRanker use kite.historical_data() |
| Strategy auto-pause (rolling PF / drawdown) | Done — StrategyMonitor |
| Portfolio exposure analysis | Done — PortfolioAnalyzer (sector/beta/correlation) |
| Market regime detection + auto-switching | Done — MarketRegimeDetector (4 regimes) |
| Relative Strength ranking vs NIFTY50 | Done — RSRanker (every 5 min) |
| Walk-forward validation | Done — WalkForwardTester (IS/OOS windows) |
| Monte Carlo reserve sizing | Done — MonteCarloSimulator (10,000 bootstraps) |
| Parameter robustness analysis | Done — ParameterRobustnessAnalyzer |
| OI change signal | Done — oi_price_signal() in nse_oi.py |
| Email notifications | Done |
| Web dashboard | Done (Streamlit) |
| REST API | Done (FastAPI) |

### 1.2 Capital Allocation

- **Total capital:** ₹3,00,000
- **EMA Crossover strategy:** 40% = ₹1,20,000
- **Credit Spread strategy:** 40% = ₹1,20,000
- **Iron Condor strategy:** 20% = ₹60,000

### 1.3 Trading Universe

41 NSE F&O stocks across 5 liquidity tiers. The LTP Poller dynamically selects the top 5 stocks per strategy each minute based on current market regime scores. RSRanker additionally filters by Relative Strength vs NIFTY50. All 41 symbols are passed to the engine; the three strategy pools each surface the best 5 candidates from this full universe.

### 1.4 Key Design Principles

- **Defined risk only:** Every option trade has a capped maximum loss
- **No naked positions:** All SELL legs have a hedge BUY leg
- **Regime-aware:** Strategy selection changes dynamically based on market volatility and VIX
- **Fail-safe exits:** Hard square-off at 15:20 IST regardless of PnL
- **Strategy self-protection:** StrategyMonitor auto-pauses strategies losing edge in real-time
- **Paper-first:** All logic runs in paper mode until live trading is confirmed safe

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          FALCON QUANT PLATFORM v2.0                         │
│                                                                             │
│  ┌────────────────┐    ┌────────────────┐    ┌──────────────────────────┐   │
│  │  Streamlit     │    │  FastAPI       │    │  APScheduler             │   │
│  │  Dashboard     │◄──►│  REST API      │◄──►│  (60s cycles + jobs)     │   │
│  │  :8501         │    │  :8000         │    │                          │   │
│  └────────────────┘    └───────┬────────┘    └────────────┬─────────────┘   │
│                                │                          │                 │
│                     ┌──────────▼──────────────────────────▼───────┐         │
│                     │           LiveTradingEngine                  │         │
│                     │  ┌──────────┐  ┌────────────┐  ┌─────────┐  │         │
│                     │  │ Strategy │  │ Exit Mgr   │  │ Monitor │  │         │
│                     │  │ Registry │  │(spreads/   │  │ (regime │  │         │
│                     │  │ +pause/  │  │ condors)   │  │  /RS)   │  │         │
│                     │  │ resume   │  └────────────┘  └─────────┘  │         │
│                     │  └──────────┘                               │         │
│                     └──────┬──────────────────────┬───────────────┘         │
│                            │                      │                         │
│              ┌─────────────▼──────┐   ┌───────────▼────────┐               │
│              │   RiskManager      │   │   OrderManager     │               │
│              │   (7-layer gate)   │   │   (OMS + slippage  │               │
│              │ + StrategyMonitor  │   │    tracking)       │               │
│              │ + PortfolioAnalyzer│   └───────────┬────────┘               │
│              └────────────────────┘               │                         │
│                                      ┌────────────▼───────────────┐         │
│                                      │      Broker Adapter        │         │
│                                      │  ┌──────────┐ ┌─────────┐  │         │
│                                      │  │ Paper    │ │Zerodha  │  │         │
│                                      │  │ Broker   │ │KiteConn │  │         │
│                                      │  │(slippage │ │(Connect │  │         │
│                                      │  │  model)  │ │  plan)  │  │         │
│                                      │  └──────────┘ └─────────┘  │         │
│                                      └────────────────────────────┘         │
│                                                                             │
│  ┌─────────────────────────────────────────┐   ┌──────────┐  ┌──────────┐  │
│  │   Zerodha Market Data (Connect plan)    │   │  Redis   │  │  MySQL   │  │
│  │  ┌──────────────┐  ┌─────────────────┐  │──►│  (cache) │  │  (DB)    │  │
│  │  │ ZerodhaTicker │  │   LTPPoller     │  │   │          │  │          │  │
│  │  │ (WebSocket   │  │ (historical_    │  │   └──────────┘  └──────────┘  │
│  │  │  real-time)  │  │  data 5-min)    │  │                               │
│  │  └──────────────┘  └─────────────────┘  │                               │
│  │  ┌──────────────┐  ┌─────────────────┐  │                               │
│  │  │ZerodhaLTP    │  │   RSRanker      │  │                               │
│  │  │Poller(REST   │  │ (daily OHLC +   │  │                               │
│  │  │ 5-sec)       │  │  NIFTY compare) │  │                               │
│  │  └──────────────┘  └─────────────────┘  │                               │
│  └─────────────────────────────────────────┘                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Component Responsibilities

| Component | Responsibility |
|-----------|---------------|
| **FastAPI** | REST API, authentication, request routing |
| **LiveTradingEngine** | Strategy orchestration, exit management, position tracking |
| **StrategyRegistry** | Plugin-based strategy loading, pause/resume per strategy instance |
| **RiskManager** | 7-layer pre-order validation, kill switch, daily limits |
| **StrategyMonitor** | Auto-pause strategies with degrading PF or excess drawdown |
| **PortfolioAnalyzer** | Sector/beta/correlation exposure warnings |
| **MarketRegimeDetector** | Classifies TRENDING/RANGE_BOUND/VOLATILE/LOW_VOL; auto-switches strategies |
| **RSRanker** | Relative Strength scoring vs NIFTY50; published to Redis every 5 min |
| **OrderManager** | Order lifecycle, DB persistence, broker routing, fill_price/slippage tracking |
| **PaperBroker** | Exchange abstraction for paper trading (tiered bid-ask slippage model) |
| **ZerodhaBroker** | Live order execution via KiteConnect (NFO, PRODUCT_NRML) |
| **ZerodhaTicker** | Real-time WebSocket LTP via KiteTicker (sub-second updates) |
| **ZerodhaLTPPoller** | REST-based LTP refresh via kite.ltp() every 5 seconds (fallback) |
| **LTPPoller** | 5-min OHLC from kite.historical_data(); computes EMA/ATR/VWAP; scores 40 symbols |
| **APScheduler** | Runs recurring jobs (60s trading cycle, 5s LTP, 5min RS ranking) |
| **Redis** | Tick cache, active position state, top symbol lists, regime, RS ranks |
| **MySQL** | Persistent storage for orders, trades, positions, audit logs, walk-forward results |
| **Streamlit Dashboard** | Live monitoring UI |
| **Nginx** | Reverse proxy, TLS termination |

### 2.2 Data Flow

```
Zerodha kite.historical_data() (5-min OHLC)
      │
      ▼
LTPPoller.poll()  [every 60s]
  ├─ Compute EMA20/50, ATR14, VWAP per symbol
  ├─ Score all 40 symbols × 3 regimes
  └─ Write to Redis:
       tick:{SYMBOL}    ← full indicator tick (ltp_source=zerodha_historical)
       nfo:top5         ← EMA crossover pool
       nfo:top5:spread  ← credit spread pool
       nfo:top5:condor  ← iron condor pool

ZerodhaTicker (WebSocket)  [continuous]
  └─ Overwrites tick:{SYMBOL}.close with real-time LTP  (ltp_source=zerodha_realtime)

ZerodhaLTPPoller (REST)  [every 5s]
  └─ Overwrites tick:{SYMBOL}.close via kite.ltp()  (ltp_source=zerodha_rest)

RSRanker.rank()  [every 5 min]
  ├─ kite.historical_data(NIFTY50_token, ..., "day")  ← benchmark
  ├─ kite.historical_data(token, ..., "day") for each of 40 symbols
  ├─ Compute RS score: 40% 5d rel-return + 35% 20d rel-return + 25% EMA stack
  └─ Write to Redis: nfo:rs_ranks, nfo:rs_top10

MarketRegimeDetector.detect()  [every 60s, inside signal cycle]
  ├─ Read VIX from Redis (set by ZerodhaLTPPoller or estimated)
  ├─ Read NIFTY50 tick (ATR%, EMA spread%)
  ├─ Classify: TRENDING / RANGE_BOUND / VOLATILE / LOW_VOL
  └─ Write to Redis: market:regime
  └─ enforce_regime_switching() → StrategyRegistry.pause/resume

LiveTradingEngine.run_signal_cycle()  [every 60s]
  ├─ StrategyMonitor.evaluate_all()   ← auto-pause degrading strategies
  ├─ MarketRegimeDetector.detect()    ← update regime, pause/resume strategies
  ├─ Read top-5 symbols from Redis per strategy
  ├─ strategy.generate_signal(tick) → signal
  ├─ RiskManager.validate_trade()    → bool
  │   └─ PortfolioAnalyzer warnings  ← sector/beta/correlation checks
  ├─ OrderManager.place_order()      → Order (with fill_price + slippage tracked)
  ├─ Broker.place_order()            → order_id
  └─ Update DB, audit log, Redis state
```

---

## 3. Infrastructure & Deployment

### 3.1 Docker Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `falcon_mysql` | mysql:8.0 | 3307 (host) / 3306 (internal) | Primary database |
| `falcon_redis` | redis:7-alpine | (internal only) | Cache & state |
| `falcon_api` | Custom Dockerfile | 8000 | FastAPI application |
| `falcon_dashboard` | Custom Dockerfile | 8501 | Streamlit UI |
| `falcon_nginx` | nginx:alpine | 80, 443 | Reverse proxy |

All services communicate on the `falcon_network` internal bridge. Only port 80/443 are exposed publicly via Nginx.

### 3.2 Dockerfile

- Base: `python:3.11-slim`
- System packages: MySQL client libs + full Chromium system dependencies (for Playwright)
- Python packages: all `requirements.txt` dependencies
- Post-install: `playwright install chromium` — Chromium installed to `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` (world-readable, shared between root build user and `appuser` runtime user)
- App user: `appuser` (UID 1000) for non-root security
- Entry point: `uvicorn src.api.main:app --host 0.0.0.0 --port 8000`

### 3.3 Nginx Configuration

- Routes `/api/*` → `falcon_api:8000`
- Routes `/` → `falcon_dashboard:8501` (Streamlit)
- Handles WebSocket upgrade headers for Streamlit
- Serves on port 80; can add TLS certificate for 443

### 3.4 Deployment Script (`deploy.sh`)

```bash
./deploy.sh
```
- Pulls latest git changes
- Runs `docker compose build --no-cache`
- Runs `docker compose up -d`
- Applies Alembic migrations
- Waits for health check

### 3.5 VPS Setup (`scripts/vps-setup.sh`)

- Installs Docker, Docker Compose, git, nginx
- Sets up system user `falcon`
- Configures systemd service for auto-start on reboot
- Sets up daily cron for Zerodha token refresh (08:30 IST)

### 3.6 Database Migrations

```bash
# Apply all pending migrations
docker exec -it falcon_api alembic upgrade head

# Create new migration
alembic revision --autogenerate -m "description"

# Rollback one step
alembic downgrade -1
```

Migrations directory: `migrations/versions/`

| File | Description |
|------|-------------|
| `aeedfa3b6aaa_initial_schema.py` | Core tables (stocks, instruments, ohlc, orders, positions, trades) |
| `b001_add_backtest_risk_ml_audit_tables.py` | Backtesting, ML, audit tables |
| `b002_add_audit_fields_to_existing_tables.py` | Adds `created_at`, `updated_at` to all tables |
| `b003_add_slippage_and_journal_analytics.py` | `orders.fill_price/slippage`; `trade_journal` analytics columns |
| `b004_add_walk_forward_results.py` | `walk_forward_results` table |

> **Note:** Migrations b003 and b004 are idempotent — they skip columns/tables that already exist (MySQL errno 1060/1050 is caught and silently skipped). Safe to re-run.

### 3.7 Database Reset

```bash
python reset_db.py
```
Drops and recreates all tables. Use only in development. In production:

```sql
-- Clear trading history only (preserves schema)
DELETE FROM orders;
DELETE FROM positions;
DELETE FROM trades;
DELETE FROM trade_journal;
DELETE FROM audit_logs;
DELETE FROM walk_forward_results;
```

---

## 4. Configuration & Environment

### 4.1 Environment File

Copy `.env.example` to `.env` and fill in values. The `.env` file is never committed to git.

### 4.2 All Configuration Variables

```env
# ── Application ─────────────────────────────────────────────────────
APP_NAME=FalconQuant
ENV=production                    # production | development
LOG_LEVEL=INFO                    # DEBUG | INFO | WARNING | ERROR
LOG_DIR=/app/logs                 # Log file directory

# ── Database ─────────────────────────────────────────────────────────
DB_HOST=falcon_mysql              # Docker service name or IP
DB_PORT=3306
DB_NAME=falcon_db
DB_USER=falcon_user
DB_PASSWORD=<strong-password>

# ── Redis ─────────────────────────────────────────────────────────────
REDIS_HOST=falcon_redis
REDIS_PORT=6379
REDIS_PASSWORD=<redis-password>

# ── Zerodha Connect Plan (NEVER commit these) ─────────────────────────
ZERODHA_API_KEY=<api-key>         # From kite.trade developer console
ZERODHA_API_SECRET=<api-secret>
ZERODHA_USER_ID=<user-id>         # e.g. HDY389
ZERODHA_PASSWORD=<trading-password>
ZERODHA_TOTP_SECRET=<totp-secret> # Base32 secret from Zerodha 2FA setup

# ── Trading Mode ─────────────────────────────────────────────────────
TRADING_MODE=paper                # paper | live
INITIAL_CAPITAL=300000

# ── Risk Parameters ───────────────────────────────────────────────────
MAX_OPEN_POSITIONS=25
MAX_DAILY_LOSS_PCT=5              # Percentage (5 = 5%)
MAX_EXPOSURE_PCT=20               # Per BUY leg as % of capital

# ── Email Alerts ─────────────────────────────────────────────────────
EMAIL_SENDER=<gmail>
EMAIL_APP_PASSWORD=<app-password> # Gmail app password (NOT Gmail password)
EMAIL_RECIPIENT=<recipient-email>

# ── API Security ─────────────────────────────────────────────────────
LOGS_API_TOKEN=<token>            # Bearer token for /logs endpoints
JWT_SECRET_KEY=<random-secret>

# ── Dashboard ─────────────────────────────────────────────────────────
DASHBOARD_PASSWORD=<password>     # Streamlit login password
```

### 4.3 Settings Class (`src/core/config.py`)

```python
settings = get_settings()
settings.TRADING_MODE          # "paper" or "live"
settings.INITIAL_CAPITAL       # float: 300000.0
settings.ZERODHA_API_KEY       # str
settings.get_database_url()    # mysql+aiomysql://...
settings.get_redis_url()       # redis://:password@host:port/0
```

---

## 5. Database Design

### 5.1 Entity Relationship Overview

```
stocks ──< instruments ──< ohlc_data
  │                           │
  │                      indicators
  │
  └──< orders ──< audit_logs
  │
  └──< positions
  │
  └──< trades
  │
  └──< trade_journal
  │
  └── walk_forward_results  (standalone — linked by strategy_name + symbol)
```

### 5.2 Table Definitions

#### `orders`
Complete order lifecycle record.

| Column | Type | Description |
|--------|------|-------------|
| id | INT PK | Internal order ID |
| broker_order_id | VARCHAR(50) | Broker-assigned order ID |
| symbol | VARCHAR(50) | Option contract symbol |
| side | ENUM(BUY, SELL) | Order direction |
| quantity | INT | Number of lots × lot size |
| price | FLOAT | Order price per unit |
| fill_price | NUMERIC(18,4) | Actual fill price (with bid-ask slippage) |
| slippage | NUMERIC(18,4) | fill_price − price per unit |
| order_status | VARCHAR(30) | PENDING → OPEN → COMPLETED / REJECTED_BY_RISK / FAILED / EXPIRED |
| created_at, updated_at | DATETIME | Lifecycle timestamps |

#### `trade_journal`
Rich context for each multi-leg structure opened.

| Column | Type | Description |
|--------|------|-------------|
| id | INT PK | |
| strategy_name | VARCHAR(50) | |
| underlying | VARCHAR(20) | Underlying symbol (not contract) |
| structure_type | VARCHAR(30) | SINGLE_LEG, BULL_PUT_SPREAD, BEAR_CALL_SPREAD, IRON_CONDOR |
| contracts | JSON | List of option contracts involved |
| entry_price | FLOAT | Net credit or debit |
| quantity | INT | Lot size |
| iv_rank | FLOAT | IV rank at entry (0.0–1.0) |
| vix_at_entry | FLOAT | India VIX at entry |
| market_regime | VARCHAR(30) | TRENDING_UP, RANGE_BOUND, etc. |
| day_of_week | INT | 0=Monday … 4=Friday (IST) |
| hour_of_day | INT | Hour of entry in IST (9–15) |
| entry_time | DATETIME | When trade was opened |
| exit_time | DATETIME | When trade was closed (nullable) |
| exit_reason | VARCHAR(50) | PROFIT_TARGET, STOP_LOSS, DTE_EXPIRY, SQUARE_OFF |
| atr_at_exit | FLOAT | ATR14 value at trade close time |
| vix_at_exit | FLOAT | India VIX at trade close time |
| regime_label | VARCHAR(30) | TRENDING/RANGE_BOUND/VOLATILE at close time |
| total_slippage_pts | FLOAT | Sum of slippage across all legs × lot size |
| slippage | FLOAT | Alias for total_slippage_pts (backwards compat) |
| pnl | FLOAT | Net PnL for the entire structure |

#### `walk_forward_results`
Stores IS and OOS metrics from walk-forward analysis windows.

| Column | Type | Description |
|--------|------|-------------|
| id | BIGINT PK | |
| strategy_name | VARCHAR(50) | Strategy under test |
| symbol | VARCHAR(30) | Symbol tested |
| window_start | VARCHAR(20) | IS window start date |
| train_end | VARCHAR(20) | End of in-sample period |
| window_end | VARCHAR(20) | End of out-of-sample period |
| is_oos | INT | 0=in-sample, 1=out-of-sample |
| profit_factor | FLOAT | Gross wins / gross losses |
| sharpe_ratio | FLOAT | |
| max_drawdown | FLOAT | |
| win_rate | FLOAT | |
| total_pnl | FLOAT | |
| trade_count | INT | |
| avg_pnl | FLOAT | |
| expectancy | FLOAT | |
| parameters | JSON | Best parameter set for this window |
| run_at | TIMESTAMP | When analysis was run |

#### Other tables (unchanged from v1.0)
`stocks`, `instruments`, `ohlc_data`, `indicators`, `signals`, `positions`, `trades`, `audit_logs` — see v1.0 documentation for column definitions.

### 5.3 Redis Key Schema

```
tick:{SYMBOL}                  ← Full indicator tick (JSON) — written by LTPPoller,
                                 overwritten for 'close' by ZerodhaTicker/ZerodhaLTPPoller
nfo:top5                       ← EMA crossover top-5 (JSON array of symbols)
nfo:top5:spread                ← Credit spread top-5
nfo:top5:condor                ← Iron condor top-5
nfo:rs_ranks                   ← Full RS rank list [{symbol, rs_score, rank}, ...]
nfo:rs_top10                   ← Top-10 symbols by RS score (JSON array)
market:regime                  ← Current regime JSON {regime, vix, atr_pct, ...}
lot:{SYMBOL}                   ← Lot size (refreshed daily from live instruments)
iv_history:{SYMBOL}            ← Rolling daily IV readings for IV percentile rank (up to 252 entries)
vix:india                      ← Cached India VIX (5 min TTL)
falcon:active_spreads          ← Persisted _active_spreads state (survives restarts)
falcon:active_condors          ← Persisted _active_condors state (survives restarts)
engine:profit_closed_today     ← JSON {date, symbols} — symbols profit-closed this session; cleared at EOD
engine:exited_today            ← JSON {date, symbols} — symbols with adverse exits (breach/SL) today
engine:order_count             ← Today's order count (JSON, cleared at market open)
sl_freq:{SYMBOL}               ← Adverse exit counter (5-day TTL); circuit breaker fires at 2
zerodha:access_token           ← Live session token (24h TTL)
nse_oi_prev:{SYMBOL}           ← Previous OI snapshot for change-delta signal
```

### 5.4 Indexes

```sql
CREATE INDEX idx_orders_status ON orders(order_status);
CREATE INDEX idx_orders_symbol ON orders(symbol);
CREATE INDEX idx_ohlc_symbol_tf ON ohlc_data(symbol, timeframe);
CREATE INDEX idx_signals_strategy ON signals(strategy_name, generated_at);
CREATE INDEX idx_audit_action ON audit_logs(action, timestamp);
CREATE INDEX idx_wf_strategy ON walk_forward_results(strategy_name);
CREATE INDEX idx_wf_window ON walk_forward_results(window_start, window_end);
```

---

## 6. API Reference

### 6.1 Base URL

```
http://<host>/api/v1/
```

### 6.2 Authentication

Logs endpoints require a static bearer token. Other endpoints use JWT (development mode).

```
GET /api/v1/logs/tail?token=<LOGS_API_TOKEN>
```

### 6.3 Health Check

```http
GET /api/v1/health
```

Response:
```json
{
  "status": "UP",
  "database": "UP",
  "redis": "UP",
  "ltp_source": "Zerodha WebSocket (real-time)"
}
```

`ltp_source` values:
- `Zerodha WebSocket (real-time)` — ZerodhaTicker connected
- `Zerodha REST poll (5 s)` — ZerodhaLTPPoller active, WebSocket down
- `Zerodha historical OHLC (60 s)` — only LTPPoller running

### 6.4 Orders

```http
GET  /api/v1/orders
GET  /api/v1/orders?status=COMPLETED&limit=50
GET  /api/v1/orders/{id}
POST /api/v1/orders
```

### 6.5 Positions

```http
GET /api/v1/positions
GET /api/v1/positions/{symbol}
```

### 6.6 Signals

```http
GET /api/v1/signals
GET /api/v1/signals?strategy=CREDIT_SPREAD&limit=20
GET /api/v1/signals/{id}
```

### 6.7 Strategies

```http
GET  /api/v1/strategies
POST /api/v1/strategies/activate
POST /api/v1/strategies/deactivate
```

### 6.8 Risk Rules

```http
GET  /api/v1/risk/rules
POST /api/v1/risk/rules
PUT  /api/v1/risk/rules/{rule_name}
```

### 6.9 Market Data

```http
GET  /api/v1/market-data/{symbol}
POST /api/v1/market-data/load
```

Tick response:
```json
{
  "symbol": "INFY",
  "close": 1051.4,
  "ema20": 1048.2,
  "ema50": 1045.1,
  "atr14": 11.8,
  "vwap": 1049.7,
  "timestamp": "2026-06-19T13:00:00",
  "ltp_source": "zerodha_realtime"
}
```

### 6.10 Analytics

```http
GET  /api/v1/analytics/trades                         # All trade history
GET  /api/v1/analytics/trades?strategy=IRON_CONDOR&from=2026-06-01
GET  /api/v1/analytics/strategy-performance           # Rolling metrics per strategy
GET  /api/v1/analytics/strategy-health                # StrategyMonitor status + alerts
GET  /api/v1/analytics/portfolio-exposure             # Sector/beta/correlation report
GET  /api/v1/analytics/market-regime                  # Current regime from Redis
GET  /api/v1/analytics/rs-ranks                       # Relative Strength rankings
GET  /api/v1/analytics/walk-forward-results           # Historical WF results from DB
POST /api/v1/analytics/walk-forward                   # Run walk-forward analysis
POST /api/v1/analytics/monte-carlo                    # Run Monte Carlo simulation
POST /api/v1/analytics/robustness                     # Run parameter robustness analysis
```

Walk-forward POST body:
```json
{
  "strategy_name": "EMA_CROSSOVER",
  "symbol": "RELIANCE",
  "start_year": 2020,
  "end_year": 2025,
  "train_years": 2,
  "test_years": 1,
  "fast_min": 18,  "fast_max": 22,
  "slow_min": 45,  "slow_max": 55
}
```

Monte Carlo POST body:
```json
{ "strategy_name": "EMA_CROSSOVER", "n_simulations": 10000 }
```

Robustness POST body:
```json
{
  "strategy_name": "EMA_CROSSOVER",
  "symbol": "RELIANCE",
  "years": 3,
  "fast_min": 18,  "fast_max": 22,
  "slow_min": 45,  "slow_max": 55
}
```

### 6.11 Backtesting

```http
POST /api/v1/backtest/run
GET  /api/v1/backtest/{run_id}
```

### 6.12 Admin

```http
POST /api/v1/admin/reset                ← Wipe all trading data + reset in-memory engine state
POST /api/v1/admin/reset-iv-history     ← Clear iv_history:{symbol} keys for all 41 F&O symbols
POST /api/v1/admin/email-alerts/pause   ← Pause email notifications
POST /api/v1/admin/email-alerts/resume  ← Resume email notifications
GET  /api/v1/admin/email-alerts         ← Check current email alert state
```

`POST /api/v1/admin/reset` clears:
- All trading DB tables (orders, positions, trades, trade_journal, audit_logs, signals, walk_forward_results)
- All engine Redis keys (active_spreads, active_condors, exited_today, profit_closed_today, order_count)
- All `iv_history:{symbol}` keys
- In-memory engine state (_active_spreads, _active_condors, _exited_today, _profit_closed_today, _close_on_first_cycle, _peak_premiums)
- PaperBroker virtual balance (restored to INITIAL_CAPITAL)

`POST /api/v1/admin/reset-iv-history` clears only the IV rank history keys without touching trading data. Use this after a sigma calibration change to restart IV percentile accumulation with correct values.

### 6.13 Logs

```http
GET /api/v1/logs/?token=<LOGS_API_TOKEN>
GET /api/v1/logs/tail?n=300&token=<LOGS_API_TOKEN>
GET /api/v1/logs/download/{filename}?token=<LOGS_API_TOKEN>
```

---

## 7. Trading Engine

### 7.1 Overview

`LiveTradingEngine` (`src/live_trading/live_trading_engine.py`) is the central orchestrator. It now also hosts the StrategyMonitor, PortfolioAnalyzer, MarketRegimeDetector, and RSRanker evaluations inside each signal cycle.

### 7.2 Initialization (`src/api/main.py` lifespan)

```
1.  Create async DB engine, Redis client
2.  Fetch Zerodha access token from Redis
3.  If token present:
      a. Create ZerodhaBroker (kite_instance)
      b. Fetch all NSE instrument tokens from kite.instruments("NSE")
         — maps 40 F&O symbols to int tokens
         — finds NIFTY 50 token (stable value: 256265)
         — tokens shared across ZerodhaTicker, LTPPoller, RSRanker
      c. Start ZerodhaTicker (WebSocket, injects pre-fetched tokens)
4.  Create broker (ZerodhaBroker for LIVE, PaperBroker for PAPER)
5.  Create RiskManager(initial_capital=300000)
6.  Create OrderManager(broker, risk_manager, order_repo, audit_repo)
7.  Create PortfolioManager(broker)
8.  Create StrategyMonitor(trade_journal_repo)
9.  Create PortfolioAnalyzer()
10. Create MarketRegimeDetector(redis_client)
11. Create RSRanker(redis_client, kite=kite_instance, instrument_tokens=tokens)
12. Load 3 strategies into StrategyRegistry
13. Create LiveTradingEngine(broker, risk_mgr, order_mgr, portfolio_mgr, notifier,
        strategy_monitor=..., portfolio_analyzer=..., regime_detector=..., rs_ranker=...)
14. engine.attach_kite(kite_instance)  ← enables VIX + option quotes
15. Create LTPPoller(redis_client, kite=kite_instance, instrument_tokens=tokens)
16. Start ZerodhaLTPPoller (REST, every 5s)
17. Schedule all recurring APScheduler jobs
18. Store in app.state: trading_engine, redis, zerodha_ticker, kite, instrument_tokens
```

### 7.3 Signal Cycle (`run_signal_cycle`)

Called every 60 seconds by APScheduler during market hours (09:15–15:30 IST).

```python
async def run_signal_cycle():
    if not is_market_open(): return
    if is_square_off_time():
        await self._square_off_all()
        return

    # 0. Research quality checks (new)
    self.strategy_monitor.evaluate_all()          # auto-pause degrading strategies
    await self.regime_detector.detect()           # classify regime
    await self.regime_detector.enforce_regime_switching()  # pause/resume by regime

    vix = await self._get_cached_vix()
    active_strategies = StrategyRegistry.get_active_strategies()

    # 1. Update risk state with current broker positions
    positions = await self._safe_get_positions()
    await self._refresh_risk_state(positions)

    # 2. Portfolio exposure warnings
    exposure = self.portfolio_analyzer.get_report(positions)
    for flag in exposure.get("concentration_alerts", []):
        logger.warning(f"PortfolioAnalyzer: {flag}")

    # 3. Expire stale orders (open > 5 minutes)
    await self.order_manager.expire_stale_orders()

    # 4. Check all exit conditions
    await self._check_spread_exits(active_strategies)
    await self._check_condor_exits(active_strategies)
    await self._check_open_option_exits(positions, active_strategies)

    # 5. CRITICAL: Refresh risk state after exits
    positions = await self._safe_get_positions()
    await self._refresh_risk_state(positions)

    # 6. Entry signals
    for strategy_id, strategy in active_strategies.items():
        symbols = await self._get_active_symbols(strategy)
        for symbol in symbols:
            await self._process_signal(strategy, symbol, vix=vix)
```

### 7.4 Strategy Symbol Selection

Each strategy reads from its own Redis key:
- `EMACrossoverStrategy` → reads `nfo:top5` (top 5 by ATR% × trend score)
- `CreditSpreadStrategy` → reads `nfo:top5:spread` (top 5 low-vol directional)
- `IronCondorStrategy` → reads `nfo:top5:condor` (top 5 low-vol flat EMA)

Additionally, `RSRanker` publishes `nfo:rs_top10` — the top 10 by Relative Strength vs NIFTY50. The engine can filter entry candidates against this list.

### 7.5 Exit Management

Exits are checked in two places:
- **`_check_spread_exits` / `_check_condor_exits`** run inside `run_signal_cycle()` (every 60 s)
- A **dedicated 10-second exit check job** (APScheduler) calls these same methods more frequently for faster stop-loss execution; `_exit_cycle_lock` (asyncio.Lock) prevents concurrent runs

All exits are classified as either **adverse** (breach/SL/regime shift/near-expiry/forced close) or **profit** (75%+ target hit). This routing is critical: adverse exits populate `_exited_today` (blocks re-entry today), while profit exits populate `_profit_closed_today` (allows re-entry at DTE ≥ 14).

#### Spread Exits (`_check_spread_exits`)

For each spread in `_active_spreads`:
1. **DTE < 7:** Close immediately — gamma risk near expiry (adverse)
2. **Underlying crosses short strike:** Emergency stop — spread in-the-money (adverse)
3. **Short leg rises to 2× sold price:** Stop loss (adverse)
4. **Short leg decays to 25% of sold price:** 75% profit captured, close (profit)

#### Condor Exits (`_check_condor_exits`)

For each condor in `_active_condors`:
1. **DTE < 7:** Close all 4 legs (adverse)
2. **Underlying breaches short put OR short call strike:** Close entire condor (adverse)
3. **Either short leg rises to 2× sold price:** Close entire condor (adverse)
4. **Either short leg decays to 25% of sold price:** Close entire condor (profit) — when one wing wins, the stock has moved toward that side, putting the other wing at increasing risk; closing immediately locks in the gain

#### Single-Leg Exits (`_check_open_option_exits`)

For EMA crossover long option positions (intraday only — closed at EOD if not already out):
- Hard stop loss: premium fell ≥ 50%
- Profit target: premium rose ≥ 100% (doubled)
- Trailing stop: fell ≥ 25% from peak

### 7.6 State Persistence

Active spreads, condors, and session state are persisted to Redis on every change:
```
falcon:active_spreads          ← JSON dict of _active_spreads
falcon:active_condors          ← JSON dict of _active_condors
engine:profit_closed_today     ← JSON {date, symbols} for profit-closed symbols this session
engine:exited_today            ← JSON {date, symbols} for adverse-exit symbols this session
```

On startup, `_restore_state()` reloads all four keys from Redis. The engine survives restarts without losing track of open multi-leg structures or today's exit history.

**DTE roll detection on restart:** If the engine restarts and finds an active spread/condor that now has a different (lower) DTE than when it was opened — indicating the position rolled through an expiry — that symbol is added to `_close_on_first_cycle`. The position is closed immediately on the first signal cycle rather than being held into gamma risk territory.

### 7.7 Market Open/Close Hooks

| Time (IST) | Job | Action |
|------------|-----|--------|
| 09:15 | `on_market_open` | Reset `_today_order_count`, `risk_manager.reset_daily_state()` |
| 15:20 | `is_square_off_time()` check | `_square_off_all()` — closes **EMA crossover single-leg positions only**; credit spreads and iron condors are multi-day and are NOT force-closed at EOD |
| 15:30 | `on_market_close` | Cleanup |
| 15:45 | `send_daily_report` | Email PnL summary; clears `_profit_closed_today` set |

**Multi-day holding:** Credit spreads and iron condors persist overnight. They are only closed when their own exit conditions trigger (DTE < 7, breach, SL, or profit target) — not by the 15:20 square-off. This allows theta to compound over days without forced intraday closes.

---

## 8. Trading Strategies

### 8.1 Strategy Registry Pattern

```python
@StrategyRegistry.register("EMA_CROSSOVER")
class EMACrossoverStrategy(StrategyBase):
    ...
```

The `StrategyRegistry` now supports **pause/resume** per instance:

```python
StrategyRegistry.pause_strategy("ema_crossover_v1")   # sets instance.is_active = False
StrategyRegistry.resume_strategy("ema_crossover_v1")  # sets instance.is_active = True
```

Used by `StrategyMonitor` (auto-pause on PF degradation) and `MarketRegimeDetector` (auto-pause on regime mismatch).

### 8.2 EMA Crossover Strategy

**File:** `src/strategies/ema_crossover.py` | **Capital:** 40% (₹1,20,000)

Buys ATM options in the direction of EMA20/50 crossover. Directional momentum strategy suited for high-volatility stocks.

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fast_period` | 20 | Fast EMA period |
| `slow_period` | 50 | Slow EMA period |
| `stop_loss_pct` | 0.50 | Exit if premium falls 50% |
| `target_pct` | 1.0 | Exit if premium doubles |
| `trailing_stop_pct` | 0.25 | Exit if premium falls 25% from peak |
| `signal_confirm_bars` | 2 | Crossover must persist 2 bars |

**Signal logic:**
```
EMA20 crosses above EMA50 → BUY CE (bullish crossover)
EMA20 crosses below EMA50 → BUY PE (bearish crossover)
Crossover must persist for signal_confirm_bars=2 to filter whipsaws.
```

### 8.3 Credit Spread Strategy

**File:** `src/strategies/credit_spread.py` | **Capital:** 40% (₹1,20,000)

Sells a near-ATM option, buys a further-OTM option for net credit. Profits from multi-day time decay (theta). Defined risk. Positions held overnight until exit conditions trigger — not closed at EOD.

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `low_vol_threshold` | 1.2 | ATR% must be below this |
| `spread_width` | 2 | Strike intervals between legs |
| `profit_close_pct` | 0.25 | Close at 75% profit capture |
| `stop_loss_multiple` | 2.0 | Close at 2× original premium |
| `min_dte` | 7 | DTE at which exit is triggered (gamma risk) |

**Signal logic:**
```
ATR% >= 1.2% → HOLD (EMA crossover handles high-vol)
EMA20 > EMA50 → BULL_PUT_SPREAD
EMA20 < EMA50 → BEAR_CALL_SPREAD
```

**Entry gates (all must pass):**
- DTE ≥ 21 for fresh entries; DTE ≥ 14 for re-entries after a same-day profit close (`_profit_closed_today`)
- Not already in `_exited_today` (adverse exit blocks re-entry for the day)
- Not already in `_active_condors` (stacking guard — no spread on top of live condor)
- VWAP alignment: underlying price ≥ VWAP × 0.995 for BULL_PUT_SPREAD; ≤ VWAP × 1.005 for BEAR_CALL_SPREAD (medium-term trend confirmation from 10-day 5-minute candles)
- HV/IV ratio: market implied vol (from live short-leg LTP) ÷ realized vol (sigma) ≥ 1.10 — only sell premium when the market is paying at least 10% more than realized volatility
- Net credit ≥ ₹350 total
- PCR alignment with spread direction
- No crowded OI at short strike

### 8.4 Iron Condor Strategy

**File:** `src/strategies/iron_condor.py` | **Capital:** 20% (₹60,000)

Sells both a put spread and a call spread simultaneously. Profits when underlying stays within the range of both short strikes. Positions held overnight — not closed at EOD.

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `low_vol_threshold` | 1.2 | ATR% must be below this |
| `flat_threshold` | 0.1 | EMA spread% must be below this |
| `short_offset` | 1 | Short strikes 1 interval from ATM |
| `hedge_offset` | 2 | Long strikes 2 intervals beyond short |
| `profit_close_pct` | 0.25 | Close when either short leg hits 75% profit |
| `stop_loss_multiple` | 2.0 | Close entire condor if either short leg 2× original |
| `min_dte` | 7 | DTE at which exit is triggered (gamma risk) |

**Signal logic:**
```
ATR% >= 1.2% → HOLD
EMA_spread% >= 0.1% → HOLD (credit spread handles directional)
→ IRON_CONDOR
```

**Entry gates:**
- DTE ≥ 21 for fresh entries; DTE ≥ 14 for re-entries after same-day profit close
- Not already in `_active_spreads` (stacking guard — no condor on top of live spread for same symbol)
- Net credit ≥ ₹600 (covers 8-order round-trip fees)
- HV/IV ratio and VWAP alignment apply via the sigma computation (same as credit spread)

### 8.5 Strategy Regime Assignment

```
ATR% >= 1.2%                   → EMA crossover pool (nfo:top5)
ATR% < 1.2% AND EMA% >= 0.1%  → Credit spread pool (nfo:top5:spread)
ATR% < 1.2% AND EMA% < 0.1%   → Iron condor pool (nfo:top5:condor)
```

### 8.6 Regime-Driven Auto-Switching

`MarketRegimeDetector` classifies the market every cycle and auto-pauses/resumes strategies:

| Regime | Condition | Active Strategies |
|--------|-----------|-------------------|
| TRENDING | VIX 12–20, ATR% ≥ 1.5% | EMA Crossover |
| RANGE_BOUND | VIX 12–20, ATR% < 1.5%, EMA flat | Iron Condor |
| VOLATILE | VIX > 20 or ATR% ≥ 2.5% | Credit Spread |
| LOW_VOL | VIX < 12 | Credit Spread + Iron Condor |

Paused strategies do not generate signals until the regime shifts back.

---

## 9. Risk Management

### 9.1 Overview

Three layers of risk control operate in parallel:
1. **RiskManager** — pre-order 7-layer validation gate
2. **StrategyMonitor** — rolling performance auto-pause
3. **PortfolioAnalyzer** — sector/beta/correlation exposure warnings

### 9.2 RiskManager — 7-Layer Validation

#### Layer 0: Exit Orders — Always Pass
```python
if is_exit_order:
    return True  # all layers bypassed — an open position must always be closeable
```

This is checked before anything else. Kill switch, daily loss limit, position count — none of these apply to exit orders. Trapping open losses behind a risk gate is more dangerous than the exit order itself.

#### Layer 1: Kill Switch / Circuit Breaker (entries only)
```python
if kill_switch_active or circuit_breaker_active:
    return False  # new entries blocked; exits already returned True above
```

#### Layer 2: Daily Loss Limit
```python
max_loss = -(initial_capital * 0.05)  # -₹15,000
if total_daily_pnl <= max_loss:
    activate_kill_switch()
    return False
```

#### Layer 3: IV Rank Gate (first legs only)
```python
if iv_rank < 0.30: return False   # options too cheap
if vix < 14.0:     return False   # market-wide IV too low
```

#### Layer 4: Sector Concentration
```python
MAX_SECTOR_POSITIONS = 2
if sector_count >= 2: return False
```

#### Layer 5: Per-Strategy Capital Allocation
```python
if deployed + trade_value > budget: return False
```

#### Layer 6: Max Open Positions
```python
if is_new_symbol and len(open_positions) >= 25: return False
```

#### Layer 7: Per-Leg BUY Exposure
```python
if side == "BUY" and trade_value > initial_capital * 0.20: return False
```

Spread legs (is_spread_leg=True) skip layers 3–7 and return True after just layers 1–2.

### 9.3 StrategyMonitor — Rolling Performance Auto-Pause

**File:** `src/risk/strategy_monitor.py`

```python
ROLLING_WINDOW = 30        # last 30 closed trades per strategy
ROLLING_PF_FLOOR = 0.9    # pause if profit factor < 0.9
DRAWDOWN_MULTIPLIER = 1.5  # pause if drawdown > 1.5× expected
MIN_TRADES_REQUIRED = 30  # minimum trades before evaluation (< 30 is statistically meaningless)

DEFAULT_EXPECTED_DRAWDOWN = {
    "ema_crossover": 15000,
    "credit_spread":  10000,
    "iron_condor":     8000,
}
```

`evaluate_all()` is called every signal cycle:
- If rolling PF < 0.9 → `StrategyRegistry.pause_strategy(instance_id)`
- If rolling drawdown > 1.5× expected → pause
- Auto-resumes when metrics recover above thresholds
- Reports available via `GET /api/v1/analytics/strategy-health`

### 9.4 PortfolioAnalyzer — Exposure Analysis

**File:** `src/risk/portfolio_analyzer.py`

Runs on every signal cycle for warnings (does not block orders):

```python
SECTOR_NOTIONAL_LIMIT = 60_000   # ₹60k per sector
PORTFOLIO_BETA_LIMIT  = 5.0      # max weighted portfolio beta

get_report(positions) → {
    "sector_exposure":       {...},  # sector → notional ₹
    "beta_exposure":         float,  # weighted portfolio beta
    "correlation_flags":     [...],  # pairs with r > 0.85 both open
    "concentration_alerts":  [...],  # sectors over limit
}
```

Contains betas for all 40 F&O symbols and 21 high-correlation pairs (r > 0.85).

---

## 10. Order Management System

### 10.1 Overview

`OrderManager` manages the complete order lifecycle: creation, risk validation, broker routing, state tracking, fill tracking, and audit logging.

### 10.2 `place_order` Flow

```
1. Create DB record (status=PENDING)
   └─> AuditLog: ORDER_RECEIVED

2. risk_manager.validate_trade()
   └─> If fails: status=REJECTED_BY_RISK → return

3. broker.place_order(symbol, side, quantity, price)
   └─> Returns broker_order_id
   └─> DB: status=OPEN, broker_order_id=...
   └─> AuditLog: ORDER_ROUTED
   └─> If BUY: risk_manager.add_deployed_capital(strategy_name, value)
```

### 10.3 Order Status Flow

```
PENDING → REJECTED_BY_RISK
        → OPEN
             → COMPLETED
             → CANCELLED
             → EXPIRED  (open > 5 minutes)
             → FAILED
```

### 10.4 Fill Price & Slippage Tracking

`sync_orders()` runs every 30 seconds and now persists actual fill data:

```python
fill_price = b_order.get("fill_price") or b_order.get("average_price")
if fill_price and db_order.fill_price is None:
    db_order.fill_price = fill_price
    db_order.slippage   = fill_price - db_order.price  # negative for SELL
```

This enables post-trade slippage analysis and live trading cost measurement.

### 10.5 Audit Trail

Every order action is logged to `audit_logs`:
```python
ORDER_RECEIVED, ORDER_ROUTED, ORDER_REJECTED_RISK,
ORDER_STATUS_SYNC, ORDER_EXPIRED, ORDER_FAILED
```

---

## 11. Market Data Pipeline

**All market data now comes from Zerodha (Connect plan). yfinance has been removed entirely.**

### 11.1 Layer 1: WebSocket Ticker (Real-Time)

**File:** `src/market_data/zerodha_ticker.py`

- `ZerodhaTicker` connects via `KiteTicker` WebSocket
- Subscribes to all 40 F&O underlying tokens in `MODE_LTP`
- On each tick: reads existing `tick:{SYMBOL}` from Redis, updates `close`, writes back
- Sets `ltp_source = "zerodha_realtime"`
- Runs in a background daemon thread with automatic reconnect (max 5 attempts)
- On 403 error: stops reconnecting (auth issue, must re-run auth script)

Instrument tokens are fetched once at startup from `kite.instruments("NSE")` and injected directly — no duplicate API call.

### 11.2 Layer 2: REST LTP Poller (5-Second Fallback)

**File:** `src/market_data/zerodha_ltp_poller.py`

- `ZerodhaLTPPoller.refresh_ltp()` calls `kite.ltp(["NSE:SYMBOL", ...])` for all 40 symbols in one batch
- Runs every 5 seconds via APScheduler
- Updates only the `close` field in each Redis tick, leaving EMA/ATR/VWAP intact
- Sets `ltp_source = "zerodha_rest"`
- Gracefully disables itself on "Insufficient permission" error

Always runs when `kite_instance` is available, even when WebSocket is also active (provides a reliable 5-second backup).

### 11.3 Layer 3: Historical OHLC + Indicators (60-Second Cycle)

**File:** `src/market_data/ltp_poller.py`

Runs every 60 seconds via APScheduler.

**Data flow per cycle:**

```
For each of 40 FNO_SYMBOLS:
    1. _get_history(symbol):
       - If cache stale (> 5 min):
           kite.historical_data(token, from=now-10d, to=now, "5minute")
       - Returns list of {date, open, high, low, close, volume}

    2. _enrich(symbol, df, ltp) → tick dict:
       - EMA20, EMA50 (exponential moving averages)
       - ATR14 (Average True Range)
       - VWAP (Volume Weighted Average Price)
       - ltp_source = "zerodha_historical"

    3. Write to Redis: tick:{SYMBOL}

    4. _score_all(tick) → (ema_score, spread_score, condor_score)

After all 40 symbols:
    5. Publish top-5 pools to Redis
```

**Scoring formulas:**
```
EMA Crossover:  ATR% × 0.6 + EMA_spread% × 0.4
Credit Spread:  (1.2 - ATR%) × 0.4 + EMA_spread% × 0.6   [0 if ATR% ≥ 1.2%]
Iron Condor:    (1.2 - ATR%) × 0.6 + (0.1 - EMA_spread%) × 0.4  [0 if ATR% ≥ 1.2% or EMA_spread% ≥ 0.1%]
```

### 11.4 RSRanker — Relative Strength vs NIFTY50

**File:** `src/market_data/rs_ranker.py`

Runs every 5 minutes via APScheduler.

```python
# NIFTY50 index: stable instrument token 256265 on NSE
kite.historical_data(256265, from_date, to_date, "day")

# All 40 F&O symbols
kite.historical_data(token, from_date, to_date, "day")
```

**RS Score (0–100):**
```
5-day relative return  (40%): (sym_ret5 - nifty_ret5) normalized to 0–40
20-day relative return (35%): (sym_ret20 - nifty_ret20) normalized to 0–35
EMA20 > EMA50 bonus    (25%): +25 if bullish stack, else 0
```

Published to Redis:
- `nfo:rs_ranks` — full list sorted by RS score descending
- `nfo:rs_top10` — top 10 symbol strings

Sample output (live):
```
#1  EICHERMOT: RS=48.51
#2  TITAN:     RS=46.74
#3  BAJFINANCE: RS=44.90
```

### 11.5 MarketRegimeDetector

**File:** `src/market_data/regime_detector.py`

Called every signal cycle. Reads VIX, NIFTY50 ATR%, and EMA spread from Redis ticks.

```python
TRENDING:     VIX 12–20,  ATR% >= 1.5%
RANGE_BOUND:  VIX 12–20,  ATR% <  1.5%,  EMA_spread <  0.15%
VOLATILE:     VIX > 20   or  ATR% >= 2.5%
LOW_VOL:      VIX < 12
```

After classifying, writes to `market:regime` and calls `enforce_regime_switching()` which pauses/resumes strategy instances.

### 11.6 Option Chain Analysis

**File:** `src/market_data/option_chain.py`

Key functions:

```python
find_delta_strike(price, target_delta, option_type, dte, sigma, interval)
    → BSM delta to find the −0.20 delta strike for short legs

get_entry_prices_for_spread(symbol, short_contract, long_contract, kite, redis, atr, dte)
    → kite.ltp() for real prices; falls back to estimate_option_premium()

get_india_vix(redis)       → reads cached VIX
fetch_and_cache_vix(kite, redis)  → fetches + caches (5-min TTL)

atr_to_annualised_vol(atr, price)
    → (atr / price) * sqrt(252) — assumes DAILY ATR input

implied_vol(premium, spot, strike, T, option_type)
    → Black-Scholes implied volatility from a live option price
```

**Critical: ATR scaling before sigma computation**

LTPPoller supplies a 5-minute ATR (ATR14 over 14 five-minute bars). `atr_to_annualised_vol()` assumes daily ATR. Without correction, sigma is ~7× too low, causing `find_delta_strike()` to place short strikes only ~1% OTM instead of the intended ~5-6%.

The fix applied in all three sigma computations (credit spread, iron condor, IV rank):
```python
_5MIN_ATR_SCALE: float = 75 ** 0.5   # sqrt(375 min/day ÷ 5 min/bar) = sqrt(75)
sigma = atr_to_annualised_vol(atr * _5MIN_ATR_SCALE, underlying_price)
```

This scales 5-minute ATR to daily-equivalent before annualising, producing correct sigma (~28% for typical F&O stocks) and correctly placed delta strikes (~5-6% OTM for short legs).

### 11.7 NSE Open Interest Data

**File:** `src/market_data/nse_oi.py`

Now includes OI change-delta signal:

```python
oi_price_signal(call_oi_change, put_oi_change, price_change_pct):
    Price↑ + OI↑  → STRONG_BULLISH   (fresh longs entering)
    Price↑ + OI↓  → WEAK_BULLISH     (short covering)
    Price↓ + OI↑  → STRONG_BEARISH   (fresh shorts entering)
    Price↓ + OI↓  → WEAK_BEARISH     (longs exiting)
```

Previous OI snapshot stored in `nse_oi_prev:{symbol}` for change calculation.

---

## 12. Paper Broker

**File:** `src/paper_trading/paper_broker.py`

### 12.1 Tiered Bid-Ask Slippage Model

PaperBroker now applies a realistic bid-ask half-spread based on option premium:

| Premium Range | Half-Spread (per unit) |
|--------------|------------------------|
| ≥ ₹50 | ₹0.50 |
| ₹10 – ₹50 | ₹0.25 |
| ₹2 – ₹10 | ₹0.10 |
| < ₹2 | ₹0.05 |

- **BUY** fill = price + half_spread (pay the ask)
- **SELL** fill = price − half_spread (receive the bid)

`fill_price` and `slippage` are returned in the order dict so OrderManager can persist them.

### 12.2 Fee Structure

| Component | Formula | Notes |
|-----------|---------|-------|
| Brokerage | min(₹20, turnover × 0.03%) | ₹20 flat for most trades |
| STT | turnover × 0.1% | SELL side only |
| Exchange charge | turnover × 0.053% | NSE F&O rate |
| GST | (brokerage + exchange) × 18% | |
| SEBI charge | turnover × 0.000001 | ₹10 per crore |
| Stamp duty | turnover × 0.003% | BUY side only |

Where `turnover = quantity × fill_price`.

---

## 13. Live Broker (Zerodha)

**File:** `src/brokers/zerodha.py`

### 13.1 Zerodha Plan

The platform uses the **Zerodha Connect plan** (not Personal). This provides full access to:

| Feature | Status |
|---------|--------|
| Order placement (NFO, NRML) | ✅ Active |
| `kite.ltp()` — REST LTP | ✅ Confirmed working |
| `kite.historical_data()` — OHLC | ✅ Confirmed working |
| KiteTicker WebSocket streaming | ✅ Active (after Docker rebuild) |
| India VIX via `kite.quote()` | ✅ Active |
| Instrument lot sizes (live refresh) | ✅ Fetched daily after auth |

### 13.2 Automated Daily Authentication

**File:** `scripts/zerodha_auto_auth.py`

Runs at 08:30 IST every weekday. Fully headless — no manual intervention.

**Full OAuth flow:**
```
1. GET kite.trade/connect/login?api_key=...&v=3
   → Follows redirect to kite.zerodha.com
   → Establishes session cookie

2. POST kite.zerodha.com/api/login
   {user_id, password}
   → Returns request_id

3. POST kite.zerodha.com/api/twofa
   {user_id, request_id, twofa_value=pyotp.TOTP(secret).now(), twofa_type="totp"}
   → Session is now authenticated

4. GET kite.zerodha.com/connect/login?api_key=...&v=3
   → 302 → connect/finish?sess_id=...
   → 302 → connect/authorize?sess_id=...  (200 — React SPA)

5. Playwright headless Chromium (PLAYWRIGHT_BROWSERS_PATH=/ms-playwright):
   - Injects authenticated session cookies from requests.Session
   - Navigates to connect/authorize URL
   - Waits for networkidle (React renders)
   - Clicks "Allow" button (selector: button[type='submit'])
   - Intercepts redirect to capture request_token

6. kite.generate_session(request_token, api_secret) → access_token

7. Store access_token in Redis (key: zerodha:access_token, TTL: 24h)
   Also write to /tmp/zerodha_token.json

8. kite.instruments("NFO") → cache lot sizes in Redis for all 40 F&O symbols
```

Run manually:
```bash
docker exec -it falcon_api python3 scripts/zerodha_auto_auth.py
```

### 13.3 Order Placement

```python
kite.place_order(
    variety=kite.VARIETY_REGULAR,
    exchange="NFO",
    tradingsymbol=symbol,         # e.g., "INFY26JUL1500CE"
    transaction_type=side,         # "BUY" or "SELL"
    quantity=quantity,
    product=kite.PRODUCT_NRML,    # Overnight margin product
    order_type=kite.ORDER_TYPE_MARKET,
    validity=kite.VALIDITY_DAY,
)
```

Retried up to 3 times with exponential backoff via `tenacity`.

### 13.4 Market Data Diagnostic

**File:** `scripts/test_market_data.py`

Run to verify all three data sources:
```bash
docker exec -it falcon_api python3 scripts/test_market_data.py
```

Tests: instrument tokens, REST LTP, 5-min OHLC, daily OHLC, NIFTY 50, WebSocket Redis ticks, RSRanker output.

---

## 14. Portfolio Management

**Files:** `src/portfolio/portfolio_manager.py`, `src/portfolio/positions_tracker.py`

`sync_positions()` runs every 60 seconds:
1. Fetch positions from broker
2. Update or create records in `positions` DB table
3. Mark qty=0 positions as closed
4. Compute unrealized PnL = (market_price - avg_price) × quantity

---

## 15. Scheduler & Automation

### 15.1 APScheduler Jobs

| Job | Trigger | Function | Notes |
|-----|---------|----------|-------|
| LTP poll | Every 60s | `ltp_poller.poll()` | Zerodha 5-min OHLC + indicators |
| Zerodha LTP REST | Every 5s | `zerodha_ltp_poller.refresh_ltp()` | Fast LTP update via kite.ltp() |
| RS Ranking | Every 300s | `rs_ranker.rank()` | Daily OHLC + NIFTY relative strength |
| Signal cycle | Every 60s | `engine.run_signal_cycle()` | Market hours only; includes entry signals |
| **Exit check** | **Every 10s** | **`engine._check_spread_exits` + `engine._check_condor_exits`** | **Market hours; faster stop-loss response; `_exit_cycle_lock` prevents concurrent runs** |
| Order sync | Every 30s | `engine.sync_orders()` | Always |
| Position sync | Every 60s | `engine.sync_positions()` | Always |
| Market open | 09:15 IST | `engine.on_market_open()` | Weekdays |
| Market close | 15:30 IST | `engine.on_market_close()` | Weekdays |
| Daily report | 15:45 IST | `engine.send_daily_report()` | Weekdays; clears `_profit_closed_today` |
| Zerodha auth | 08:30 IST | `zerodha_auto_auth.py` (cron) | Weekdays, headless Playwright |

### 15.2 Zerodha Auto-Auth Cron

```bash
# Added to crontab on VPS:
30 3 * * 1-5 docker exec falcon_api python3 /app/scripts/zerodha_auto_auth.py
# (08:30 IST = 03:00 UTC)
```

---

## 16. Dashboard

**File:** `src/dashboard/app.py` | **Port:** 8501 (via Nginx at `/`)

### 16.1 Pages / Sections

| Section | Data Source | Key Metrics |
|---------|-------------|-------------|
| **Home** | `/api/v1/health` | Net PnL, Open positions, Capital, LTP source |
| **Positions** | `/api/v1/positions` | Symbol, Qty, Avg price, Unrealized PnL |
| **Orders & Trades** | `/api/v1/orders` | Order count, status breakdown |
| **Strategies** | `/api/v1/strategies` | Active strategies, pause/resume status |
| **Risk & PnL** | `/api/v1/risk/rules` | Daily loss, position limits, kill switch |
| **Analytics** | `/api/v1/analytics/trades` | Win rate, profit factor, drawdown |
| **Portfolio Exposure** | `/api/v1/analytics/portfolio-exposure` | Sector/beta/correlation flags |
| **Market Regime** | `/api/v1/analytics/market-regime` | Current regime, VIX, ATR% |
| **RS Rankings** | `/api/v1/analytics/rs-ranks` | Top-10 by RS score |
| **System Health** | `/api/v1/health` | DB, Redis, LTP source, uptime |

### 16.2 LTP Source Display

The dashboard shows a colour-coded indicator for the current data source:
- 🟢 `zerodha_realtime` — WebSocket live
- 🟢 `zerodha_rest` — REST poll (5s)
- 🔵 `zerodha_historical` — OHLC-based (60s)

---

## 17. Notifications

### 17.1 Email (Primary)

**File:** `src/notifications/email_service.py`

Gmail SMTP with app password. Triggered on: new spread/condor opened, position closed, kill switch activated, daily PnL report at 15:45, auth failures.

### 17.2 Combo Notifier

**File:** `src/notifications/combo_notifier.py`

Wraps email + optional Telegram into one interface: `notifier.send(message)`.

---

## 18. Backtesting & Research

### 18.1 Standard Backtesting Engine

**File:** `src/backtesting/engine.py`

Event-driven backtesting engine replays historical OHLC through a strategy. Access via `POST /api/v1/backtest/run`.

### 18.2 Walk-Forward Validation

**File:** `src/backtesting/walk_forward.py`

Prevents curve-fitting by separating parameter optimization (in-sample) from performance measurement (out-of-sample).

```
Window 1:  Train 2020–2022 → Test 2023
Window 2:  Train 2021–2023 → Test 2024
Window 3:  Train 2022–2024 → Test 2025
```

**Verdict criteria:**
```
degradation_ratio = OOS_PF / IS_PF

ROBUST:    OOS_PF ≥ 1.2 AND degradation_ratio ≥ 0.65 → genuine edge
MARGINAL:  OOS_PF ≥ 1.0 AND degradation_ratio ≥ 0.50 → run more windows
CURVE_FIT: otherwise                                   → do NOT trade live
```

Results persisted to `walk_forward_results` table. Access via `POST /api/v1/analytics/walk-forward`.

Data source: `kite.historical_data(token, from, to, "day")` — requires valid Zerodha session.

### 18.3 Monte Carlo Reserve Sizing

**File:** `src/backtesting/monte_carlo.py`

Bootstrap simulation on historical trade PnL series:

```python
MonteCarloSimulator(initial_capital=300_000, n_simulations=10_000, ruin_threshold=0.50)
```

**Output:**
```json
{
  "drawdown_p25": -8000,
  "drawdown_p50": -14000,
  "drawdown_p75": -22000,
  "drawdown_p95": -38000,    ← use this for cash reserve sizing
  "drawdown_p99": -58000,
  "total_pnl_p5":  -12000,
  "total_pnl_median": 45000,
  "total_pnl_p95": 120000,
  "ruin_probability": 0.03   ← probability of losing 50%+ of capital
}
```

Access via `POST /api/v1/analytics/monte-carlo`. Data pulled from `trade_journal` table.

### 18.4 Parameter Robustness Analysis

**File:** `src/backtesting/robustness.py`

Tests whether a strategy is curve-fit by checking how many nearby parameter combinations are profitable.

```
Robustness ratio = profitable_combos / total_combos

ROBUST:    ≥ 0.60 (60%+ of combos profitable — smooth PnL surface)
MARGINAL:  ≥ 0.35
CURVE_FIT: < 0.35 (only the exact optimal params work — overfit)
```

Includes 2D PnL heatmap surface for visual analysis. Access via `POST /api/v1/analytics/robustness`.

---

## 19. Machine Learning Module

**Files:** `src/ml/feature_store.py`, `src/ml/model_trainer.py`, `src/ml/prediction_engine.py`

Supports XGBoost and LightGBM classifiers. Features: EMA spread ratios, ATR normalised by price, RSI, VWAP deviation, MACD crossover, lag features.

> **Current status:** Implemented but not yet integrated into the live signal cycle. Designed to filter/confirm rule-based signals. Integration planned post-live-trading validation.

---

## 20. Logging & Monitoring

### 20.1 Log Configuration

**File:** `src/core/logger.py`

Two handlers: console (stdout) + rotating file (`/app/logs/falcon.log`, 10 MB, 7 backups).

Format: `YYYY-MM-DD HH:MM:SS | LEVEL | module | message`

Silenced loggers: `uvicorn.access` (WARNING), `apscheduler` (WARNING), `peewee` (WARNING).

### 20.2 Remote Log Access

```bash
curl "http://<host>/api/v1/logs/tail?n=300&token=<LOGS_API_TOKEN>"
curl "http://<host>/api/v1/logs/?token=<LOGS_API_TOKEN>"
curl "http://<host>/api/v1/logs/download/falcon.log.1?token=<LOGS_API_TOKEN>"
```

### 20.3 Key Log Patterns

| Log Pattern | Meaning |
|------------|---------|
| `ZerodhaTicker: WebSocket connected — subscribed 41 symbols in LTP mode` | Live WebSocket active |
| `ZerodhaLTPPoller: refreshed LTP for 41 symbols` | REST LTP working |
| `kite.historical_data failed for SYMBOL: ...` | OHLC fetch issue (check token) |
| `RSRanker: top-5 = [EICHERMOT, TITAN, ...]` | RS ranking complete |
| `Regime: TRENDING (VIX=16.2, ATR%=1.8)` | Regime classified |
| `StrategyMonitor: PAUSED ema_crossover_v1 (rolling_pf=0.82)` | Strategy auto-paused |
| `EMA pool top-5: ['RELIANCE', 'TCS', ...]` | Symbol selection |
| `Playwright: clicked 'button[type='submit']'` | Daily auth flow |
| `Access token stored in Redis` | Auth successful |
| `KILL SWITCH ACTIVATED: Max Daily Loss Reached` | Emergency stop |
| `Expired 1 stale order(s).` | Order auto-cancelled after 5 min |
| `[PortfolioDelta] bulls=2 bears=1 condors=1` | Portfolio directional balance logged each cycle |
| `[CreditSpread] INFY re-entering after same-day profit close (DTE=16)` | Re-entry after profit close |
| `[CreditSpread] RELIANCE skipped — VWAP filter (price below VWAP − 0.5%)` | VWAP direction filter rejected entry |
| `[CreditSpread] TCS skipped — IV/HV ratio 0.92 < 1.10` | HV/IV filter rejected entry |
| `[IronCondor] HDFCBANK skipped — already in active_spreads` | Condor stacking guard |

---

## 21. Security Model

### 21.1 Secret Storage

| Secret | Storage Location | Never In |
|--------|-----------------|----------|
| Zerodha API key | `.env` on server | Code, git |
| Zerodha API secret | `.env` | Code, git |
| Zerodha user password | `.env` | Code, git |
| Zerodha TOTP secret | `.env` | Code, git |
| Gmail app password | `.env` | Code, git |
| Zerodha access token | Redis only (24h TTL) | DB, code |
| LOGS_API_TOKEN | `.env` | Code, git |
| JWT secret | `.env` | Code, git |
| Dashboard password | `.env` | Code, git |

### 21.2 Docker Networking

- Only ports 80 and 443 (via Nginx) are exposed to the internet
- MySQL, Redis, API, Dashboard communicate on internal Docker network only
- App runs as non-root `appuser` (UID 1000)
- Playwright Chromium path (`/ms-playwright`) is world-readable but owned by root

---

## 22. End-to-End Trade Lifecycle

### 22.1 Complete Example: Iron Condor on JSWSTEEL

```
Day 1, 08:30 IST — zerodha_auto_auth.py runs
  → access_token in Redis, lot sizes refreshed (JSWSTEEL lot = 1350)

Day 1, 09:45 IST — LTPPoller.poll()
  kite.historical_data(JSWSTEEL_token, ..., "5minute") → 2,000 candles (10 days)
  JSWSTEEL tick:
    close=890.5, ema20=889.3, ema50=889.8
    atr14=8.2 (5-min), atr_daily=8.2 × √75 = 71.0
    atr_pct=0.92% (< 1.2% → low vol)
    ema_spread_pct=0.056% (< 0.1% → flat)
    vwap (10-day)=888.0
    condor_score=0.38 (high), ltp_source="zerodha_historical"

  ZerodhaTicker overwrites: close=890.7, ltp_source="zerodha_realtime"
  Redis: nfo:top5:condor = ["JSWSTEEL", "NTPC", ...]

Day 1, 09:46 IST — LiveTradingEngine.run_signal_cycle()

  StrategyMonitor.evaluate_all()  → all strategies healthy (PF > 0.9)
  RegimeDetector.detect()         → RANGE_BOUND (VIX=16.2, ATR%=0.92%)
  enforce_regime_switching()      → Iron Condor ACTIVE, EMA Crossover PAUSED

  IronCondorStrategy.generate_signal(tick):
    ATR%=0.92 < 1.2 ✓ | EMA_spread%=0.056 < 0.1 ✓ → IRON_CONDOR

  PortfolioAnalyzer: no concentration alerts (Metals sector: 0/2 open)

  _process_iron_condor("JSWSTEEL"):
    DTE=28 ≥ 21 ✓ (fresh entry floor)
    JSWSTEEL not in _active_spreads ✓ (stacking guard)
    sigma = atr_to_annualised_vol(71.0, 890.5) = 28.3%

    put_short=840 (delta ~-0.20), put_long=820
    call_short=940 (delta ~+0.20), call_long=960

    Live LTP from kite.ltp():
      put_short: ₹14.00, put_long: ₹4.50 → put wing credit = ₹9.50
      call_short: ₹12.00, call_long: ₹3.80 → call wing credit = ₹8.20
      total net credit = ₹17.70 × 1350 = ₹23,895 ≥ ₹600 ✓

    IV/HV check:
      Implied vol from ₹14.00 put_short ≈ 31.2%
      IV/HV ratio = 31.2% / 28.3% = 1.10 ✓ (≥ 1.10 threshold, barely passes)

  4 legs placed → condor registered in _active_condors
  Persisted to Redis: falcon:active_condors
  GTT backstop placed on short legs at 2.5× entry (₹35 put / ₹30 call)
  Email: "IRON CONDOR OPENED — JSWSTEEL — Net credit ₹17.70 × 1350 = ₹23,895"

  Trade journal record:
    day_of_week=3 (Thursday), hour_of_day=9
    vix_at_entry=16.2, market_regime=RANGE_BOUND

---  Day 6 (theta has decayed significantly) ---

  _check_condor_exits() [runs every 10 seconds]:
    call_short current price: ₹2.90  ≤  ₹12.00 × 0.25 = ₹3.00  → TRIGGER (call wing at 75%)

  Exit reason: "Call wing at 75%+ profit — closing condor"
  → Classified as PROFIT close → _profit_closed_today.add("JSWSTEEL")

  4 exit orders placed, fills recorded with fill_price + slippage
  Trade journal updated: atr_at_exit, vix_at_exit, regime_label, total_slippage_pts, pnl
  Email: "IRON CONDOR CLOSED — JSWSTEEL — Call wing at 75%+ profit | PnL ₹+14,200"

  JSWSTEEL can now re-enter same day if DTE ≥ 14 and conditions remain good.
```

---

## 23. Known Issues & Operational Notes

### 23.1 DTE Churn (Fixed — June 2026)

DTE gate at ENTRY prevents opening positions without sufficient time runway. No new entries when DTE < 21 (fresh entry) or DTE < 14 (re-entry after same-day profit close). Exit trigger at DTE < 7 captures most theta while avoiding gamma explosion.

### 23.2 NSE Monthly Expiry

NSE F&O expiry: last Thursday of the month.
- July 2026 = Thursday July 31

System enters quiet period when DTE < 7 (no new entries). Resumes entry after roll to next expiry (DTE ≈ 28–42). Multi-day positions already open are exited at DTE < 7.

### 23.3 Stale Risk State After Exits (Fixed — June 2026)

Second `_refresh_risk_state()` call after exits ensures sector concentration check sees the post-exit position list before evaluating new entries.

### 23.4 SEBI Fee Formula (Fixed — June 2026)

Corrected to `turnover × 0.000001` (₹10 per crore). Previous formula was 100,000× too large.

### 23.5 Migration Idempotency

If `alembic upgrade head` fails with `Duplicate column name` (errno 1060) or `Table already exists` (errno 1050), use:
```bash
docker exec -it falcon_api alembic stamp b004   # mark as applied
docker exec -it falcon_api alembic current      # verify = b004 (head)
```

### 23.6 WebSocket vs REST LTP Race

Both ZerodhaTicker (WebSocket) and ZerodhaLTPPoller (REST, 5s) write to the same Redis `tick:{SYMBOL}.close` key. This is intentional — they provide the same data from different latency paths. The last write wins but both are fresh. ZerodhaLTPPoller is the guaranteed reliable path; WebSocket is the faster path.

### 23.7 Playwright Chromium Path

`playwright install chromium` must run as root during Docker build with `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` set. If this env var is missing, Chromium installs to `/root/.cache/ms-playwright/` but `appuser` looks in `/home/appuser/.cache/ms-playwright/` and fails. The Dockerfile is correctly configured — only appears on old images pre-rebuild.

### 23.8 Position Limit vs Strategy Demand

With 5 credit spread symbols (10 position legs) and 5 iron condor symbols (20 position legs), total = 30 > 25 max. If both strategies are at full capacity simultaneously, the 6th condor's hedge legs may hit the position limit.

**Workaround options:**
- Increase `MAX_OPEN_POSITIONS` to 35 in `.env`
- Reduce active symbols per strategy to 4 (8 + 16 = 24 ≤ 25)

### 23.9 Sigma Calibration Fix (July 2026)

LTPPoller computes ATR14 over 5-minute bars. `atr_to_annualised_vol()` assumes daily ATR. Without the `_5MIN_ATR_SCALE = sqrt(75)` correction, sigma ≈ 4% when true annualized volatility ≈ 28%, causing `find_delta_strike()` to place short strikes only ~1% OTM instead of the intended ~5-6% OTM.

All three sigma computations (credit spread, iron condor, `_get_iv_rank`) now apply this scale factor. After applying the fix, run:
```bash
docker exec -it falcon_api curl -X POST http://localhost:8000/api/v1/admin/reset-iv-history
```
This clears the corrupted IV rank history so correct values accumulate from the fix date forward.

### 23.10 Adverse vs Profit Exit Routing

The `_check_spread_exits` and `_check_condor_exits` methods classify each exit as adverse (breach, SL, regime shift, near-expiry, forced close) or profit (75%+ target). Only adverse exits populate `_exited_today` (which blocks same-day re-entry). Profit exits populate `_profit_closed_today`, which allows re-entry at DTE ≥ 14 — enabling two trades per expiry cycle on the same symbol if conditions remain good.

This distinction matters during code review: any new exit reason string must be added to `_adverse_kw` in `_check_spread_exits` if it should block re-entry, otherwise it will be treated as a profit close and re-entry will be allowed.

### 23.11 IV Rank History Rebuild Timeline

`iv_history:{SYMBOL}` accumulates one daily IV reading per market day. After clearing history (post-sigma fix), the IV rank gate (`≥ 0.30`) requires enough history to compute a meaningful percentile. With 30-40 trading days before go-live, the gate becomes meaningful within ~2 weeks and produces reliable percentile rankings by go-live.

---

## 24. F&O Trading Reference

### 24.1 NSE F&O Universe (41 Symbols)

| Tier | Symbols |
|------|---------|
| Tier 1 (liquid) | RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK, SBIN, BAJFINANCE, KOTAKBANK, AXISBANK, LT |
| Tier 2 | HINDUNILVR, ITC, WIPRO, HCLTECH, MARUTI, SUNPHARMA, M&M, BHARTIARTL, ADANIPORTS, ASIANPAINT |
| Tier 3 | TITAN, BAJAJ-AUTO, EICHERMOT, INDUSINDBK, DRREDDY, CIPLA, DIVISLAB, JSWSTEEL, HINDALCO, GRASIM |
| Tier 4 | TATACONSUM, APOLLOHOSP, NESTLEIND, TECHM, BPCL, ONGC, NTPC, POWERGRID, ULTRACEMCO, TATASTEEL |
| Tier 5 | COALINDIA |

> TATAMOTORS removed (Jun 2026) — replaced by M&M (Auto) and COALINDIA (Mining) for better sector diversity.

### 24.2 Lot Sizes

Lot sizes are fetched from live Zerodha instrument data every morning after auth and cached in Redis (`lot:{SYMBOL}`, 7-day TTL). Hardcoded fallbacks exist in `src/core/constants.py` for startup-before-auth scenarios.

Sample current lot sizes (may change with SEBI revisions):

| Symbol | Lot Size | Symbol | Lot Size |
|--------|----------|--------|----------|
| RELIANCE | 250 | SBIN | 1500 |
| TCS | 175 | KOTAKBANK | 2000 |
| INFY | 400 | NTPC | 1500 |
| HDFCBANK | 550 | CIPLA | 375 |

### 24.3 Sector Map

| Sector | Symbols |
|--------|---------|
| Energy | RELIANCE, BPCL, ONGC |
| IT | TCS, INFY, WIPRO, HCLTECH, TECHM |
| Banking | HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK, INDUSINDBK |
| NBFC | BAJFINANCE |
| Infrastructure | LT, ADANIPORTS |
| FMCG | HINDUNILVR, ITC, TATACONSUM, NESTLEIND |
| Auto | MARUTI, M&M, BAJAJ-AUTO, EICHERMOT |
| Pharma | SUNPHARMA, DRREDDY, CIPLA, DIVISLAB |
| Telecom | BHARTIARTL |
| Chemicals | ASIANPAINT, GRASIM |
| Consumer | TITAN |
| Healthcare | APOLLOHOSP |
| Metals | JSWSTEEL, HINDALCO, TATASTEEL |
| Power | NTPC, POWERGRID |
| Cement | ULTRACEMCO |
| Mining | COALINDIA |

### 24.4 Option Pricing Model

**Delta approximation for strike selection:**
```
d1 = [ln(S/K) + (r + σ²/2)×T] / (σ×√T)
delta_call =  Φ(d1)
delta_put  = Φ(d1) - 1

S = current price | K = strike | r = 0.065 | T = DTE/365
σ = annualised vol from ATR | Φ = standard normal CDF
```

**Premium estimation (paper mode, no live quotes):**
```python
base_premium = atr × √(dte/365) × otm_discount
# otm_discount: 1 interval=0.7, 2=0.5, 3=0.3
```

In live mode (or paper with kite token), actual market prices come from `kite.ltp()`.

### 24.5 Market Hours

| Time (IST) | Event |
|-----------|-------|
| 08:30 | Zerodha token refresh (automated headless auth) |
| 08:30 | Lot sizes refreshed from kite.instruments("NFO") |
| 09:00 | Pre-open session |
| 09:15 | Market opens — engine resets daily state, starts cycles |
| 09:15–15:20 | Normal trading — signal cycles every 60s |
| 15:20 | Square-off begins — all positions closed |
| 15:30 | Market closes — engine stops new cycles |
| 15:45 | Daily PnL report emailed |

---

*End of Falcon Quant Platform Documentation — v2.0*
