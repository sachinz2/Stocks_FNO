# VOLUME 6

# mySQL DDL + MIGRATIONS + INDEXING + PARTITIONING STRATEGY

Database Engine: mysql
Timezone: UTC
Naming Convention: snake_case
Primary Keys: BIGSERIAL
Foreign Keys: ON DELETE RESTRICT
Soft Delete: deleted_at TIMESTAMP NULL
Audit Fields: created_at, updated_at

---

# DATABASE SCHEMAS
* core
* market
* trading
* analytics
* risk
* ml
* audit

---

# CORE.STOCKS
```sql
CREATE TABLE core.stocks (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(30) UNIQUE NOT NULL,
    company_name VARCHAR(200),
    sector VARCHAR(100),
    exchange VARCHAR(20),
    fno_enabled BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_stocks_symbol ON core.stocks(symbol);
```

---

# CORE.INSTRUMENTS
```sql
CREATE TABLE core.instruments (
    id BIGSERIAL PRIMARY KEY,
    stock_id BIGINT REFERENCES core.stocks(id),
    instrument_token BIGINT,
    expiry_date DATE,
    lot_size INTEGER,
    instrument_type VARCHAR(20),
    strike NUMERIC(18,4),
    option_type VARCHAR(10)
);

CREATE INDEX idx_instruments_token ON core.instruments(instrument_token);
```

---

# MARKET.OHLC_DATA
CRITICAL TABLE
Expected Size: 50 stocks, 1 minute candles, 5 years
Approx: 60+ million rows
Partition Required

```sql
CREATE TABLE market.ohlc_data (
    id BIGSERIAL,
    symbol VARCHAR(30),
    timeframe VARCHAR(10),
    candle_timestamp TIMESTAMP,
    open NUMERIC(18,4),
    high NUMERIC(18,4),
    low NUMERIC(18,4),
    close NUMERIC(18,4),
    volume BIGINT,
    open_interest BIGINT
) PARTITION BY RANGE (candle_timestamp);
```

---

# MONTHLY PARTITIONS
* market.ohlc_data_2026_01
* market.ohlc_data_2026_02
* market.ohlc_data_2026_03
...

---

# INDEXES
```sql
CREATE INDEX idx_ohlc_symbol_time ON market.ohlc_data (symbol, candle_timestamp DESC);
CREATE INDEX idx_ohlc_time ON market.ohlc_data (candle_timestamp DESC);
```

---

# MARKET.TICKS
Tick data grows extremely fast.
Retention: 30 days only.

```sql
CREATE TABLE market.ticks (
    id BIGSERIAL,
    symbol VARCHAR(30),
    tick_timestamp TIMESTAMP,
    last_price NUMERIC(18,4),
    bid_price NUMERIC(18,4),
    ask_price NUMERIC(18,4),
    volume BIGINT
) PARTITION BY RANGE (tick_timestamp);
```

---

# INDICATOR TABLE
Never recalculate indicators repeatedly. Store them.

```sql
CREATE TABLE analytics.indicators (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(30),
    timestamp TIMESTAMP,
    ema20 NUMERIC,
    ema50 NUMERIC,
    ema200 NUMERIC,
    rsi14 NUMERIC,
    atr14 NUMERIC,
    vwap NUMERIC,
    macd NUMERIC,
    macd_signal NUMERIC
);

CREATE INDEX idx_indicators_symbol_time ON analytics.indicators (symbol, timestamp);
```

---

# SIGNALS
```sql
CREATE TABLE trading.signals (
    id BIGSERIAL PRIMARY KEY,
    strategy_name VARCHAR(100),
    symbol VARCHAR(30),
    signal_type VARCHAR(20),
    confidence NUMERIC(6,4),
    generated_at TIMESTAMP,
    status VARCHAR(20)
);
```

---

# ORDERS
```sql
CREATE TABLE trading.orders (
    id BIGSERIAL PRIMARY KEY,
    broker_order_id VARCHAR(100),
    symbol VARCHAR(30),
    side VARCHAR(10),
    quantity INTEGER,
    price NUMERIC,
    order_status VARCHAR(50),
    created_at TIMESTAMP
);

-- INDEX: broker_order_id, order_status
```

---

# POSITIONS
```sql
CREATE TABLE trading.positions (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(30),
    quantity INTEGER,
    avg_price NUMERIC,
    market_price NUMERIC,
    unrealized_pnl NUMERIC,
    realized_pnl NUMERIC,
    updated_at TIMESTAMP
);
```

---

# TRADES
```sql
CREATE TABLE trading.trades (
    id BIGSERIAL PRIMARY KEY,
    strategy_name VARCHAR(100),
    symbol VARCHAR(30),
    entry_price NUMERIC,
    exit_price NUMERIC,
    quantity INTEGER,
    pnl NUMERIC,
    entry_time TIMESTAMP,
    exit_time TIMESTAMP
);

-- INDEX: symbol, strategy_name, entry_time
```

---

# BACKTEST TABLES
* backtest_runs
* backtest_results
* backtest_trades
* backtest_equity_curve
* backtest_drawdowns

---

# RISK TABLES
* risk_rules
* risk_events
* risk_violations
* daily_risk_metrics

---

# ML TABLES
* feature_store
* training_runs
* model_registry
* model_metrics
* predictions

---

# AUDIT TABLES
* audit_logs
* system_logs
* api_logs
* deployment_logs

---

# MIGRATION STRATEGY
Tool: Alembic
Command:
* `alembic revision --autogenerate`
* `alembic upgrade head`

Rules:
* Never modify production tables manually.
* Every schema change must have migration.

---

# RETENTION POLICY
* Ticks: 30 days
* OHLC: Forever
* Signals: 2 years
* Orders: Forever
* Trades: Forever
* Logs: 180 days

---

# BACKUP POLICY
Daily: Database dump
Weekly: Full backup
Monthly: Archive backup
Store: AWS S3