import calendar
from datetime import datetime, timedelta
import pytz

from src.core.constants import FNO_LOT_SIZES, FNO_STRIKE_INTERVALS

IST = pytz.timezone("Asia/Kolkata")

_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def get_lot_size(symbol: str) -> int:
    """Return the NSE lot size for a symbol (defaults to 1 if unknown)."""
    return FNO_LOT_SIZES.get(symbol, 1)


def get_atm_strike(price: float, symbol: str) -> int:
    """Round the underlying price to the nearest valid strike for this symbol."""
    interval = FNO_STRIKE_INTERVALS.get(symbol, 50)
    return int(round(price / interval) * interval)


def _last_thursday(year: int, month: int) -> datetime:
    """Return the last Thursday of the given month (NSE monthly expiry day)."""
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day)
    # Walk backwards to find Thursday (weekday 3)
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d


def get_near_month_expiry() -> datetime:
    """
    Return the near-month NSE option expiry (last Thursday of the month).
    Rolls to next month if fewer than 3 calendar days remain.
    """
    today = datetime.now(IST).replace(tzinfo=None)
    expiry = _last_thursday(today.year, today.month)
    if (expiry - today).days < 3:
        # Roll to next month
        if today.month == 12:
            expiry = _last_thursday(today.year + 1, 1)
        else:
            expiry = _last_thursday(today.year, today.month + 1)
    return expiry


def build_option_symbol(symbol: str, strike: int, option_type: str, expiry: datetime = None) -> str:
    """
    Build the NSE/Zerodha tradingsymbol for a stock option.
    Format: SYMBOL + YY + MON + STRIKE + TYPE
    Example: HDFCBANK25JUL800CE
    option_type must be 'CE' or 'PE'.
    """
    if expiry is None:
        expiry = get_near_month_expiry()
    yy = expiry.strftime("%y")
    mon = _MONTH_ABBR[expiry.month]
    return f"{symbol}{yy}{mon}{strike}{option_type}"


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
