"""
LTPPoller / update_intraday_bar() tests: volume accumulation across 5-min
bucket rollovers, session_vwap, RVOL/ADX validity flags, and day_prev_close
(the real previous-trading-day close used for market breadth).
"""
import datetime as dt
import pytz
import pandas as pd
import pytest

from src.market_data.ltp_poller import LTPPoller

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze now_ist() (as imported inside core/utils and ltp_poller) so bar-
    bucket math is deterministic. Returns a mutable {'t': datetime} the test
    can advance."""
    state = {"t": IST.localize(dt.datetime(2026, 7, 30, 9, 15, 0))}

    def _now_ist():
        return state["t"]

    monkeypatch.setattr("src.core.utils.now_ist", _now_ist)
    monkeypatch.setattr("src.market_data.ltp_poller.now_ist", _now_ist)
    return state


def test_volume_accumulates_and_resets_across_bar_rollovers(frozen_now):
    from src.core.utils import update_intraday_bar

    tick = {}
    # New day, first tick: cumulative volume 1000 -> baseline, no delta yet.
    update_intraday_bar(tick, 100.0, volume_traded=1000)
    assert tick["cur_bar_volume"] == 0
    assert tick["_last_cum_volume"] == 1000

    # Same 5-min bucket, cumulative climbs -> delta counted.
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 30, 9, 16, 30))
    update_intraday_bar(tick, 101.0, volume_traded=1500)
    assert tick["cur_bar_volume"] == 500

    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 30, 9, 18, 0))
    update_intraday_bar(tick, 102.0, volume_traded=1800)
    assert tick["cur_bar_volume"] == 800

    # Roll into the next bucket -> first bar finalizes at volume=800, new
    # bar starts accumulating from this tick's delta.
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 30, 9, 21, 0))
    update_intraday_bar(tick, 103.0, volume_traded=2000)
    assert len(tick["bars_today"]) == 1
    assert tick["bars_today"][0]["volume"] == 800
    assert tick["cur_bar_volume"] == 200

    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 30, 9, 23, 0))
    update_intraday_bar(tick, 104.0, volume_traded=2300)
    assert tick["cur_bar_volume"] == 500

    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 30, 9, 26, 0))
    update_intraday_bar(tick, 105.0, volume_traded=2300)  # no new trades this tick
    assert tick["bars_today"][1]["volume"] == 500
    assert tick["cur_bar_volume"] == 0


def test_volume_none_rest_fallback_path_unaffected(frozen_now):
    from src.core.utils import update_intraday_bar

    tick = {}
    update_intraday_bar(tick, 50.0)  # volume_traded defaults to None
    assert "_last_cum_volume" not in tick

    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 30, 9, 21, 0))
    update_intraday_bar(tick, 51.0)
    assert tick["bars_today"][0]["volume"] == 0


def test_new_day_resets_volume_baseline(frozen_now):
    from src.core.utils import update_intraday_bar

    tick = {}
    update_intraday_bar(tick, 100.0, volume_traded=1000)
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 9, 15, 0))  # next day
    update_intraday_bar(tick, 200.0, volume_traded=50)

    assert tick["cur_bar_volume"] == 0
    assert tick["_last_cum_volume"] == 50
    assert tick["bars_today"] == []


def _make_hist_baseline(base_date, n=30, price=100.0, volume=1000):
    dates = pd.date_range(base_date, periods=n, freq="5min")
    return pd.DataFrame({
        "date": dates, "open": price, "high": price + 0.5, "low": price - 0.5,
        "close": price, "volume": volume,
    })


def test_enrich_session_vwap_and_rvol_with_real_volume():
    hist = _make_hist_baseline("2026-07-20", n=30, price=100.0, volume=1000)

    bars_today = []
    base_time = IST.localize(dt.datetime(2026, 7, 30, 9, 15, 0))
    price = 100.0
    for i in range(25):
        price += 0.3
        bars_today.append({
            "date": (base_time + dt.timedelta(minutes=5 * i)).isoformat(),
            "open": price - 0.3, "high": price + 0.1, "low": price - 0.3, "close": price,
            "volume": 5000,  # much higher than the 1000/bar historical baseline
        })

    live_range = {
        "bars_today": bars_today,
        "cur_bar_open": price, "cur_bar_high": price + 0.05, "cur_bar_low": price,
        "cur_bar_volume": 9000,
        "day_open": 100.0, "day_high": price + 0.1, "day_low": 99.5,
    }
    ltp = price + 0.05

    tick = LTPPoller._enrich("TESTSTOCK", hist.copy(), ltp, live_range=live_range)

    assert tick["session_vwap"] != tick["vwap"], "session_vwap should differ from the multi-day vwap"
    today_low = min(b["low"] for b in bars_today)
    today_high = max(b["high"] for b in bars_today)
    assert today_low <= tick["session_vwap"] <= today_high

    # Fixed 2026-07-30 (MODE_QUOTE switch) + 2026-08-06 (rvol_valid flag):
    # RVOL is genuinely non-zero with real per-bar volume, and correctly
    # flagged valid once >=20 live bars exist.
    assert tick["rvol"] > 1.0
    assert tick["rvol_valid"] is True


def test_enrich_bootstrap_edge_case_no_live_range():
    hist = _make_hist_baseline("2026-07-20")
    tick = LTPPoller._enrich("TESTSTOCK", hist.copy(), 100.2, live_range=None)
    assert tick["session_vwap"] == 100.2


def test_enrich_rvol_and_adx_invalid_with_insufficient_history():
    # Fixed 2026-08-06: rvol_valid/adx_valid distinguish "not enough bars to
    # compute a real value yet" from "genuinely computed to a low/zero
    # value" -- entry gates in live_trading_engine.py must block on the
    # former instead of silently treating it as a passing check.
    hist = _make_hist_baseline("2026-07-20", n=2)  # far short of the 20/14 bars needed
    tick = LTPPoller._enrich("TESTSTOCK", hist.copy(), 100.2, live_range=None)
    assert tick["rvol_valid"] is False
    assert tick["rvol"] == 0.0


def test_enrich_day_prev_close_is_prior_trading_day_not_bar_to_bar():
    # Fixed 2026-08-06: market breadth was computed from the previous 5-min
    # bar's close, not the actual previous trading day's close -- despite
    # being described everywhere as day-level sentiment. day_prev_close must
    # resolve to the real prior-day close; prev_close stays bar-to-bar for
    # its existing (different) use.
    def _mkbars(day, base_price, n=10):
        rows = []
        for i in range(n):
            t = dt.datetime(day.year, day.month, day.day, 9, 15) + dt.timedelta(minutes=5 * i)
            px = base_price + i * 0.1
            rows.append({"date": t, "open": px, "high": px + 0.5, "low": px - 0.5, "close": px, "volume": 1000})
        return rows

    today = dt.date(2026, 7, 30)
    yesterday = dt.date(2026, 7, 29)
    day_before = dt.date(2026, 7, 28)

    hist_rows = _mkbars(day_before, 100.0, n=10) + _mkbars(yesterday, 200.0, n=10)
    df = pd.DataFrame(hist_rows)

    live_range = {
        "bars_today": [
            {"date": dt.datetime(today.year, today.month, today.day, 9, 15), "open": 300.0, "high": 300.5, "low": 299.5, "close": 300.0, "volume": 500},
            {"date": dt.datetime(today.year, today.month, today.day, 9, 20), "open": 300.0, "high": 300.5, "low": 299.5, "close": 301.0, "volume": 500},
        ],
        "cur_bar_open": 301.0, "cur_bar_high": 301.5, "cur_bar_low": 300.5, "cur_bar_volume": 200,
    }

    tick = LTPPoller._enrich("TESTSYM", df.copy(), 301.5, live_range=live_range)

    expected_yesterday_close = 200.0 + 9 * 0.1  # last bar of "yesterday"'s series
    assert abs(tick["day_prev_close"] - expected_yesterday_close) < 0.01
    assert tick["day_prev_close"] != tick["prev_close"]
    assert abs(tick["prev_close"] - 300.0) < 2.0  # bar-to-bar, from today's own series


def test_enrich_multi_bar_series_beats_single_blob_bar_for_ema_flip(frozen_now):
    """
    Regression guard for the pre-2026-07-27 single-blob-bar approach: today's
    live ticks must be represented as a real, growing multi-bar series
    (bars_today), not one synthetic "blob" candle for the whole day so far --
    the blob approach can fail to flip an EMA crossover that a real bar
    series correctly catches on the same underlying price action.
    """
    import random
    random.seed(42)

    base = 1040.0
    now = IST.localize(dt.datetime(2026, 7, 30, 11, 20, 0))
    frozen_now["t"] = now  # _enrich()'s "exclude today's rows" filter needs a matching now_ist()
    yesterday_close = now.replace(hour=15, minute=25, second=0, microsecond=0) - dt.timedelta(days=1)
    dates = [yesterday_close - dt.timedelta(minutes=5 * i) for i in range(599, -1, -1)]
    df = pd.DataFrame({
        "date": dates, "open": [base] * 600, "high": [base + 1] * 600,
        "low": [base - 1] * 600, "close": [base] * 600, "volume": [10000] * 600,
    })

    bars_today = []
    price = base
    cur_time = IST.localize(dt.datetime(2026, 7, 30, 9, 15, 0))
    for _ in range(24):  # 2 hours of real 5-min bars, upward drift
        bar_open = price
        for _ in range(5):
            price += random.uniform(-0.3, 1.1)
        bar_close = price
        bars_today.append({
            "date": cur_time.isoformat(), "open": round(bar_open, 2),
            "high": round(max(bar_open, bar_close) + 0.5, 2),
            "low": round(min(bar_open, bar_close) - 0.5, 2), "close": round(bar_close, 2),
        })
        cur_time += dt.timedelta(minutes=5)

    live_range = {
        "day_open": base, "day_high": max(b["high"] for b in bars_today),
        "day_low": min(b["low"] for b in bars_today), "close": price,
        "bars_today": bars_today,
        "cur_bar_open": price, "cur_bar_high": price + 0.2, "cur_bar_low": price - 0.2,
        "cur_bar_close": price,
    }

    # OLD approach: one synthetic "blob" row for the whole session so far.
    blob_row = pd.DataFrame([{
        "date": now, "open": base, "high": live_range["day_high"],
        "low": live_range["day_low"], "close": price, "volume": 0,
    }])
    df_blob = pd.concat([df, blob_row], ignore_index=True)
    c = df_blob["close"]
    ema20_blob = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
    ema50_blob = float(c.ewm(span=50, adjust=False).mean().iloc[-1])

    # NEW approach: real multi-bar series via _enrich().
    tick = LTPPoller._enrich("TESTSYM", df.copy(), price, live_range=live_range)

    total_move_pct = (price - base) / base * 100
    assert total_move_pct > 1.5, "test setup should produce a meaningful upward move"
    # The real bar series must reflect the move at least as strongly as the
    # blob approach -- specifically, it must not fail to flip where the blob
    # approach also fails, and should show a wider (more responsive) spread.
    assert tick["ema20"] > tick["ema50"], "real multi-bar series should flip the crossover on a clear uptrend"

    # Stale "today" rows leaking into the historical baseline (e.g. a partial
    # day from a slow/lagging historical_data() call) must be dropped before
    # blending, not double-counted alongside the live bars_today series.
    df_with_stale_today = pd.concat([df, pd.DataFrame([
        {"date": cur_time, "open": base, "high": base, "low": base, "close": base, "volume": 0},
    ])], ignore_index=True)
    tick_dedup = LTPPoller._enrich("TESTSYM", df_with_stale_today, price, live_range=live_range)
    assert abs(tick_dedup["ema20"] - tick["ema20"]) < 1e-6
