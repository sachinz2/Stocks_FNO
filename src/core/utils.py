from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    now = now_ist()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def is_square_off_time() -> bool:
    """True when within the auto square-off window (15:20–15:30 IST)."""
    now = now_ist()
    square_off = now.replace(hour=15, minute=20, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return square_off <= now <= market_close


def format_inr(amount: float) -> str:
    return f"₹{amount:,.2f}"


def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100


def round2(value: float) -> float:
    return round(value, 2)
