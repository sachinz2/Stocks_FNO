"""
One-off diagnostic (2026-08-20, not part of the deployed app): ranks the real
208-symbol F&O stock universe by actual traded liquidity (average daily
turnover, not just NSE's minimum F&O-eligibility bar), so a future
FNO_SYMBOLS expansion adds symbols that clear a real liquidity floor instead
of "add all 208 blindly" -- many of the 167 not currently traded are
genuinely thinner than the current hand-picked 41.

Metric: 20-trading-day average daily turnover (mean(volume * close)) via
kite.historical_data(interval="day") -- rupee-value turnover, not raw share
volume, so it's comparable across stocks at very different price points
(closer to what actually matters for option-chain depth than share count
alone). Read-only: only calls kite.instruments()/kite.historical_data(),
writes nothing to Redis or the DB, does not import/modify FNO_SYMBOLS.

Run on the server (needs a live kite session):
    docker exec falcon_api python3 scripts/diagnostic_universe_liquidity.py
"""
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from scripts.zerodha_auto_auth import get_redis_client, fetch_nfo_instruments
from src.core.constants import FNO_SYMBOLS


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

    print("\nFetching NSE instrument tokens for the full universe...")
    nse_instruments = kite.instruments("NSE")
    all_fno_set = set(all_fno_stocks)
    tokens = {}
    for inst in nse_instruments:
        sym = inst.get("tradingsymbol", "")
        if sym in all_fno_set:
            tokens[sym] = inst["instrument_token"]
    print(f"Resolved tokens: {len(tokens)}/{len(all_fno_stocks)}")

    to_date = datetime.now()
    from_date = to_date - timedelta(days=30)  # ~20 trading days

    print(f"\nFetching {from_date.date()}..{to_date.date()} daily candles for {len(tokens)} symbols...")
    t0 = time.monotonic()
    turnover = {}
    failed = []
    for i, (symbol, token_id) in enumerate(tokens.items()):
        try:
            records = kite.historical_data(token_id, from_date, to_date, "day", continuous=False, oi=False)
            if records:
                daily_turnover = [r["volume"] * r["close"] for r in records if r.get("volume")]
                if daily_turnover:
                    turnover[symbol] = sum(daily_turnover) / len(daily_turnover)
        except Exception as e:
            failed.append((symbol, str(e)))
        if (i + 1) % 50 == 0:
            print(f"  ...{i + 1}/{len(tokens)} done, {time.monotonic() - t0:.1f}s elapsed")
    print(f"Done in {time.monotonic() - t0:.1f}s. Failed: {len(failed)}")
    if failed:
        for sym, err in failed[:10]:
            print(f"  FAILED {sym}: {err}")

    ranked = sorted(turnover.items(), key=lambda kv: kv[1], reverse=True)

    print(f"\n=== Current 41 FNO_SYMBOLS: liquidity rank within the real {len(ranked)}-symbol universe ===")
    rank_of = {sym: i + 1 for i, (sym, _) in enumerate(ranked)}
    current_ranks = sorted(
        ((rank_of.get(s), s, turnover.get(s, 0)) for s in FNO_SYMBOLS if s in rank_of),
        key=lambda x: x[0],
    )
    for rank, sym, adtv in current_ranks:
        print(f"  #{rank:>3}  {sym:<15} Rs{adtv / 1e7:,.1f} Cr/day")

    worst_of_current = max(rank_of.get(s, 0) for s in FNO_SYMBOLS if s in rank_of)
    worst_symbol = [s for s in FNO_SYMBOLS if rank_of.get(s) == worst_of_current][0]
    threshold_adtv = turnover[worst_symbol]
    print(f"\nLeast-liquid of the current 41: {worst_symbol} at rank #{worst_of_current}, "
          f"Rs{threshold_adtv / 1e7:,.1f} Cr/day average turnover.")

    qualifying = [s for s, v in ranked if v >= threshold_adtv]
    print(f"\n=== Symbols clearing that same liquidity bar (Rs{threshold_adtv / 1e7:,.1f} Cr/day+): "
          f"{len(qualifying)} of {len(ranked)} ===")
    new_qualifying = [s for s in qualifying if s not in set(FNO_SYMBOLS)]
    print(f"Of those, {len(new_qualifying)} are NOT in the current FNO_SYMBOLS list:")
    for s in new_qualifying:
        print(f"  {s:<15} Rs{turnover[s] / 1e7:,.1f} Cr/day  (rank #{rank_of[s]})")

    print(f"\n=== Bottom 15 of the full 208 -- clearly too thin to add ===")
    for sym, adtv in ranked[-15:]:
        print(f"  #{rank_of[sym]:>3}  {sym:<15} Rs{adtv / 1e7:,.2f} Cr/day")


if __name__ == "__main__":
    main()
