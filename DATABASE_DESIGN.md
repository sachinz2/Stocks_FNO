# VOLUME 3

# DATABASE DESIGN

Database:
Mysql

---

## TABLE 1: stocks
* id BIGSERIAL PK
* symbol VARCHAR(20)
* name VARCHAR(200)
* sector VARCHAR(100)
* fno_enabled BOOLEAN
* active BOOLEAN
* created_at TIMESTAMP
* updated_at TIMESTAMP
* INDEX: symbol

---

## TABLE 2: instruments
* id BIGSERIAL PK
* exchange VARCHAR(20)
* instrument_token BIGINT
* symbol VARCHAR(50)
* expiry DATE
* lot_size INTEGER
* strike NUMERIC
* instrument_type VARCHAR(20)

---

## TABLE 3: ohlc_data
* id BIGSERIAL PK
* symbol VARCHAR(50)
* timeframe VARCHAR(10)
* timestamp TIMESTAMP
* open NUMERIC
* high NUMERIC
* low NUMERIC
* close NUMERIC
* volume BIGINT
* open_interest BIGINT
* INDEX: (symbol,timestamp)

---

## TABLE 4: ticks
* id BIGSERIAL PK
* symbol VARCHAR(50)
* timestamp TIMESTAMP
* last_price NUMERIC
* bid_price NUMERIC
* ask_price NUMERIC
* volume BIGINT

---

## TABLE 5: indicators
* id BIGSERIAL PK
* symbol VARCHAR(50)
* timestamp TIMESTAMP
* ema20 NUMERIC
* ema50 NUMERIC
* ema200 NUMERIC
* rsi14 NUMERIC
* atr14 NUMERIC
* vwap NUMERIC
* macd NUMERIC
* macd_signal NUMERIC

---

## TABLE 6: signals
* id BIGSERIAL PK
* strategy_name VARCHAR(100)
* symbol VARCHAR(50)
* signal VARCHAR(20)
* confidence NUMERIC
* timestamp TIMESTAMP
* status VARCHAR(20)

---

## TABLE 7: orders
* id BIGSERIAL PK
* broker_order_id VARCHAR(100)
* symbol VARCHAR(50)
* side VARCHAR(10)
* quantity INTEGER
* price NUMERIC
* status VARCHAR(50)
* timestamp TIMESTAMP

---

## TABLE 8: positions
* id BIGSERIAL PK
* symbol VARCHAR(50)
* quantity INTEGER
* avg_price NUMERIC
* market_price NUMERIC
* unrealized_pnl NUMERIC
* realized_pnl NUMERIC
* updated_at TIMESTAMP

---

## TABLE 9: trades
* id BIGSERIAL PK
* strategy_name VARCHAR(100)
* symbol VARCHAR(50)
* entry_price NUMERIC
* exit_price NUMERIC
* quantity INTEGER
* pnl NUMERIC
* entry_time TIMESTAMP
* exit_time TIMESTAMP

---

## TABLE 10: strategy_parameters
* id BIGSERIAL PK
* strategy_name VARCHAR(100)
* parameter_name VARCHAR(100)
* parameter_value VARCHAR(500)

---

## TABLE 11: backtest_runs
* id BIGSERIAL PK
* strategy_name VARCHAR(100)
* start_date DATE
* end_date DATE
* status VARCHAR(50)
* started_at TIMESTAMP
* completed_at TIMESTAMP

---

## TABLE 12: backtest_results
* id BIGSERIAL PK
* run_id BIGINT
* net_profit NUMERIC
* drawdown NUMERIC
* win_rate NUMERIC
* profit_factor NUMERIC
* sharpe_ratio NUMERIC

---

## TABLE 13: risk_rules
* id BIGSERIAL PK
* rule_name VARCHAR(100)
* rule_value NUMERIC
* active BOOLEAN

---

## TABLE 14: risk_events
* id BIGSERIAL PK
* rule_name VARCHAR(100)
* symbol VARCHAR(50)
* description TEXT
* timestamp TIMESTAMP

---

## TABLE 15: daily_pnl
* id BIGSERIAL PK
* trade_date DATE
* realized_pnl NUMERIC
* unrealized_pnl NUMERIC

---

## TABLE 16: monthly_pnl
* id BIGSERIAL PK
* month DATE
* net_pnl NUMERIC
* max_drawdown NUMERIC

---

## TABLE 17: audit_logs
* id BIGSERIAL PK
* service_name VARCHAR(100)
* action VARCHAR(100)
* payload JSONB
* timestamp TIMESTAMP

---

## TABLE 18: system_logs
* id BIGSERIAL PK
* service_name VARCHAR(100)
* level VARCHAR(20)
* message TEXT
* timestamp TIMESTAMP

---

## TABLE 19: notifications
* id BIGSERIAL PK
* channel VARCHAR(20)
* message TEXT
* status VARCHAR(20)
* timestamp TIMESTAMP

---

## TABLE 20: deployments
* id BIGSERIAL PK
* version VARCHAR(50)
* environment VARCHAR(50)
* deployed_at TIMESTAMP
* status VARCHAR(50)

---

# Future Tables:
* options_chain
* option_greeks
* fii_dii_data
* news_sentiment
* model_registry
* ml_predictions
* portfolio_allocations
* strategy_performance
* execution_metrics
* broker_accounts