from enum import Enum


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"
    HOLD = "HOLD"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    REJECTED_BY_RISK = "REJECTED_BY_RISK"
    FAILED = "FAILED"
    # Added 2026-08-21: real, actively-used order_status string values written
    # by src/orders/order_manager.py (the DB column itself is a plain string,
    # not backed by this enum) that were missing here, making this reference
    # incomplete/misleading.
    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # place_order() timed out; awaiting reconciliation retry
    EXPIRED = "EXPIRED"  # reconciliation retries exhausted with no verifiable broker fill


class TimeFrame(str, Enum):
    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_30 = "30m"
    HOUR_1 = "1h"
    DAY_1 = "1d"


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class MarketRegime(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE_BOUND = "RANGE_BOUND"
    VOLATILE = "VOLATILE"
    UNKNOWN = "UNKNOWN"
