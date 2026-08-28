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
        loop = asyncio.get_running_loop()
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
    Threshold = 12.0 (aligns with LOW_VOL boundary in regime_detector.py).
    Below 12: market is unusually dead, premiums are too cheap everywhere.
    Between 12-14: quiet but normal — let per-symbol IV Rank decide instead.

    Fixed 2026-08-28 (code review): was fail-OPEN on vix=None ("paper mode"
    rationale) -- inconsistent with every other entry-blocking data gap in
    this codebase (lot size, contract resolution, RS/MTF, entry price,
    event calendar), all of which fail closed. A genuine VIX-feed outage in
    live trading would otherwise let this gate silently pass, selling
    premium with zero confirmation it's actually rich -- exactly what the
    gate exists to prevent. A data outage is a data outage regardless of
    paper vs. live mode; paper trading is supposed to validate what live
    behavior would be, not a looser mode with weaker gates.
    """
    if vix is None:
        return False
    return vix >= 12.0


def iv_rank_allows_selling(iv_rank: Optional[float]) -> bool:
    """
    Per-symbol IV rank gate.
    Rank ≥ 0.30 (i.e., IV is in the top 70% of its year range) → ok to sell.

    Fixed 2026-08-28 (code review): same fail-closed fix as
    vix_allows_selling() above, same rationale -- "not enough history yet"
    is exactly the kind of missing-data case this codebase's fail-closed
    convention exists for, not a reason to let the trade through anyway.
    """
    if iv_rank is None:
        return False
    return iv_rank >= 0.30


# ── Real contract validation ────────────────────────────────────────────────

async def get_real_contract(
    symbol: str, expiry: date, strike: float, option_type: str, redis,
) -> Optional[Tuple[str, float]]:
    """
    Look up the REAL Zerodha tradingsymbol for (symbol, expiry, strike,
    option_type) against the daily-refreshed instrument cache (see
    scripts/zerodha_auto_auth.py's fetch_and_cache_real_contracts()) — instead
    of trusting build_option_symbol()'s hand-formatted string and our own
    computed strike to always match what's actually listed.

    Returns (real_tradingsymbol, real_strike_used) on a cache hit -- real_strike_used
    equals the input `strike` in the common case (exact match), or the nearest
    ACTUALLY LISTED strike if the computed one isn't listed for this expiry
    (e.g. a strike-interval assumption slightly off, or this expiry's strike
    range doesn't extend as far as the near-month one). Returns None on any
    cache miss (Redis down, symbol not cached, expiry not in the cached
    window) -- caller falls back to build_option_symbol()'s formula, exactly
    today's existing behavior, so this can only make contract selection more
    correct, never less available.
    """
    if redis is None:
        return None
    try:
        from src.core.constants import REDIS_CONTRACT_PREFIX
        raw = await redis.get(f"{REDIS_CONTRACT_PREFIX}{symbol}")
        if not raw:
            return None
        data = json.loads(raw)
        expiry_iso = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)
        strikes_for_expiry = data.get(expiry_iso)
        if not strikes_for_expiry:
            return None

        def _key(k: float) -> str:
            return str(int(k)) if float(k) == int(k) else str(float(k))

        entry = strikes_for_expiry.get(_key(strike))
        if entry and entry.get(option_type):
            return entry[option_type], strike

        # Exact strike not listed for this expiry -- snap to nearest real one.
        available = sorted(float(k) for k in strikes_for_expiry.keys())
        if not available:
            return None
        nearest = min(available, key=lambda k: abs(k - strike))
        entry = strikes_for_expiry.get(_key(nearest))
        if entry and entry.get(option_type):
            if nearest != strike:
                logger.warning(
                    f"get_real_contract: {symbol} {expiry_iso} {option_type} strike "
                    f"{strike} not listed -- snapped to nearest real strike {nearest}"
                )
            return entry[option_type], nearest
        return None
    except Exception as e:
        logger.debug(f"get_real_contract: cache lookup failed for {symbol}: {e}")
        return None


async def get_real_strike_interval(symbol: str, expiry: date, redis) -> Optional[float]:
    """
    Derive the real NSE strike interval for (symbol, expiry) from the same
    daily-refreshed real-contract cache get_real_contract() reads, instead of
    a hand-maintained FNO_STRIKE_INTERVALS table.

    Added 2026-08-20: that table was already found wrong for 27/39 symbols
    once this project. get_real_contract() already protects against ordering
    a PHANTOM (non-listed) strike by snapping to the nearest real one -- but
    a wrong interval still corrupts the *candidate* strike fed into it,
    especially find_delta_strike()'s scan grid (candidates spaced by
    strike_interval across up to 30 strikes from ATM): too large an interval
    overshoots the intended delta range entirely, too small clusters every
    candidate near ATM and never reaches it. This is self-correcting instead
    -- the interval is read from strikes Zerodha has actually listed for this
    exact expiry, so it can never drift out of sync the way a static table can,
    and a newly-added symbol needs zero manual verification to get this right.

    Returns the minimum gap between consecutive real listed strikes, or None
    on a cache miss (Redis down, symbol not cached, expiry not in the cached
    window, or fewer than 2 strikes listed) -- callers should fall back to
    the static FNO_STRIKE_INTERVALS table exactly like today, since a missing
    real interval is an availability gap, not new information that trading
    should stop (get_real_contract()'s own snap-to-nearest-listed-strike
    check downstream is what actually guards against a phantom contract).
    """
    if redis is None:
        return None
    try:
        from src.core.constants import REDIS_CONTRACT_PREFIX
        raw = await redis.get(f"{REDIS_CONTRACT_PREFIX}{symbol}")
        if not raw:
            return None
        data = json.loads(raw)
        expiry_iso = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry)
        strikes_for_expiry = data.get(expiry_iso)
        if not strikes_for_expiry or len(strikes_for_expiry) < 2:
            return None
        strikes = sorted(float(k) for k in strikes_for_expiry.keys())
        gaps = [round(b - a, 4) for a, b in zip(strikes, strikes[1:]) if b > a]
        if not gaps:
            return None
        interval = min(gaps)
        return int(interval) if interval == int(interval) else interval
    except Exception as e:
        logger.debug(f"get_real_strike_interval: cache lookup failed for {symbol}: {e}")
        return None


async def get_active_fno_symbols(redis) -> List[str]:
    """
    The dynamically-recomputed, liquidity-verified actively-traded symbol
    list (src/core/constants.py's REDIS_ACTIVE_FNO_SYMBOLS, written weekly
    by scripts/zerodha_auto_auth.py's recompute_active_universe()) -- falls
    back to the static FNO_SYMBOLS list on a cache miss or Redis error
    (fresh Redis before the first weekly run, Redis down, or a malformed
    cached value), never to an empty list. Added 2026-08-20 alongside the
    41->132 FNO_SYMBOLS expansion, to keep the active universe from going
    stale the same way FNO_STRIKE_INTERVALS did before it was made
    self-correcting.
    """
    from src.core.constants import FNO_SYMBOLS, REDIS_ACTIVE_FNO_SYMBOLS
    if redis is None:
        return list(FNO_SYMBOLS)
    try:
        raw = await redis.get(REDIS_ACTIVE_FNO_SYMBOLS)
        if not raw:
            return list(FNO_SYMBOLS)
        symbols = json.loads(raw)
        if not symbols:
            return list(FNO_SYMBOLS)
        return symbols
    except Exception as e:
        logger.debug(f"get_active_fno_symbols: cache lookup failed, falling back to static list: {e}")
        return list(FNO_SYMBOLS)


def resolve_reliable_option_price(quote: Dict) -> Optional[float]:
    """
    Given one symbol's raw kite.quote() response dict, return a price we can
    actually trust as the current tradeable value -- or None if nothing
    reliable is available (caller should fall back to the ATR/Black-Scholes
    estimate already used everywhere get_option_quote() can return None).

    Zerodha's last_price is the LAST TRADED price -- for an option contract
    nobody's traded in a while, that can be badly stale while still being
    returned as if current. Confirmed live 2026-08-13: TITAN26SEP4650PE's
    last_price was frozen at Rs82 (its last_trade_time was 2026-07-29, two
    weeks earlier) with zero bids and zero volume that day, while the
    visible ask side (from Rs34.80) showed the real, current, tradeable
    value was far lower. Both the dashboard's displayed PnL and
    credit_spread_v1/iron_condor_v1's own profit-target/stop-loss exit
    checks read this same price, so a stale one isn't just cosmetic -- it
    can make a strategy's exit logic completely blind to a position's real
    current value.

    Trusts last_price directly if the contract genuinely traded TODAY.
    Otherwise prefers the live order book: bid/ask midpoint if both sides
    have real (non-zero) depth, whichever single side is present if only
    one does. Returns None only when there's no reliable signal at all.
    """
    from src.core.utils import now_ist

    last_price = quote.get("last_price")
    last_trade_time = quote.get("last_trade_time")
    traded_today = False
    if last_trade_time is not None:
        try:
            ltt_date = last_trade_time.date() if hasattr(last_trade_time, "date") else None
            traded_today = ltt_date == now_ist().date()
        except Exception:
            traded_today = False

    if traded_today and last_price and float(last_price) > 0:
        return float(last_price)

    depth = quote.get("depth") or {}
    best_bid = next((lvl["price"] for lvl in depth.get("buy", []) if lvl.get("price", 0) > 0), None)
    best_ask = next((lvl["price"] for lvl in depth.get("sell", []) if lvl.get("price", 0) > 0), None)
    if best_bid and best_ask:
        return round((best_bid + best_ask) / 2, 2)
    if best_bid:
        return float(best_bid)
    if best_ask:
        return float(best_ask)
    return None


# ── Real option quotes ────────────────────────────────────────────────────────

async def get_option_quote(contract: str, kite, redis) -> Optional[float]:
    """
    Fetch real LTP for an option contract from Zerodha.

    Priority order:
      1. optltp:{contract}  — written every 5 s by ZerodhaLTPPoller when the
                              contract is registered as an active position. This
                              is the freshest source; TTL = 15 s so stale data
                              auto-expires if the poller stops. Already staleness-
                              checked at the source (see resolve_reliable_option_price).
      2. optq:{contract}    — 30-second on-demand cache from a previous quote
                              call (used before the position was registered, or as
                              a fallback when the live cache has expired).
      3. kite.quote() live  — fresh REST call; result stored in optq cache.

    Fixed 2026-08-13: step 3 used kite.ltp() (last-traded-price only, no
    staleness signal) -- switched to kite.quote() + resolve_reliable_option_price()
    for the same reason the ZerodhaLTPPoller path was fixed (see that
    function's docstring): a contract that hasn't actually traded in a
    while can report a last_price that's badly stale while looking current.
    This is specifically the fallback for when steps 1-2 are both empty
    (rare -- active positions are kept warm by the poller), so the extra
    kite.quote() cost here is not a rate-limit concern.

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

    # 3. Fresh kite.quote() REST call
    try:
        import asyncio
        loop = asyncio.get_running_loop()
        nfo_sym = f"NFO:{contract}"
        data = await loop.run_in_executor(None, lambda: kite.quote([nfo_sym]))
        price = resolve_reliable_option_price(data.get(nfo_sym, {}))
        if price and price > 0:
            try:
                if redis is not None:
                    await redis.set(f"optq:{contract}", str(price), ex=30)
            except Exception:
                pass
            return price
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
    strike_interval: float = 50,
    enforce_min_otm: bool = True,
) -> float:
    """
    Find the strike K whose Black-Scholes delta is closest to target_delta.
    target_delta: positive for CE (e.g. 0.20), negative for PE (e.g. -0.20)

    Linear scan over 33 candidate strikes (2 ITM to 30 OTM from ATM) — cheap enough
    at this size that a binary search isn't worth the complexity.
    Returns the nearest valid strike (rounded to strike_interval).

    strike_interval may be fractional (2.5, for symbols like ITC/WIPRO/ONGC/
    POWERGRID/TATASTEEL whose real NSE grid includes half-strikes) -- the
    result is NOT forced to int here (that used to silently truncate a real
    half-strike like 247.5 into a non-existent 247); build_option_symbol()
    formats the final value correctly either way.

    enforce_min_otm: when True (default), the result is clamped to at least
    1 strike interval OTM of the underlying -- correct for a short spread/
    condor leg, where an ITM strike would be an immediate breach. Set False
    for callers deliberately targeting an ITM strike (e.g. momentum_v1's
    near-ITM single-leg entries via entry_option_delta) -- otherwise this
    clamp silently pushes an intentional delta~0.60 fit back out to OTM,
    since any delta above 0.50 is ITM by definition. Found 2026-08-21: this
    was happening on every momentum_v1 entry, defeating its ITM-entry
    redesign without any downstream check catching it.
    """
    T = max(dte, 1) / 365.0
    atm = round(underlying_price / strike_interval) * strike_interval

    # Build candidate strikes: 30 OTM to ATM
    if option_type == "CE":
        candidates = [atm + i * strike_interval for i in range(-2, 31)]
    else:
        candidates = [atm - i * strike_interval for i in range(-2, 31)]

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
    # Skipped entirely when the caller is deliberately targeting an ITM
    # strike (enforce_min_otm=False) -- see docstring.
    if enforce_min_otm:
        if option_type == "PE":
            # Put short strike must be strictly below the underlying
            ceiling = round(underlying_price / strike_interval) * strike_interval - strike_interval
            if best_strike > ceiling:
                best_strike = ceiling
        else:
            # Call short strike must be strictly above the underlying
            floor_ = round(underlying_price / strike_interval) * strike_interval + strike_interval
            if best_strike < floor_:
                best_strike = floor_

    return int(best_strike) if best_strike == int(best_strike) else best_strike


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
) -> Optional[Tuple[float, float]]:
    """
    Get short + long leg prices.
    Tries real Zerodha quotes first; falls back to ATR-based estimate only
    for a leg whose real quote is genuinely unavailable.
    Returns (short_price, long_price), or None if the resulting prices are
    inverted (short <= long) -- a real credit spread cannot have a negative
    net credit, and this shape almost always means at least one quote is
    stale/thin/unreliable rather than that the market is genuinely priced
    that way.

    Fixed 2026-08-21 (deep review): the inversion branch used to discard
    BOTH legs' prices -- including a perfectly good real quote -- and
    substitute fabricated ATR estimates for both, which the caller then
    used directly as the real LIMIT order price. That contradicts the
    fail-closed convention already established for the single-leg entry
    path in the engine (skip the entry rather than place a LIMIT order on a
    guessed price) -- extended here: an inversion now fails closed (returns
    None, caller skips the entry) instead of fabricating a tradeable price.
    """
    from src.core.utils import estimate_option_premium

    short_real = await get_option_quote(short_contract, kite, redis)
    long_real = await get_option_quote(long_contract, kite, redis)

    short_price = short_real if short_real and short_real > 0 else estimate_option_premium(atr, dte, short_otm_intervals)
    long_price = long_real if long_real and long_real > 0 else estimate_option_premium(atr, dte, long_otm_intervals)

    # Sanity check: net credit must be positive
    if short_price <= long_price:
        logger.warning(
            f"Spread prices inverted ({short_contract}=₹{short_price} <= {long_contract}=₹{long_price}). "
            "Fail-closed: not placing a LIMIT order on a guessed/unreliable price -- skipping entry."
        )
        return None

    return round(short_price, 2), round(long_price, 2)
