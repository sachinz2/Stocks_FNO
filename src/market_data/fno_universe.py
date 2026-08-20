"""
F&O stock-universe discovery and liquidity ranking (2026-08-20).

Shared logic behind:
  - scripts/diagnostic_universe_timing.py / diagnostic_universe_liquidity.py
    (the read-only, one-off diagnostics that first measured this)
  - scripts/zerodha_auto_auth.py's daily instrument-cache population (now
    covers the full real universe, not just the currently-active subset)
  - the weekly active-universe recompute job (src/api/main.py)

Kept as pure/blocking functions (no Redis, no async) so the same logic is
usable from both the sync daily-auth script and the async FastAPI app (via
run_in_executor), and is trivially unit-testable against a fixed instrument
dump/kite fake without any live network or event loop.
"""
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

# NSE index names that appear in the NFO instrument dump's CE/PE rows
# alongside real stock underlyings -- these are index options, not stocks,
# and must never be treated as a tradeable "F&O stock".
INDEX_NAMES = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYNXT50",
})

# Minimum 20-30 trading day average daily turnover (Rs Crore) for a symbol to
# be included in the actively-traded universe. Fixed threshold (not "worst of
# whatever's currently active") deliberately -- a self-referencing floor
# would drift: if thin symbols get dropped, the floor of "the worst
# remaining" symbol rises, silently shrinking the universe on every
# successive re-run. 150 Cr/day was the real measured floor (TATACONSUM,
# 2026-08-20, see docs/LIVE_TRADING_CHECKLIST.md) rounded down slightly.
MIN_ADTV_CR = 150.0


def extract_stock_underlyings(nfo_instruments: list) -> List[str]:
    """
    Every real stock underlying with listed CE/PE options in a raw
    kite.instruments("NFO") dump, excluding index options. Sorted, deduped.
    """
    names = {
        inst.get("name", "")
        for inst in nfo_instruments
        if inst.get("instrument_type") in ("CE", "PE") and inst.get("name", "") not in INDEX_NAMES
    }
    names.discard("")
    return sorted(names)


def resolve_nse_tokens(nse_instruments: list, symbols) -> Dict[str, int]:
    """Instrument tokens for `symbols` from a raw kite.instruments("NSE") dump."""
    symbol_set = set(symbols)
    tokens: Dict[str, int] = {}
    for inst in nse_instruments:
        sym = inst.get("tradingsymbol", "")
        if sym in symbol_set:
            tokens[sym] = inst["instrument_token"]
    return tokens


def compute_liquidity_turnover(kite, tokens: Dict[str, int], lookback_days: int = 30) -> Dict[str, float]:
    """
    Blocking (real kite.historical_data() calls) -- average daily turnover
    (volume x close, in Rs) per symbol over `lookback_days` calendar days
    (~20-22 trading days). Caller must run this in a thread executor from
    async code. Skips (does not raise for) any symbol whose fetch fails, so
    one bad/delisted-mid-flight symbol doesn't abort the whole ranking.
    """
    from datetime import datetime, timedelta

    to_date = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)
    turnover: Dict[str, float] = {}
    for symbol, token_id in tokens.items():
        try:
            records = kite.historical_data(token_id, from_date, to_date, "day", continuous=False, oi=False)
        except Exception as e:
            logger.warning(f"fno_universe: historical_data failed for {symbol}: {e}")
            continue
        if not records:
            continue
        daily = [r["volume"] * r["close"] for r in records if r.get("volume")]
        if daily:
            turnover[symbol] = sum(daily) / len(daily)
    return turnover


def qualifying_symbols(turnover: Dict[str, float], min_adtv_cr: float = MIN_ADTV_CR) -> List[str]:
    """Symbols whose average daily turnover clears the Rs Crore floor, sorted by symbol name."""
    floor_rs = min_adtv_cr * 1e7  # 1 Crore = 1e7
    return sorted(s for s, v in turnover.items() if v >= floor_rs)
