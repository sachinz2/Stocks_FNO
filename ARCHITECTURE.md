# VOLUME 2

# SYSTEM ARCHITECTURE

## High-Level Architecture
Market Data Layer
↓
Data Processing Layer
↓
Strategy Layer
↓
Risk Layer
↓
Order Layer
↓
Broker Layer
↓
Analytics Layer

---

# Services

## 1. market-data-service
Responsibilities:
* Fetch ticks
* Fetch OHLC
* Store data
* Retry failed requests

Technology:
Python, AsyncIO, WebSocket

## 2. indicator-service
Responsibilities:
* EMA
* RSI
* ATR
* VWAP
* MACD

Output:
Indicator tables

## 3. strategy-service
Responsibilities:
* Evaluate strategies
* Generate signals

Input:
Market Data, Indicators

Output:
Signals (BUY, SELL, EXIT, HOLD)

## 4. risk-service
Responsibilities:
Validate:
* Capital available
* Daily loss limit
* Open positions
* Exposure

Risk Rules:
Reject trade if violated.

## 5. order-service
Responsibilities:
* Place orders
* Cancel orders
* Modify orders

Features:
Retry, Audit logging, Order tracking

## 6. broker-service
Responsibilities:
Zerodha API integration

Functions:
place_order(), cancel_order(), modify_order(), fetch_positions(), fetch_orders()

## 7. portfolio-service
Responsibilities:
Track:
* Open positions
* Closed trades
* Daily PnL
* Monthly PnL

## 8. reporting-service
Generate:
Daily reports, Weekly reports, Monthly reports, Strategy reports

## 9. ml-service
Future phase
Responsibilities:
Feature generation, Model training, Prediction serving

## 10. dashboard-service
Technology:
Streamlit

Views:
Positions, Trades, Signals, Performance, Risk, Logs

---

# Internal Event Flow
Market Data
↓
Indicator Engine
↓
Strategy Engine
↓
Signal Generated
↓
Risk Validation
↓
Order Placement
↓
Broker
↓
Position Update
↓
PnL Update
↓
Dashboard Update

---

# Project Structure
src/
  market_data/
  indicators/
  strategies/
  risk/
  orders/
  brokers/
  portfolio/
  analytics/
  ml/
  dashboard/
  database/
tests/
docker/
configs/
scripts/
docs/
logs/

---

# Strategy Plugin Contract
Every strategy must implement:
* initialize()
* generate_signal()
* manage_position()
* shutdown()

Return:
BUY, SELL, EXIT, HOLD

Each strategy must be independently deployable.
