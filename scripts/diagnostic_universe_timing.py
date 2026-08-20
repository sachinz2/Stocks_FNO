"""
One-off diagnostic (2026-08-20, not part of the deployed app): times how long
a full LTPPoller-style pass (kite.historical_data() + indicator computation)
would take across the REAL, currently-listed F&O stock universe (209 symbols,
via a live kite.instruments("NFO") pull), compared against the current
41-symbol FNO_SYMBOLS list -- to answer "would 209 symbols still fit in the
60-second signal-cycle budget" with real numbers instead of a guess, before
touching FNO_SYMBOLS itself.

Read-only: only calls kite.instruments()/kite.historical_data(), writes
nothing to Redis or the DB, and does not import/modify FNO_SYMBOLS.

Run on the server (needs a live kite session):
    docker exec falcon_api python3 scripts/diagnostic_universe_timing.py
"""
import sys
import time

sys.path.insert(0, "/app")

from scripts.zerodha_auto_auth import get_redis_client, fetch_nfo_instruments
from src.core.constants import FNO_SYMBOLS
from src.market_data.ltp_poller import LTPPoller


def main():
    r = get_redis_client()
    token = r.get("zerodha:access_token")
    if not token:
        print("No live Zerodha token -- can't run this diagnostic.")
        return

    from src.brokers.zerodha import ZerodhaBroker
    from src.core.config import settings
    broker = ZerodhaBroker.from_redis_token(settings.ZERODHA_API_KEY, settings.ZERODHA_API_SECRET, token)
    kite = broker.kite

    print("Fetching NFO instrument dump (for the real F&O stock universe)...")
    instruments = fetch_nfo_instruments(token)
    indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYNXT50"}
    all_fno_stocks = sorted({
        inst.get("name", "") for inst in instruments
        if inst.get("instrument_type") in ("CE", "PE") and inst.get("name", "") not in indices
    })
    print(f"Real F&O-eligible stock universe: {len(all_fno_stocks)} symbols")
    print(f"Current hardcoded FNO_SYMBOLS: {len(FNO_SYMBOLS)} symbols")

    print("\nFetching NSE instrument tokens for the full universe...")
    nse_instruments = kite.instruments("NSE")
    all_fno_set = set(all_fno_stocks)
    tokens = {}
    for inst in nse_instruments:
        sym = inst.get("tradingsymbol", "")
        if sym in all_fno_set:
            tokens[sym] = inst["instrument_token"]
    print(f"Resolved tokens: {len(tokens)}/{len(all_fno_stocks)}")
    missing = all_fno_set - set(tokens.keys())
    if missing:
        print(f"No NSE equity token found for: {sorted(missing)}")

    # ── Time fetching 5-min OHLC (10-day window) for the FULL real universe,
    # sequentially via kite.historical_data(), matching LTPPoller._fetch_kite_ohlc()
    # exactly -- this is the real per-cycle cost once every symbol's 5-min
    # cache expires and all need a refetch in the same window (worst case).
    poller = LTPPoller(redis_client=None, symbols=list(tokens.keys()), kite=kite, instrument_tokens=tokens)

    print(f"\nTiming sequential kite.historical_data() calls for all {len(tokens)} real F&O stocks...")
    t0 = time.monotonic()
    ok, failed = 0, 0
    per_call = []
    for i, symbol in enumerate(tokens.keys()):
        t_call = time.monotonic()
        try:
            df = poller._fetch_kite_ohlc(symbol)
            if df is not None and not df.empty:
                ok += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"  FAILED {symbol}: {e}")
        per_call.append(time.monotonic() - t_call)
        if (i + 1) % 25 == 0:
            elapsed = time.monotonic() - t0
            print(f"  ...{i + 1}/{len(tokens)} done, {elapsed:.1f}s elapsed")
    total = time.monotonic() - t0

    print(f"\n=== RESULTS: full {len(tokens)}-symbol universe ===")
    print(f"Total wall time: {total:.1f}s  (ok={ok} failed={failed})")
    print(f"Avg per-symbol call: {sum(per_call) / len(per_call):.3f}s  "
          f"min={min(per_call):.3f}s max={max(per_call):.3f}s")

    # Extrapolate the CURRENT 41-symbol subset's timing from the same run
    # (same network/API conditions, apples-to-apples) instead of a second pass.
    current_set = set(FNO_SYMBOLS)
    current_times = [t for sym, t in zip(tokens.keys(), per_call) if sym in current_set]
    if current_times:
        print(f"\n=== Same-run timing for the CURRENT {len(current_times)} symbols (subset of above) ===")
        print(f"Total: {sum(current_times):.1f}s  avg={sum(current_times) / len(current_times):.3f}s")

    print(f"\n60-second signal-cycle budget check:")
    print(f"  {len(tokens)}-symbol full universe: {'FITS' if total < 60 else 'DOES NOT FIT'} "
          f"({total:.1f}s of 60s budget, {total / 60 * 100:.0f}%)")


if __name__ == "__main__":
    main()
