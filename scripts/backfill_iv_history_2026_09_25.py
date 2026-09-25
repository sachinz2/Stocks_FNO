"""
One-off backfill (2026-09-25, live incident: 5 straight trading days of
zero credit_spread_v1/iron_condor_v1 trades).

Root cause (see _refresh_iv_history_for_universe()'s docstring in
live_trading_engine.py for the live incident write-up): update_iv_history()
was only ever reached from inside the VIX>=12-gated credit_spread_v1/
iron_condor_v1 pipeline, so the 4-day LOW_VOL stretch (09-21..09-24) left
most of the 132-symbol universe with far fewer than the 20 accumulated
daily entries get_iv_rank() requires before it'll return a rank at all
(confirmed live: BHARTIARTL 19/20, JIOFIN 2/20, BEL 4/20 with multi-week
gaps). The new daily scheduled job (15:20 IST) stops this recurring, but
does nothing for the history that's ALREADY short -- that would otherwise
take another 1-3+ weeks to accumulate live, one entry per trading day.

This script reconstructs the missing history from REAL historical daily
OHLC (via kite.historical_data(), same source used throughout this
codebase for historical replay -- see backtest_paytm_2026_09_18.py),
computing the identical ATR-based sigma proxy
(atr_to_annualised_vol()) the live fallback path already uses, for every
calendar day in the lookback window. Existing entries are preserved as-is
(a live_sigma-derived reading, when the gate happened to pass, is higher
quality than this proxy) -- this only FILLS GAPS, never overwrites.

Run inside the API container (needs the live Zerodha kite session already
cached in Redis for historical_data()):
    docker compose exec api python3 scripts/backfill_iv_history_2026_09_25.py
"""
import sys
import json
import asyncio
import time
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

import pandas as pd

# Comfortably clears the 20-trading-day minimum even across a holiday-heavy
# stretch (used by get_iv_rank()'s own length check).
LOOKBACK_DAYS = 45
# Wilder ATR warmup -- same convention used everywhere else in this codebase
# (compute_indicators() in backtest_paytm_2026_09_18.py, refresh_nifty_regime_inputs()).
ATR_WARMUP_BARS = 14
# Zerodha historical_data() rate limit is ~3 req/sec; stay well under it.
REQUEST_SLEEP_SECONDS = 0.35


async def main():
    import redis.asyncio as aioredis
    from src.core.config import settings
    from src.core.constants import FNO_SYMBOLS
    from src.market_data.option_chain import (
        atr_to_annualised_vol, _IV_HISTORY_KEY, _IV_HISTORY_MAX,
    )

    r = aioredis.from_url(settings.get_redis_url(), decode_responses=True)
    token = await r.get("zerodha:access_token")
    if not token:
        print("No Zerodha access token in Redis -- is the API container logged in?")
        return

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=settings.ZERODHA_API_KEY)
    kite.set_access_token(token)

    print(f"Fetching instrument tokens for {len(FNO_SYMBOLS)} symbols...")
    instruments = kite.instruments("NSE")
    token_by_symbol = {
        i["tradingsymbol"]: i["instrument_token"]
        for i in instruments if i["tradingsymbol"] in FNO_SYMBOLS
    }
    missing = [s for s in FNO_SYMBOLS if s not in token_by_symbol]
    if missing:
        print(f"WARNING: no instrument token found for {len(missing)} symbol(s): {missing}")

    to_date = datetime.now()
    from_date = to_date - timedelta(days=LOOKBACK_DAYS)

    seeded_days_total = 0
    now_short_count = 0
    still_short = []

    for idx, symbol in enumerate(FNO_SYMBOLS):
        tok = token_by_symbol.get(symbol)
        if not tok:
            continue
        try:
            bars = kite.historical_data(tok, from_date, to_date, "day", continuous=False, oi=False)
        except Exception as exc:
            print(f"  [{symbol}] historical_data() failed: {exc}")
            time.sleep(REQUEST_SLEEP_SECONDS)
            continue
        time.sleep(REQUEST_SLEEP_SECONDS)

        if len(bars) <= ATR_WARMUP_BARS:
            print(f"  [{symbol}] only {len(bars)} daily bars returned -- too short to seed, skipping")
            continue

        df = pd.DataFrame(bars)
        close, high, low = df["close"], df["high"], df["low"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr14"] = tr.ewm(alpha=1.0 / 14, adjust=False).mean()

        backfill_by_date = {}
        for i in range(ATR_WARMUP_BARS, len(df)):
            row = df.iloc[i]
            price = float(row["close"])
            atr = float(row["atr14"])
            if price <= 0 or atr <= 0:
                continue
            d = row["date"]
            d_iso = d.date().isoformat() if hasattr(d, "date") else str(d)[:10]
            sigma = atr_to_annualised_vol(atr, price)
            backfill_by_date[d_iso] = sigma

        key = _IV_HISTORY_KEY.format(symbol=symbol)
        raw = await r.get(key)
        existing = json.loads(raw) if raw else []
        existing_dates = {h["d"] for h in existing}

        added = 0
        for d_iso, sigma in backfill_by_date.items():
            if d_iso in existing_dates:
                continue  # preserve existing (possibly live-quality) entry
            existing.append({"d": d_iso, "iv": sigma})
            added += 1

        existing.sort(key=lambda h: h["d"])
        if len(existing) > _IV_HISTORY_MAX:
            existing = existing[-_IV_HISTORY_MAX:]

        await r.set(key, json.dumps(existing))
        seeded_days_total += added

        status = "OK" if len(existing) >= 20 else "STILL SHORT"
        if len(existing) < 20:
            now_short_count += 1
            still_short.append((symbol, len(existing)))
        print(f"  [{symbol}] {len(existing)} total entries (+{added} backfilled) -- {status}")

        if (idx + 1) % 20 == 0:
            print(f"--- {idx + 1}/{len(FNO_SYMBOLS)} symbols processed ---")

    print(f"\nDone. {seeded_days_total} day-entries backfilled across {len(FNO_SYMBOLS)} symbols.")
    print(f"{now_short_count} symbol(s) still under the 20-entry minimum after backfill:")
    for sym, n in still_short:
        print(f"  {sym}: {n}")


if __name__ == "__main__":
    asyncio.run(main())
