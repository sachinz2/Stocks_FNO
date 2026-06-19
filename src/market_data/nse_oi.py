"""
NSE Option Chain — OI, PCR, Max Pain (free, no auth needed).

Uses NSE's public option chain API. NSE requires a session cookie
obtained by first visiting the homepage, so we maintain a requests.Session.

Data refreshed every 15 minutes and cached in Redis.

Key outputs per symbol:
  - pcr       : Put-Call Ratio (total put OI / total call OI)
                > 1.2 = bullish  |  < 0.8 = bearish  |  0.8–1.2 = neutral
  - max_pain  : Strike where option buyers lose the most (strong magnet near expiry)
  - crowded_strikes : Strikes with exceptionally high OI — avoid selling here
"""
import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_NSE_HOME      = "https://www.nseindia.com"
_OC_URL        = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
_CACHE_TTL     = 900   # 15 minutes
_CACHE_KEY     = "nse_oi:{symbol}"
_PREV_OI_KEY   = "nse_oi_prev:{symbol}"   # previous-cycle OI snapshot for delta

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.nseindia.com/option-chain",
    "Connection":      "keep-alive",
}


def _make_session():
    import requests
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:
        # Hit the homepage first to get NSE session cookies
        s.get(_NSE_HOME, timeout=10)
        time.sleep(0.5)
    except Exception as e:
        logger.debug(f"NSE session init warning: {e}")
    return s


def _fetch_option_chain_blocking(symbol: str) -> Optional[Dict]:
    """Synchronous NSE option chain fetch. Run in thread executor."""
    import requests
    s = _make_session()
    url = _OC_URL.format(symbol=symbol.upper())
    try:
        resp = s.get(url, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"NSE OI: HTTP {resp.status_code} for {symbol}")
            return None
        data = resp.json()
        return data.get("records", {})
    except Exception as e:
        logger.warning(f"NSE OI fetch failed [{symbol}]: {e}")
        return None


def _parse_option_chain(records: Dict) -> Dict:
    """
    Parse raw NSE records into PCR, max pain, and crowded strikes.

    Returns:
        {
          "pcr": float,
          "max_pain": int,
          "crowded_call_strikes": [int, ...],  # top-3 call OI strikes
          "crowded_put_strikes":  [int, ...],  # top-3 put OI strikes
          "total_call_oi": int,
          "total_put_oi":  int,
          "expiry_date":   str,
        }
    """
    data = records.get("data", [])
    expiry = records.get("expiryDates", [""])[0]   # nearest expiry

    call_oi_by_strike: Dict[int, int] = {}
    put_oi_by_strike:  Dict[int, int] = {}

    for row in data:
        strike = int(row.get("strikePrice", 0))
        ce = row.get("CE", {})
        pe = row.get("PE", {})
        if ce and ce.get("expiryDate") == expiry:
            call_oi_by_strike[strike] = int(ce.get("openInterest", 0))
        if pe and pe.get("expiryDate") == expiry:
            put_oi_by_strike[strike]  = int(pe.get("openInterest", 0))

    total_call_oi = sum(call_oi_by_strike.values())
    total_put_oi  = sum(put_oi_by_strike.values())
    pcr = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else 1.0

    # Max Pain: strike where total option buyer loss is maximum
    max_pain = _calculate_max_pain(call_oi_by_strike, put_oi_by_strike)

    # Crowded strikes: top-3 by OI on each side
    crowded_calls = sorted(call_oi_by_strike, key=call_oi_by_strike.get, reverse=True)[:3]
    crowded_puts  = sorted(put_oi_by_strike,  key=put_oi_by_strike.get,  reverse=True)[:3]

    return {
        "pcr":                   pcr,
        "max_pain":              max_pain,
        "crowded_call_strikes":  sorted(crowded_calls),
        "crowded_put_strikes":   sorted(crowded_puts),
        "total_call_oi":         total_call_oi,
        "total_put_oi":          total_put_oi,
        "expiry_date":           expiry,
        # OI change fields — populated by get_oi_data() on subsequent calls
        "call_oi_change":        0,
        "put_oi_change":         0,
        "oi_signal":             "NEUTRAL",   # see oi_price_signal()
    }


def _calculate_max_pain(
    call_oi: Dict[int, int], put_oi: Dict[int, int]
) -> int:
    """
    Max pain = strike where total option buyer loss is highest
    (= option writer gain is highest = price most likely to pin near expiry).
    """
    all_strikes = sorted(set(call_oi) | set(put_oi))
    if not all_strikes:
        return 0

    min_pain   = float("inf")
    max_pain_k = all_strikes[len(all_strikes) // 2]

    for test_price in all_strikes:
        # Call buyers lose when test_price < strike
        call_loss = sum(
            max(0, k - test_price) * oi for k, oi in call_oi.items()
        )
        # Put buyers lose when test_price > strike
        put_loss = sum(
            max(0, test_price - k) * oi for k, oi in put_oi.items()
        )
        total_loss = call_loss + put_loss
        if total_loss < min_pain:
            min_pain    = total_loss
            max_pain_k  = test_price

    return max_pain_k


# ── Public async API ──────────────────────────────────────────────────────────

async def get_oi_data(symbol: str, redis) -> Optional[Dict]:
    """
    Return cached OI data for a symbol.
    Refreshes from NSE if cache is stale (> 15 minutes).
    Returns None if NSE is unreachable.
    """
    cache_key = _CACHE_KEY.format(symbol=symbol)
    try:
        raw = await redis.get(cache_key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass

    # Cache miss — fetch from NSE in a thread
    loop = asyncio.get_event_loop()
    records = await loop.run_in_executor(None, _fetch_option_chain_blocking, symbol)
    if not records:
        return None

    parsed = _parse_option_chain(records)

    # ── OI Change delta ───────────────────────────────────────────────────────
    # Compare current OI with previous cycle to detect institutional positioning.
    # Price↑ + OI↑ = fresh longs entering = STRONG BULLISH
    # Price↑ + OI↓ = short covering only  = WEAK BULLISH (potential reversal)
    # Price↓ + OI↑ = fresh shorts entering = STRONG BEARISH
    # Price↓ + OI↓ = long exits only      = WEAK BEARISH
    prev_key = _PREV_OI_KEY.format(symbol=symbol)
    try:
        prev_raw = await redis.get(prev_key)
        if prev_raw:
            prev = json.loads(prev_raw)
            parsed["call_oi_change"] = (
                parsed["total_call_oi"] - prev.get("total_call_oi", parsed["total_call_oi"])
            )
            parsed["put_oi_change"] = (
                parsed["total_put_oi"] - prev.get("total_put_oi", parsed["total_put_oi"])
            )
            parsed["oi_signal"] = _oi_change_signal(
                parsed["call_oi_change"], parsed["put_oi_change"]
            )
        # Store current as previous for next cycle
        await redis.set(prev_key, json.dumps({
            "total_call_oi": parsed["total_call_oi"],
            "total_put_oi":  parsed["total_put_oi"],
        }), ex=3600)   # expire after 1 hour (covers one trading session)
    except Exception:
        pass

    try:
        await redis.set(cache_key, json.dumps(parsed), ex=_CACHE_TTL)
    except Exception:
        pass

    logger.info(
        f"NSE OI [{symbol}]: PCR={parsed['pcr']:.2f} "
        f"MaxPain={parsed['max_pain']} "
        f"OI_signal={parsed['oi_signal']} "
        f"Expiry={parsed['expiry_date']}"
    )
    return parsed


def _oi_change_signal(call_oi_change: int, put_oi_change: int) -> str:
    """
    Classify the OI delta without price context (used during OI fetch).
    Full price-aware signal: use oi_price_signal() after combining with LTP.
    """
    if put_oi_change > 0 and call_oi_change <= 0:
        return "BULLISH_OI"   # puts added, calls not — market hedging puts
    if call_oi_change > 0 and put_oi_change <= 0:
        return "BEARISH_OI"   # calls added, puts not
    return "NEUTRAL"


def oi_price_signal(
    call_oi_change: int,
    put_oi_change:  int,
    price_change_pct: float,   # current bar return vs prev bar
) -> str:
    """
    Combined OI + price signal — the most useful OI interpretation.

    Classic Price + OI analysis:
      Price ↑ + Total OI ↑  → new longs entering   = STRONG_BULLISH
      Price ↑ + Total OI ↓  → short covering        = WEAK_BULLISH (fading)
      Price ↓ + Total OI ↑  → new shorts entering   = STRONG_BEARISH
      Price ↓ + Total OI ↓  → long exits / unwinding = WEAK_BEARISH

    We use combined OI (calls + puts) as a proxy for total market commitment.
    """
    total_oi_change = call_oi_change + put_oi_change
    price_up = price_change_pct >= 0

    if price_up and total_oi_change > 0:
        return "STRONG_BULLISH"
    if price_up and total_oi_change < 0:
        return "WEAK_BULLISH"
    if not price_up and total_oi_change > 0:
        return "STRONG_BEARISH"
    if not price_up and total_oi_change < 0:
        return "WEAK_BEARISH"
    return "NEUTRAL"


def pcr_sentiment(pcr: Optional[float]) -> str:
    """
    Translate PCR into a sentiment string.
    Used by credit spread strategy to confirm direction.
    """
    if pcr is None:
        return "NEUTRAL"
    if pcr > 1.2:
        return "BULLISH"   # market hedging with puts → underlying likely supported
    if pcr < 0.8:
        return "BEARISH"   # more calls → underlying may struggle
    return "NEUTRAL"


def is_strike_crowded(
    strike: int, oi_data: Optional[Dict], option_type: str
) -> bool:
    """
    Returns True if this strike has exceptionally high OI — avoid selling here.
    High OI = the market is using this strike as a hedge, making it more likely
    to be tested.
    """
    if not oi_data:
        return False
    key = "crowded_call_strikes" if option_type == "CE" else "crowded_put_strikes"
    return strike in oi_data.get(key, [])


def pcr_allows_spread(pcr: Optional[float], spread_type: str) -> bool:
    """
    Gate for credit spread entry based on PCR sentiment.
    - BULL_PUT_SPREAD: needs PCR ≥ 0.8 (not strongly bearish)
    - BEAR_CALL_SPREAD: needs PCR ≤ 1.2 (not strongly bullish)
    Fails-open when PCR is unknown.
    """
    if pcr is None:
        return True
    if spread_type == "BULL_PUT_SPREAD":
        return pcr >= 0.8   # don't sell puts into a bearish market
    if spread_type == "BEAR_CALL_SPREAD":
        return pcr <= 1.2   # don't sell calls into a bullish market
    return True
