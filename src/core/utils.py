import calendar
import logging
import math
from datetime import date, datetime, timedelta
import pytz

from src.core.constants import FNO_LOT_SIZES, FNO_STRIKE_INTERVALS

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

# Hardcoded fallback — used only when exchange_calendars is not installed.
# exchange_calendars (pip install exchange-calendars) is the preferred source
# and is updated automatically whenever you run pip install --upgrade.
_NSE_HOLIDAYS_FALLBACK: frozenset = frozenset({
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
    date(2026, 2, 26),   # Maha Shivaratri
    date(2026, 3, 20),   # Holi
    date(2026, 3, 30),   # Id-Ul-Fitr / Ramzan Eid (moon-sighting dependent)
    date(2026, 4, 2),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 6, 7),    # Eid ul-Adha / Bakri Eid (approx)
    date(2026, 6, 26),   # Muharram (Ashura)
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 27),   # Janmashtami
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra / Vijaya Dashami (approx)
    date(2026, 11, 14),  # Diwali Laxmi Pujan (approx)
    date(2026, 11, 15),  # Diwali Balipratipada (approx)
    date(2026, 11, 25),  # Guru Nanak Jayanti (approx)
    date(2026, 12, 25),  # Christmas
})


def _init_nse_holiday_checker():
    """
    Tries to load NSE trading calendar from exchange_calendars (dynamic, auto-updated).
    Falls back to the hardcoded _NSE_HOLIDAYS_FALLBACK set if the package is absent.

    Returns a callable:  is_nse_holiday(d: date) -> bool
    """
    try:
        import exchange_calendars as ecals
        import pandas as pd

        cal = ecals.get_calendar("XNSE")
        logger.info("NSE holiday calendar loaded from exchange_calendars (dynamic)")

        def _check(d: date) -> bool:
            try:
                return not cal.is_session(pd.Timestamp(d))
            except Exception:
                return d in _NSE_HOLIDAYS_FALLBACK

        return _check

    except ImportError:
        logger.warning(
            "exchange_calendars not installed — run: pip install exchange-calendars  "
            "Using hardcoded NSE holiday list as fallback."
        )
    except Exception as e:
        logger.warning(f"exchange_calendars init failed ({e}) — using hardcoded NSE holiday list")

    return lambda d: d in _NSE_HOLIDAYS_FALLBACK


# Initialised once at import time — zero overhead per is_market_open() call.
_is_nse_holiday = _init_nse_holiday_checker()


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


def estimate_option_premium(
    atr: float,
    dte: int,
    otm_intervals: int = 0,
    underlying_price: float = 0.0,
    strike: float = 0.0,
    option_type: str = "PE",
) -> float:
    """
    Estimate option premium for paper trading.

    When underlying_price and strike are supplied, uses Black-Scholes for
    accurate pricing — this correctly includes intrinsic value for ITM
    options, which is critical for realistic exit PnL after a breach.

    Falls back to the ATR-based heuristic when strike/price are unavailable
    (e.g. when computing entry credits before strikes are chosen).

    ATR-based fallback model:
      ATM premium at 20 DTE  ≈  ATR × 4  × sqrt(DTE/20)
      Each interval OTM discounts by 25%
    """
    dte = max(dte, 1)

    if underlying_price > 0 and strike > 0:
        from src.market_data.option_chain import bs_price
        T = dte / 365.0
        if atr > 0 and underlying_price > 0:
            daily_vol = atr / underlying_price
            sigma = daily_vol * math.sqrt(252)
            sigma = max(0.05, min(sigma, 2.0))
        else:
            sigma = 0.30
        price = bs_price(underlying_price, strike, T, sigma, option_type)
        return max(round(price, 2), 0.05)

    # ATR-based fallback
    if atr <= 0:
        return 0.01
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
    if _is_nse_holiday(now.date()):
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
