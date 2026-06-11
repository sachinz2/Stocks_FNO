# VOLUME 9

# GITHUB COPILOT MASTER PROMPT LIBRARY

Version 1.0

Purpose
Generate production-grade code using AI coding assistants.

Rules For Every Prompt
Use:
Python 3.12
FastAPI
SQLAlchemy 2.0
PostgreSQL
Redis
Docker
Dependency Injection
Repository Pattern
Service Layer Pattern
SOLID Principles
Type Hints
Pydantic V2
Pytest
Structured Logging
Alembic Migrations
Never use global variables.
Never write business logic inside API routes.
Every service must be testable.
Every module must include unit tests.

---

MASTER SYSTEM PROMPT
You are a senior quantitative trading software engineer.
Generate institutional-grade Python code.
Requirements:
* Python 3.12
* FastAPI
* PostgreSQL
* SQLAlchemy 2.0
* Dependency Injection
* Repository Pattern
* Service Layer Pattern
* Unit Tests
* Type Hints
* Pydantic Models
* Structured Logging
* Production Ready

Output:
1. Folder structure
2. Models
3. Repositories
4. Services
5. DTOs
6. Tests
7. Documentation

No placeholders.
No pseudo code.
Generate complete working code.

---

PROMPT 1
DATABASE FOUNDATION
Generate:
* SQLAlchemy models
* Alembic migrations
* Repository classes
Tables:
stocks, instruments, ohlc_data, indicators, signals, orders, positions, trades
Requirements:
* SQLAlchemy 2.0
* PostgreSQL
* Proper indexes
* Relationships
* Soft delete support
* Audit fields
* Repository pattern
Output complete code.

---

PROMPT 2
REPOSITORY PATTERN
Create a generic repository framework.
Requirements:
BaseRepository
create(), update(), delete(), get_by_id(), get_all(), filter(), paginate()
Support:
SQLAlchemy AsyncSession, Type Generics, Dependency Injection, Unit Tests
Provide complete implementation.

---

PROMPT 3
FASTAPI FOUNDATION
Create a production-grade FastAPI application.
Requirements:
Dependency Injection, JWT Authentication, Middleware, Exception Handling, Health Checks, Swagger, OpenAPI, Versioned APIs, Folder Structure, Routers, Services, Repositories, DTOs, Tests
Generate complete code.

---

PROMPT 4
MARKET DATA SERVICE
Build a market data ingestion service.
Responsibilities:
Historical Data Download, Live WebSocket Feed, OHLC Aggregation, Database Persistence
Requirements:
AsyncIO, Retry Logic, Reconnect Logic, Structured Logging, Metrics Collection
Output:
Complete implementation.

---

PROMPT 5
INDICATOR ENGINE
Build a technical indicator engine.
Indicators: EMA, RSI, ATR, VWAP, MACD
Requirements:
Incremental calculation, Avoid recalculation, Database persistence, Unit tests, Validation against TradingView
Generate complete implementation.

---

PROMPT 6
STRATEGY FRAMEWORK
Create a strategy plugin architecture.
Requirements:
StrategyBase abstract class (initialize(), generate_signal(), manage_position(), shutdown())
Dynamic loading, Strategy registration, Hot deployment support
Generate complete code.

---

PROMPT 7
VWAP STRATEGY
Implement VWAP mean reversion strategy.
Inputs: OHLC, VWAP, ATR
Rules: Price below VWAP by X ATR -> Generate BUY. Price above VWAP by X ATR -> Generate SELL.
Include: Entry, Exit, Stop Loss, Position Sizing, Backtest Support, Unit Tests

---

PROMPT 8
EMA CROSSOVER STRATEGY
Implement EMA20/EMA50 crossover strategy.
Requirements: Signal generation, Stop loss, Trailing stop, Risk validation, Backtesting support
Generate complete implementation.

---

PROMPT 9
BACKTEST ENGINE
Create an institutional-grade backtesting framework.
Requirements:
Event-driven architecture, Historical simulation, Order simulation, Slippage, Brokerage, Metrics (Sharpe Ratio, Profit Factor, Drawdown, CAGR, Win Rate)
Generate complete implementation.

---

PROMPT 10
PAPER TRADING ENGINE
Build a paper trading engine.
Requirements: Virtual broker, Order execution, Position tracking, PnL calculation, Reporting
Generate complete code.

---

PROMPT 11
ZERODHA BROKER ADAPTER
Build a broker adapter using Kite Connect.
Functions: authenticate(), refresh_token(), place_order(), cancel_order(), modify_order(), get_positions(), get_orders()
Requirements: Retry logic, Error handling, Rate limit handling, Logging, Unit Tests
Generate complete implementation.

---

PROMPT 12
RISK ENGINE
Create institutional-grade risk management.
Rules: Maximum Daily Loss, Maximum Open Positions, Maximum Exposure, Maximum Capital Allocation, Circuit Breaker, Kill Switch
Requirements: Trades blocked when risk violated, Audit logging, Unit tests
Generate complete implementation.

---

PROMPT 13
ORDER MANAGEMENT
Build an order management system.
Requirements: Order lifecycle, Order states, Retry handling, Broker synchronization, Audit trail
Generate complete code.

---

PROMPT 14
PORTFOLIO MANAGEMENT
Create portfolio management service.
Features: Position tracking, Exposure tracking, PnL, Margin utilization, Sector exposure
Generate complete implementation.

---

PROMPT 15
REPORTING ENGINE
Generate: Daily Reports, Weekly Reports, Monthly Reports, Strategy Reports
Metrics: Profit Factor, Drawdown, Sharpe Ratio, Expectancy
Generate complete implementation.

---

PROMPT 16
STREAMLIT DASHBOARD
Build trading dashboard.
Pages: Home, Trades, Positions, Signals, Strategies, Risk, PnL, System Health
Requirements: Auto Refresh, Responsive UI, Role Based Access
Generate complete code.

---

PROMPT 17
FEATURE STORE
Build ML feature store.
Features: Returns, Volume Delta, ATR, RSI, VWAP Distance, Open Interest Change
Requirements: Feature versioning, Feature validation
Generate complete implementation.

---

PROMPT 18
MODEL TRAINER
Create ML training framework.
Models: XGBoost, LightGBM, Random Forest
Requirements: Cross Validation, Walk Forward Testing, Feature Importance, Model Registry
Generate complete implementation.

---

PROMPT 19
PREDICTION ENGINE
Create prediction service.
Requirements: Load latest model, Generate prediction, Store prediction, Serve prediction API
Generate complete implementation.

---

PROMPT 20
DOCKERIZATION
Generate: Dockerfile, docker-compose.yml, Environment Variables, Health Checks, Production Configuration
Generate complete implementation.

---

PROMPT 21
CI/CD
Generate GitHub Actions pipeline.
Requirements: Lint, Tests, Build, Security Scan, Docker Build, Deployment, Health Checks, Rollback
Generate complete implementation.

---

PROMPT 22
MONITORING
Generate monitoring stack.
Prometheus, Grafana, Alert Manager
Metrics: CPU, RAM, PnL, Orders, Trades, API Latency
Generate complete implementation.

---

PROMPT 23
AWS DEPLOYMENT
Generate infrastructure.
Services: EC2, RDS, S3, CloudWatch, Route53, Security Groups, IAM, Docker Deployment
Generate Terraform code.

---

PROMPT 24
INTEGRATION TEST SUITE
Create end-to-end test suite.
Validate: Signal Generation, Risk Validation, Order Placement, Position Updates, PnL Updates, Reporting
Generate complete implementation.

---

PROMPT 25
PRODUCTION READINESS REVIEW
Review entire codebase.
Validate: Security, Performance, Scalability, Reliability, SOLID Compliance, Dependency Injection, Test Coverage
Generate improvement report.