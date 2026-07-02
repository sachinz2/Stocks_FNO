"""
Option chain utilities:
  1. Black-Scholes pricing, IV calculation, delta computation
  2. IV Rank per symbol (stored as daily history in Redis)
  3. India VIX fetching via Zerodha (market-wide IV proxy)
  4. Real option quote fetching via Zerodha kite.ltp()
  5. Delta-based strike selection (~0.20 delta for short legs)

In paper mode or when Zerodha is unavailable, all functions degrade
gracefully — returning None / falling back to ATR-based estimates.
"""
import json
import logging
import math
from datetime import date
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_RISK_FREE_RATE = 0.065   # 6.5% RBI repo rate proxy
_IV_HISTORY_KEY = "iv_history:{symbol}"  # Redis key template
_IV_HISTORY_MAX = 252     # Trading days in a year
_VIX_REDIS_KEY = "market:india_vix"


# ── Black-Scholes core ────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf — avoids scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def bs_price(S: float, K: float, T: float, sigma: float, option_type: str = "CE") -> float:
    """Black-Scholes theoretical option price."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(0.0, (S - K) if option_type == "CE" else (K - S))
    r = _RISK_FREE_RATE
    d1 = _bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, sigma: float, option_type: str = "CE") -> float:
    """Black-Scholes delta."""
    if T <= 0 or sigma <= 0:
        return 1.0 if (option_type == "CE" and S > K) else 0.0
    r = _RISK_FREE_RATE
    d1 = _bs_d1(S, K, T, r, sigma)
    if option_type == "CE":
        return _norm_cdf(d1)
    else:
        return _norm_cdf(d1) - 1.0


def implied_vol(market_price: float, S: float, K: float, T: float, option_type: str = "CE") -> Optional[float]:
    """
    Newton-Raphson implied volatility from market price.
    Returns None if the price is below intrinsic value (can't solve).
    """
    intrinsic = max(0.0, (S - K) if option_type == "CE" else (K - S))
    if market_price < intrinsic or market_price <= 0 or T <= 0:
        return None

    r = _RISK_FREE_RATE
    sigma = 0.30  # initial guess
    for _ in range(100):
        price = bs_price(S, K, T, sigma, option_type)
        d1 = _bs_d1(S, K, T, r, sigma)
        vega = S * _norm_pdf(d1) * math.sqrt(T)
        if vega < 1e-10:
            break
        diff = price - market_price
        if abs(diff) < 0.001:
            break
        sigma -= diff / vega
        sigma = max(0.01, min(sigma, 5.0))
    return round(sigma, 4)


def atr_to_annualised_vol(atr: float, underlying_price: float) -> float:
    """Convert ATR (daily range proxy) to annualised vol for BS calculations."""
    daily_vol = (atr / underlying_price) if underlying_price > 0 else 0.01
    return daily_vol * math.sqrt(252)


# ── IV Rank ───────────────────────────────────────────────────────────────────

async def update_iv_history(symbol: str, current_iv: float, redis) -> None:
    """Append today's IV to the rolling 252-day history in Redis."""
    key = _IV_HISTORY_KEY.format(symbol=symbol)
    try:
        raw = await redis.get(key)
        history: List[Dict] = json.loads(raw) if raw else []
        today = date.today().isoformat()
        # One entry per day — overwrite if already exists
        if history and history[-1].get("d") == today:
            history[-1]["iv"] = current_iv
        else:
            history.append({"d": today, "iv": current_iv})
        if len(history) > _IV_HISTORY_MAX:
            history = history[-_IV_HISTORY_MAX:]
        await redis.set(key, json.dumps(history))
    except Exception as e:
        logger.debug(f"IV history update failed [{symbol}]: {e}")


async def get_iv_rank(symbol: str, redis) -> Optional[float]:
    """
    Returns IV Rank ∈ [0, 1] for a symbol.
    IV Rank = (current_iv - 52w_low) / (52w_high - 52w_low)
    Returns None if history is too short (< 20 days) to be meaningful.
    """
    key = _IV_HISTORY_KEY.format(symbol=symbol)
    try:
        raw = await redis.get(key)
        if not raw:
            return None
        history = json.loads(raw)
        if len(history) < 20:
            return None
        ivs = [h["iv"] for h in history if h.get("iv", 0) > 0]
        if not ivs:
            return None
        current = ivs[-1]
        lo, hi = min(ivs), max(ivs)
        if hi <= lo:
            return 0.5
        return round((current - lo) / (hi - lo), 3)
    except Exception as e:
        logger.debug(f"IV rank fetch failed [{symbol}]: {e}")
        return None


async def get_india_vix(redis) -> Optional[float]:
    """Return cached India VIX from Redis (written by fetch_and_cache_vix())."""
    try:
        raw = await redis.get(_VIX_REDIS_KEY)
        return float(raw) if raw else None
    except Exception:
        return None


async def fetch_and_cache_vix(kite, redis) -> Optional[float]:
    """
    Fetch India VIX from Zerodha and cache it in Redis.
    Call once at startup and then every 5 minutes via scheduler.
    """
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: kite.ltp(["NSE:INDIA VIX"])
        )
        vix = data.get("NSE:INDIA VIX", {}).get("last_price")
        if vix:
            await redis.set(_VIX_REDIS_KEY, str(vix), ex=600)  # 10-min TTL
            logger.info(f"India VIX: {vix:.2f}")
            return float(vix)
    except Exception as e:
        logger.debug(f"VIX fetch failed: {e}")
    return None


def vix_allows_selling(vix: Optional[float]) -> bool:
    """
    Selling options is attractive only when IV is elevated (premium is rich).
    VIX > 14: reasonable to sell   VIX < 11: premium too cheap, skip
    If VIX is unknown (None), allow the trade (fail-open for paper mode).
    """
    if vix is None:
        return True
    return vix >= 14.0


def iv_rank_allows_selling(iv_rank: Optional[float]) -> bool:
    """
    Per-symbol IV rank gate.
    Rank ≥ 0.30 (i.e., IV is in the top 70% of its year range) → ok to sell.
    If rank is unknown, allow the trade (not enough history yet).
    """
    if iv_rank is None:
        return True
    return iv_rank >= 0.30


# ── Real option quotes ────────────────────────────────────────────────────────

async def get_option_quote(contract: str, kite, redis) -> Optional[float]:
    """
    Fetch real LTP for an option contract from Zerodha.

    Priority order:
      1. optltp:{contract}  — written every 5 s by ZerodhaLTPPoller when the
                              contract is registered as an active position. This
                              is the freshest source; TTL = 15 s so stale data
                              auto-expires if the poller stops.
      2. optq:{contract}    — 30-second on-demand cache from a previous kite.ltp()
                              call (used before the position was registered, or as
                              a fallback when the live cache has expired).
      3. kite.ltp() live    — fresh REST call; result stored in optq cache.

    contract: NSE F&O symbol, e.g. "BPCL26JUL315CE"
    Returns LTP (float) or None.
    """
    if redis is not None:
        # 1. Live 5-second cache (written by ZerodhaLTPPoller for active positions)
        try:
            live = await redis.get(f"optltp:{contract}")
            if live:
                return float(live)
        except Exception:
            pass

        # 2. On-demand 30-second cache
        try:
            cached = await redis.get(f"optq:{contract}")
            if cached:
                return float(cached)
        except Exception:
            pass

    if kite is None:
        return None

    # 3. Fresh kite.ltp() REST call
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        nfo_sym = f"NFO:{contract}"
        data = await loop.run_in_executor(None, lambda: kite.ltp([nfo_sym]))
        ltp = data.get(nfo_sym, {}).get("last_price")
        if ltp and float(ltp) > 0:
            ltp = float(ltp)
            try:
                if redis is not None:
                    await redis.set(f"optq:{contract}", str(ltp), ex=30)
            except Exception:
                pass
            return ltp
    except Exception as e:
        logger.debug(f"Option quote fetch failed [{contract}]: {e}")
    return None


# ── Delta-based strike selection ──────────────────────────────────────────────

def find_delta_strike(
    underlying_price: float,
    target_delta: float,
    option_type: str,
    dte: int,
    sigma: float,
    strike_interval: int = 50,
) -> int:
    """
    Find the strike K whose Black-Scholes delta is closest to target_delta.
    target_delta: positive for CE (e.g. 0.20), negative for PE (e.g. -0.20)

    Binary searches within ±20 strikes from ATM.
    Returns the nearest valid strike (rounded to strike_interval).
    """
    T = max(dte, 1) / 365.0
    atm = round(underlying_price / strike_interval) * strike_interval

    # Build candidate strikes: 20 OTM to ATM
    if option_type == "CE":
        candidates = [atm + i * strike_interval for i in range(-2, 21)]
    else:
        candidates = [atm - i * strike_interval for i in range(-2, 21)]

    best_strike = atm
    best_diff = float("inf")

    abs_target = abs(target_delta)
    for K in candidates:
        if K <= 0:
            continue
        d = bs_delta(underlying_price, K, T, sigma, option_type)
        diff = abs(abs(d) - abs_target)
        if diff < best_diff:
            best_diff = diff
            best_strike = K

    # Enforce minimum 1-interval OTM distance — the short leg must never be
    # at or inside the current price (that would be an immediate breach).
    if option_type == "PE":
        # Put short strike must be strictly below the underlying
        ceiling = int(round(underlying_price / strike_interval) * strike_interval) - strike_interval
        if best_strike > ceiling:
            best_strike = ceiling
    else:
        # Call short strike must be strictly above the underlying
        floor_ = int(round(underlying_price / strike_interval) * strike_interval) + strike_interval
        if best_strike < floor_:
            best_strike = floor_

    return int(best_strike)


async def get_entry_prices_for_spread(
    symbol: str,
    short_contract: str,
    long_contract: str,
    kite,
    redis,
    atr: float,
    dte: int,
    short_otm_intervals: int = 0,
    long_otm_intervals: int = 2,
) -> Tuple[float, float]:
    """
    Get short + long leg prices.
    Tries real Zerodha quotes first; falls back to ATR-based estimate.
    Returns (short_price, long_price).
    """
    from src.core.utils import estimate_option_premium

    short_real = await get_option_quote(short_contract, kite, redis)
    long_real = await get_option_quote(long_contract, kite, redis)

    short_price = short_real if short_real and short_real > 0 else estimate_option_premium(atr, dte, short_otm_intervals)
    long_price = long_real if long_real and long_real > 0 else estimate_option_premium(atr, dte, long_otm_intervals)

    # Sanity check: net credit must be positive
    if short_price <= long_price:
        logger.warning(
            f"Spread prices inverted ({short_contract}=₹{short_price} < {long_contract}=₹{long_price}). "
            "Falling back to ATR estimate."
        )
        short_price = estimate_option_premium(atr, dte, short_otm_intervals)
        long_price = estimate_option_premium(atr, dte, long_otm_intervals)

    return round(short_price, 2), round(long_price, 2)
