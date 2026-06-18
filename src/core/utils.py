import calendar
import math
from datetime import date, datetime, timedelta
import pytz

from src.core.constants import FNO_LOT_SIZES, FNO_STRIKE_INTERVALS

IST = pytz.timezone("Asia/Kolkata")

_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

# NSE trading holidays. Verify against nseindia.com/market-data/market-holidays each year.
# Missing a holiday is non-fatal (no data returns → no signals fire), but adds log noise.
_NSE_HOLIDAYS: frozenset = frozenset({
    # 2025
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Eid)
    date(2025, 4, 10),   # Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 10, 2),   # Gandhi Jayanti / Dussehra
    date(2025, 10, 21),  # Diwali Laxmi Pujan
    date(2025, 10, 22),  # Diwali Balipratipada
    date(2025, 11, 5),   # Guru Nanak Jayanti
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Maha Shivaratri (approx)
    date(2026, 3, 20),   # Holi (approx)
    date(2026, 3, 30),   # Id-Ul-Fitr (approx — moon-sighting dependent)
    date(2026, 4, 2),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 11, 14),  # Diwali Laxmi Pujan (approx)
    date(2026, 11, 15),  # Diwali Balipratipada (approx)
    date(2026, 12, 25),  # Christmas
})


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
    while d.weekday() != 3:
        d -= timedelta(days=1)
    return d


def get_near_month_expiry() -> datetime:
    """
    Return the near-month NSE option expiry (last Thursday of the month).
    Rolls to next month if fewer than 4 calendar days remain — avoids
    the illiquid final 4 days where spreads widen and theta decay accelerates.
    """
    today = datetime.now(IST).replace(tzinfo=None)
    expiry = _last_thursday(today.year, today.month)
    if (expiry - today).days < 4:
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


def estimate_option_premium(atr: float, dte: int, otm_intervals: int = 0) -> float:
    """
    Estimate option premium for paper trading — DTE-aware and moneyness-aware.

    Model:
      ATM premium at 20 DTE  ≈  ATR × 4
      Scales with sqrt(DTE / 20)  — theta decays roughly proportional to sqrt(time)
      Each interval away from ATM discounts by 25%  (OTM options lose value quickly)

    Examples at ATR=10, DTE=20:
      ATM  (0 intervals): 10 × 4 × 1.00 = Rs 40.00
      1 OTM:              10 × 4 × 0.75 = Rs 30.00
      2 OTM:              10 × 4 × 0.56 = Rs 22.50
      3 OTM:              10 × 4 × 0.42 = Rs 16.88

    At DTE=7 (with ATR=10, ATM):
      10 × 4 × sqrt(7/20) = Rs 23.66  (realistic near-expiry decay)
    """
    if atr <= 0:
        return 0.01
    dte = max(dte, 1)
    dte_factor = math.sqrt(dte / 20.0)
    atm_premium = atr * 4.0 * dte_factor
    otm_discount = 0.75 ** max(otm_intervals, 0)
    return max(round(atm_premium * otm_discount, 2), 0.05)


def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    now = now_ist()
    if now.weekday() >= 5:
        return False
    if now.date() in _NSE_HOLIDAYS:
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
