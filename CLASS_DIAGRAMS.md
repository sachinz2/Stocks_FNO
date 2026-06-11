# VOLUME 5

# CLASS DIAGRAMS + EXACT PYTHON PACKAGE STRUCTURE

PROJECT ROOT
src/
  market_data/
  indicators/
  strategies/
  backtesting/
  paper_trading/
  live_trading/
  risk/
  portfolio/
  brokers/
  reporting/
  analytics/
  ml/
  api/
  dashboard/
  database/
  core/
tests/

---

# CORE PACKAGE
core/
  config.py
  constants.py
  exceptions.py
  logger.py
  enums.py
  utils.py
  scheduler.py

---

# DATABASE PACKAGE
database/
  connection.py
  session.py
  base.py
  repositories/
  models/
  migrations/

---

# DATABASE MODELS
models/
  stock.py
  instrument.py
  ohlc.py
  indicator.py
  signal.py
  order.py
  position.py
  trade.py
  risk_rule.py
  backtest_run.py
  backtest_result.py

---

# MARKET DATA PACKAGE
market_data/
  market_data_service.py
  historical_loader.py
  websocket_client.py
  tick_processor.py
  ohlc_builder.py

Class: MarketDataService
Methods:
* load_historical_data()
* subscribe_live_feed()
* save_ticks()
* save_ohlc()

---

# INDICATOR PACKAGE
IndicatorEngine
Methods:
* calculate_ema()
* calculate_rsi()
* calculate_atr()
* calculate_vwap()
* calculate_macd()
* update_indicators()

---

# STRATEGY PACKAGE
Base Class: StrategyBase
Methods:
* initialize()
* generate_signal()
* manage_position()
* shutdown()

VWAPStrategy, EMACrossoverStrategy, ORBStrategy, VolumeBreakoutStrategy All inherit: StrategyBase

---

# SIGNAL ENGINE
SignalEngine
Methods:
* generate_signals()
* validate_signal()
* store_signal()

---

# BACKTEST ENGINE
BacktestEngine
Methods:
* run()
* calculate_metrics()
* generate_report()

BacktestMetrics
Methods:
* sharpe_ratio()
* profit_factor()
* max_drawdown()
* cagr()

---

# PAPER TRADING
PaperBroker
Methods:
* place_order()
* cancel_order()
* modify_order()
* get_positions()

---

# LIVE TRADING
LiveTradingEngine
Methods:
* start()
* stop()
* process_signal()
* execute_order()

---

# RISK PACKAGE
RiskManager
Methods:
* validate_trade()
* validate_portfolio()
* validate_daily_loss()
* validate_exposure()
* kill_switch()

RiskRule
Methods:
* evaluate()

---

# ORDER PACKAGE
OrderManager
Methods:
* place_order()
* cancel_order()
* modify_order()
* track_order()

---

# BROKER PACKAGE
AbstractBroker
Methods:
* place_order()
* cancel_order()
* modify_order()
* get_positions()
* get_orders()

ZerodhaBroker Implements: AbstractBroker

Future: DhanBroker, UpstoxBroker, AngelBroker

---

# PORTFOLIO PACKAGE
PortfolioManager
Methods:
* update_position()
* calculate_pnl()
* calculate_exposure()
* calculate_margin()

---

# REPORTING PACKAGE
ReportGenerator
Methods:
* daily_report()
* weekly_report()
* monthly_report()
* strategy_report()

---

# ML PACKAGE
FeatureGenerator
* generate_features()

ModelTrainer
* train()
* save_model()
* load_model()

PredictionService
* predict()

---

# API PACKAGE
FastAPI
routers/
  stocks_router.py
  orders_router.py
  positions_router.py
  signals_router.py
  risk_router.py
  backtest_router.py
  strategy_router.py

services/
dto/
middleware/

---

# DASHBOARD
Streamlit
Pages: Home, Positions, Orders, Trades, Strategies, Risk, PnL, Logs, Settings

---

# Dependency Injection
Every service must be injected.
Never instantiate inside classes.
Use: constructor injection

Example:
```python
class OrderManager:
    def __init__(
        self,
        broker,
        risk_manager,
        repository
    ):
        self.broker = broker
        self.risk_manager = risk_manager
        self.repository = repository
```

---

# Mandatory Design Rules
* SOLID
* Repository Pattern
* Service Layer Pattern
* Dependency Injection
* Factory Pattern
* Strategy Pattern
* No business logic inside API routes
* No direct database access inside services
* All database access via repositories