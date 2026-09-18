"""
One-off diagnostic (2026-09-18, user-reported live incident): backtest
ema_crossover_v1/momentum_v1 against PAYTM's REAL historical 5-min bars,
using production strategy parameters, to answer -- empirically, not by
guessing -- whether the regime gate excluding these strategies during
PAYTM's real multi-day RS-rank-#1 run actually cost a profitable trade, or
whether the strategies' own entry/exit logic would have produced the same
poor results seen elsewhere in their trade history even with a clean shot
at it.

Run inside the API container (needs the live Zerodha kite session already
cached in Redis for historical_data()):
    docker compose exec api python3 scripts/backtest_paytm_2026_09_18.py

Deliberately does NOT use src.backtesting.engine.BacktestEngine directly --
that engine's open-position dict only carries avg_price/quantity/entry_time/
atr_at_entry, which silently no-ops most of manage_position()'s real exit
paths (underlying-based stop/target, EMA-reversal -- 44% of ALL real
Aug 24-26 ema_crossover_v1 exits per api/main.py's own comment on that
strategy's config, trailing stop, Rs-profit-booking). FaithfulBacktestEngine
below populates the full field contract those checks actually read, matching
exactly what live_trading_engine.py passes them in production.

Caveats, stated plainly: operates on the UNDERLYING's price directly (a
BUY "trade" is long the underlying at 1x, not a real option with delta/
theta/gamma/bid-ask spread), so this answers "was the DIRECTIONAL entry/exit
logic sound," not "would the real P&L in rupees have matched." RVOL here is
computed from the full historical volume series (a rolling 20-bar average
that spans across day boundaries), not the live "today's bars only"
convention _enrich() uses intraday -- a reasonable proxy for a multi-day
batch replay, but not bit-for-bit identical to what the live engine would
have seen tick-by-tick.
"""
import sys
import json
import asyncio
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

import pandas as pd
import numpy as np


FIVE_MIN_ATR_DAILY_SCALE = 75 ** 0.5


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Replicate ltp_poller.py's _enrich() core formulas (EMA20/50, ATR14,
    ADX14/adx_valid) on a static historical OHLC frame -- no live-tick/
    intraday RVOL modeling, since this is pure historical replay."""
    close, high, low = df["close"], df["high"], df["low"]
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1.0 / 14, adjust=False).mean()

    alpha = 1.0 / 14
    hdiff = high.diff()
    ldiff = -low.diff()
    dm_plus = pd.Series(np.where((hdiff > ldiff) & (hdiff > 0), hdiff, 0.0), index=high.index, dtype=float)
    dm_minus = pd.Series(np.where((ldiff > hdiff) & (ldiff > 0), ldiff, 0.0), index=low.index, dtype=float)
    tr_w = tr.ewm(alpha=alpha, adjust=False).mean()
    dmp_w = dm_plus.ewm(alpha=alpha, adjust=False).mean()
    dmm_w = dm_minus.ewm(alpha=alpha, adjust=False).mean()
    di_p = 100.0 * dmp_w / tr_w.replace(0, np.nan)
    di_m = 100.0 * dmm_w / tr_w.replace(0, np.nan)
    dx = 100.0 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)
    adx_raw = dx.ewm(alpha=alpha, adjust=False).mean()
    df["adx14"] = adx_raw.fillna(0.0).round(2)
    df["adx_valid"] = ~adx_raw.isna()

    df["ohlc_bar_key"] = df["date"].astype(str)

    # RVOL: each bar's volume vs a trailing 20-bar average. In this fully-
    # historical batch replay every bar is already "closed" by the time the
    # strategy evaluates it (unlike live streaming, where the CURRENT bar is
    # still forming) -- so rvol and rvol_closed_bar are the same series here.
    vol = df["volume"]
    vol_avg20 = vol.rolling(20).mean()
    rvol_valid = vol_avg20.notna() & (vol_avg20 > 0)
    df["rvol"] = np.where(rvol_valid, (vol / vol_avg20).round(2), 0.0)
    df["rvol_valid"] = rvol_valid
    df["rvol_closed_bar"] = df["rvol"]
    df["rvol_closed_bar_valid"] = df["rvol_valid"]
    return df


class FaithfulBacktestEngine:
    """
    Same signal/execution loop as src.backtesting.engine.BacktestEngine, but
    the open-position dict carries every field manage_position() actually
    reads in production (see live_trading_engine.py's _check_open_option_exits
    call), including peak-premium/peak-profit-Rs tracking updated every bar --
    not just avg_price/quantity.
    """
    def __init__(self, strategy_name, parameters, symbol, initial_capital=300_000.0):
        from src.strategies.base import StrategyRegistry
        self.strategy = StrategyRegistry.load_strategy(strategy_name, f"backtest_{strategy_name}_{symbol}", parameters)
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.open_position = None
        self.trades = []
        self._activation_floor_rs = 700.0  # PROFIT_BOOKING_ACTIVATION_RS default

    def run(self, df: pd.DataFrame):
        for _, row in df.iterrows():
            data = row.to_dict()
            data["symbol"] = self.symbol
            price = data["close"]
            ts = data["date"]

            if self.open_position:
                pos = self.open_position
                is_call = pos["side"] == "BUY"
                pnl_now = (price - pos["avg_price"]) if is_call else (pos["avg_price"] - price)
                pnl_rs = pnl_now * pos["quantity"]
                pos["peak_premium"] = max(pos.get("peak_premium", pos["avg_price"]), price) if is_call \
                    else min(pos.get("peak_premium", pos["avg_price"]), price)
                if pnl_rs > self._activation_floor_rs:
                    pos["peak_profit_rs"] = max(pos.get("peak_profit_rs") or 0.0, pnl_rs)

                current_position = {
                    "avg_price": pos["avg_price"],
                    "peak_premium": pos["peak_premium"],
                    "quantity": pos["quantity"],
                    "peak_profit_rs": pos.get("peak_profit_rs"),
                    "current_adx": data.get("adx14"),
                    "current_adx_valid": data.get("adx_valid"),
                    "current_ema_fast": data.get("ema20"),
                    "current_ema_slow": data.get("ema50"),
                    "current_close": price,
                    "is_call": is_call,
                    "entry_regime": "BACKTEST",
                    "entry_underlying_price": pos["entry_underlying_price"],
                    "entry_atr": pos["entry_atr"],
                    "contract": pos["contract"],
                    "ohlc_bar_key": data.get("ohlc_bar_key"),
                }
                action = self.strategy.manage_position(current_position, price)
                if action == "EXIT":
                    self._close(price, ts, "manage_position EXIT")
                    continue

            signal = self.strategy.generate_signal(data)
            if signal == "BUY" and not self.open_position:
                self._open("BUY", price, ts, data)
            elif signal == "SELL" and not self.open_position:
                self._open("SELL", price, ts, data)

        if self.open_position:
            last = df.iloc[-1]
            self._close(last["close"], last["date"], "end of backtest")

        return self.trades

    def _open(self, side, price, ts, data):
        # entry_atr matches production's exact basis: raw atr14 * FIVE_MIN_ATR_DAILY_SCALE
        # (see live_trading_engine.py's "entry_atr": atr * _5MIN_ATR_SCALE)
        self.open_position = {
            "side": side,
            "avg_price": price,
            "quantity": 1,
            "entry_time": ts,
            "entry_underlying_price": price,
            "entry_atr": float(data.get("atr14") or 0) * FIVE_MIN_ATR_DAILY_SCALE,
            "contract": f"{self.symbol}_BT_{ts}",
            "peak_premium": price,
            "peak_profit_rs": None,
        }

    def _close(self, price, ts, reason):
        pos = self.open_position
        is_call = pos["side"] == "BUY"
        pnl = (price - pos["avg_price"]) if is_call else (pos["avg_price"] - price)
        self.trades.append({
            "side": pos["side"], "entry_time": pos["entry_time"], "exit_time": ts,
            "entry_price": pos["avg_price"], "exit_price": price, "pnl": pnl,
            "pnl_pct": pnl / pos["avg_price"] * 100, "reason": reason,
        })
        self.open_position = None


async def main():
    import redis.asyncio as aioredis
    from src.core.config import settings

    r = aioredis.from_url(settings.get_redis_url(), decode_responses=True)
    token = await r.get("zerodha:access_token")
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=settings.ZERODHA_API_KEY)
    kite.set_access_token(token)

    # Need PAYTM's instrument token
    instruments = kite.instruments("NSE")
    tok = next((i["instrument_token"] for i in instruments if i["tradingsymbol"] == "PAYTM"), None)
    if not tok:
        print("PAYTM token not found"); return

    to_date = datetime.now()
    from_date = to_date - timedelta(days=70)  # covers 50-bar EMA warmup + real test window
    records = kite.historical_data(tok, from_date, to_date, "5minute", continuous=False, oi=False)
    df = pd.DataFrame(records)
    df = df.rename(columns={"date": "date"})
    print(f"Fetched {len(df)} 5-min bars for PAYTM, {df['date'].iloc[0]} to {df['date'].iloc[-1]}")

    df = compute_indicators(df)
    # Drop warmup period (first 50 bars needed for EMA50 to stabilize)
    df_test = df.iloc[50:].reset_index(drop=True)

    results = {}
    for strat_name, instance_name, params in [
        ("EMA_CROSSOVER", "ema_crossover_v1", {
            "fast_period": 20, "slow_period": 50,
            "stop_loss_pct": 0.50, "target_pct": 1.0, "trailing_stop_pct": 0.25,
            "adx_entry_threshold": 22, "rvol_hard_gate": False,
            "mtf_strict": False, "mtf_strong_opposition_pct": 0.3, "require_rs": False,
            "ema_reversal_exit": True, "underlying_stop_atr_mult": 1.4,
            "underlying_target_atr_mult": 2.0, "ema_reversal_min_gap_pct": 0.001,
            "ema_reversal_confirm_bars": 2, "entry_min_gap_pct": 0.001,
            "entry_option_delta": None,
        }),
        ("MOMENTUM", "momentum_v1", {
            "fast_period": 20, "slow_period": 50,
            "adx_entry_threshold": 25, "adx_exit_threshold": 22,
            "adx_rising_required": True, "ema_slope_required": True,
            "extension_atr_mult": 2.5, "vwap_extension_pct": 2.5,
            "rvol_entry_threshold": 1.5, "entry_option_delta": 0.60,
            "underlying_invalidation_exit": True, "min_ema_spread_pct": 0.30,
            "stop_loss_pct": 0.50, "target_pct": 1.50, "trailing_stop_pct": 0.30,
            "use_pullback_continuation_model": True, "max_pullback_bars": 6,
            "pullback_rvol_low": 0.8, "breakout_rvol_min": 1.3,
            "underlying_stop_atr_mult": 1.0, "underlying_target_atr_mult": 2.0,
        }),
    ]:
        engine = FaithfulBacktestEngine(strat_name, params, "PAYTM")
        trades = engine.run(df_test)
        results[instance_name] = trades

    for name, trades in results.items():
        print(f"\n=== {name} on PAYTM, {len(df_test)} bars ({df_test['date'].iloc[0]} to {df_test['date'].iloc[-1]}) ===")
        print(f"Total trades: {len(trades)}")
        for t in trades:
            print(f"  {t['side']} entry={t['entry_price']:.2f}@{t['entry_time']} exit={t['exit_price']:.2f}@{t['exit_time']} pnl_pct={t['pnl_pct']:+.2f}% reason={t['reason']}")
        if trades:
            wins = [t for t in trades if t["pnl"] > 0]
            print(f"Win rate: {len(wins)}/{len(trades)} = {100*len(wins)/len(trades):.1f}%")
            print(f"Sum pnl_pct: {sum(t['pnl_pct'] for t in trades):+.2f}%")

    with open("/tmp/paytm_backtest_result.json", "w") as f:
        json.dump({k: v for k, v in results.items()}, f, default=str, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
