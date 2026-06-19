"""
Market data diagnostic — tests all three Zerodha data sources.

Run inside Docker:
  docker exec -it falcon_api python3 scripts/test_market_data.py

Checks:
  1. Instrument token mapping (kite.instruments)
  2. REST LTP         — kite.ltp()             (should return last traded price)
  3. Historical OHLC  — kite.historical_data()  5-min candles for RELIANCE
  4. Daily OHLC       — kite.historical_data()  daily candles for RELIANCE + NIFTY 50
  5. WebSocket Redis  — reads tick:RELIANCE from Redis (set by ZerodhaTicker)
  6. RSRanker Redis   — reads nfo:rs_top10 from Redis
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

TEST_SYMBOLS = ["RELIANCE", "INFY", "HDFCBANK"]
NIFTY_50_TOKEN = 256265

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}!{RESET} {msg}")
def header(msg): print(f"\n{BOLD}{msg}{RESET}")


def main():
    from src.core.config import settings
    from kiteconnect import KiteConnect
    import redis

    # ── Connect to Redis ───────────────────────────────────────────────────────
    r = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
    )
    try:
        r.ping()
        ok("Redis connected")
    except Exception as e:
        fail(f"Redis connection failed: {e}")
        return

    # ── Get access token ───────────────────────────────────────────────────────
    access_token = r.get("zerodha:access_token")
    if not access_token:
        fail("No access token in Redis — run scripts/zerodha_auto_auth.py first")
        return
    ok(f"Access token found: {access_token[:8]}...")

    kite = KiteConnect(api_key=settings.ZERODHA_API_KEY)
    kite.set_access_token(access_token)

    # ── 1. Instrument token mapping ────────────────────────────────────────────
    header("1. Instrument Token Mapping")
    try:
        instruments = kite.instruments("NSE")
        fno_set = {"RELIANCE", "INFY", "HDFCBANK", "TCS", "ICICIBANK"}
        tokens = {}
        nifty_token = None
        for inst in instruments:
            sym = inst.get("tradingsymbol", "")
            if sym in fno_set:
                tokens[sym] = inst["instrument_token"]
            if sym == "NIFTY 50":
                nifty_token = inst["instrument_token"]

        ok(f"NSE instruments fetched: {len(instruments):,} total")
        for sym in TEST_SYMBOLS:
            if sym in tokens:
                ok(f"  {sym} → token {tokens[sym]}")
            else:
                warn(f"  {sym} → NOT FOUND")
        if nifty_token:
            ok(f"  NIFTY 50 → token {nifty_token} (hardcoded fallback: {NIFTY_50_TOKEN})")
            if nifty_token != NIFTY_50_TOKEN:
                warn(f"  NIFTY token mismatch! Live={nifty_token} vs hardcoded={NIFTY_50_TOKEN}")
        else:
            warn("  NIFTY 50 index token not found in instruments list")
    except Exception as e:
        fail(f"kite.instruments() failed: {e}")
        tokens = {}
        nifty_token = None

    # ── 2. REST LTP ────────────────────────────────────────────────────────────
    header("2. REST LTP  —  kite.ltp()")
    try:
        instruments_list = [f"NSE:{s}" for s in TEST_SYMBOLS]
        quotes = kite.ltp(instruments_list)
        for sym in TEST_SYMBOLS:
            key = f"NSE:{sym}"
            ltp = quotes.get(key, {}).get("last_price", 0)
            if ltp > 0:
                ok(f"  {sym}: ₹{ltp:,.2f}")
            else:
                fail(f"  {sym}: no LTP returned")
    except Exception as e:
        fail(f"kite.ltp() failed: {e}")

    # ── 3. Historical OHLC — 5-minute candles ─────────────────────────────────
    header("3. Historical OHLC  —  5-minute candles (last 2 days, RELIANCE)")
    rel_token = tokens.get("RELIANCE")
    if rel_token:
        try:
            to_dt   = datetime.now()
            from_dt = to_dt - timedelta(days=2)
            candles = kite.historical_data(
                rel_token, from_dt, to_dt, "5minute", continuous=False, oi=False
            )
            if candles:
                ok(f"  Received {len(candles)} candles")
                for c in candles[-3:]:
                    print(f"    {c['date']}  O={c['open']}  H={c['high']}  L={c['low']}  C={c['close']}  V={c['volume']}")
            else:
                warn("  No candles returned (market may be closed)")
        except Exception as e:
            fail(f"  kite.historical_data(5minute) failed: {e}")
    else:
        warn("  Skipping — RELIANCE token not available")

    # ── 4. Historical OHLC — daily candles ────────────────────────────────────
    header("4. Historical OHLC  —  daily candles (last 30 days, RELIANCE + NIFTY 50)")
    if rel_token:
        try:
            to_dt   = datetime.now()
            from_dt = to_dt - timedelta(days=30)
            candles = kite.historical_data(
                rel_token, from_dt, to_dt, "day", continuous=False, oi=False
            )
            if candles:
                ok(f"  RELIANCE: {len(candles)} daily candles")
                c = candles[-1]
                print(f"    Last: {c['date'].date()}  O={c['open']}  H={c['high']}  L={c['low']}  C={c['close']}")
            else:
                warn("  RELIANCE: no daily candles")
        except Exception as e:
            fail(f"  RELIANCE daily failed: {e}")

    nifty_tok = nifty_token or NIFTY_50_TOKEN
    try:
        to_dt   = datetime.now()
        from_dt = to_dt - timedelta(days=30)
        candles = kite.historical_data(
            nifty_tok, from_dt, to_dt, "day", continuous=False, oi=False
        )
        if candles:
            ok(f"  NIFTY 50 (token {nifty_tok}): {len(candles)} daily candles")
            c = candles[-1]
            print(f"    Last: {c['date'].date()}  C={c['close']}")
        else:
            warn(f"  NIFTY 50: no daily candles (token {nifty_tok})")
    except Exception as e:
        fail(f"  NIFTY 50 daily failed: {e}")

    # ── 5. WebSocket Redis ticks ───────────────────────────────────────────────
    header("5. WebSocket Redis Ticks  —  tick:{SYMBOL} keys")
    ws_ok = 0
    for sym in TEST_SYMBOLS:
        raw = r.get(f"tick:{sym}")
        if raw:
            tick = json.loads(raw)
            src  = tick.get("ltp_source", "unknown")
            ltp  = tick.get("close", 0)
            ts   = tick.get("timestamp", "")
            if src == "zerodha_realtime":
                ok(f"  {sym}: ₹{ltp} [{src}] @ {ts[:19]}")
                ws_ok += 1
            elif src in ("zerodha_rest", "zerodha_historical"):
                warn(f"  {sym}: ₹{ltp} [{src}] — WebSocket not yet active (REST/historical fallback)")
            else:
                warn(f"  {sym}: ₹{ltp} [{src}]")
        else:
            fail(f"  {sym}: no tick in Redis — LTPPoller/Ticker not yet run?")

    if ws_ok == len(TEST_SYMBOLS):
        ok(f"WebSocket live for all {ws_ok} test symbols")
    elif ws_ok > 0:
        warn(f"WebSocket live for {ws_ok}/{len(TEST_SYMBOLS)} symbols")
    else:
        warn("No WebSocket ticks yet — engine may still be starting (wait 60 s and retry)")

    # ── 6. RSRanker Redis output ───────────────────────────────────────────────
    header("6. Relative Strength Ranker  —  nfo:rs_top10")
    top10_raw = r.get("nfo:rs_top10")
    ranks_raw = r.get("nfo:rs_ranks")
    if top10_raw:
        top10 = json.loads(top10_raw)
        ok(f"RS top-10: {top10}")
    else:
        warn("nfo:rs_top10 not in Redis — RSRanker hasn't run yet (wait 5 min after startup)")

    if ranks_raw:
        ranks = json.loads(ranks_raw)
        ok(f"Full rank list: {len(ranks)} symbols scored")
        for entry in ranks[:5]:
            print(f"    #{entry['rank']}  {entry['symbol']}: RS={entry['rs_score']}")
    else:
        warn("nfo:rs_ranks not in Redis")

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{BOLD}Done.{RESET}")


if __name__ == "__main__":
    main()
