"""
Walk-Forward Tester

Prevents curve-fitting by separating parameter optimization (in-sample)
from performance measurement (out-of-sample).

Standard WFA structure:
  Window 1:  Train 2020-2022 → Test 2023
  Window 2:  Train 2021-2023 → Test 2024
  Window 3:  Train 2022-2024 → Test 2025
  ...

A strategy that only works on EMA 20/50 but not on 21/51 is curve-fit.
A strategy whose OOS Sharpe tracks IS Sharpe within ±0.3 is robust.

Usage:
    tester = WalkForwardTester(
        strategy_name="EMA_CROSSOVER",
        param_grid={"fast_period": [18, 20, 22], "slow_period": [45, 50, 55]},
        train_years=2,
        test_years=1,
        initial_capital=300_000,
    )
    results = await tester.run(symbol="RELIANCE", start_year=2020, end_year=2026)
    await tester.save(results)   # persists to walk_forward_results table
    summary = tester.summary(results)
"""
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class WindowResult:
    strategy_name: str
    symbol: str
    window_start: str       # ISO date
    train_end: str
    window_end: str
    is_oos: bool
    parameters: Dict[str, Any]
    profit_factor: Optional[float]
    sharpe_ratio: Optional[float]
    max_drawdown: Optional[float]
    win_rate: Optional[float]
    total_pnl: Optional[float]
    trade_count: int
    avg_pnl: Optional[float]
    expectancy: Optional[float]


class WalkForwardTester:
    """
    Runs a walk-forward analysis for any strategy registered in StrategyRegistry.

    The tester:
      1. Divides history into overlapping (train, test) windows.
      2. For each train window, finds the best parameter set via grid search.
      3. Evaluates those best params on the subsequent test window (OOS).
      4. Stores both IS and OOS metrics so you can compare them.

    If OOS metrics are consistently close to IS metrics, the strategy is robust.
    If OOS metrics collapse vs IS, the strategy is curve-fit.
    """

    def __init__(
        self,
        strategy_name: str,
        param_grid: Dict[str, List[Any]],
        train_years: int = 2,
        test_years:  int = 1,
        initial_capital: float = 300_000.0,
    ):
        self.strategy_name   = strategy_name
        self.param_grid      = param_grid
        self.train_years     = train_years
        self.test_years      = test_years
        self.initial_capital = initial_capital

    # ── Public ────────────────────────────────────────────────────────────────

    async def run(
        self,
        symbol: str,
        start_year: int,
        end_year: int,
    ) -> List[WindowResult]:
        """
        Run full walk-forward analysis. Downloads history and iterates windows.
        Returns a list of WindowResult (both IS and OOS per window).
        """
        import asyncio
        loop = asyncio.get_event_loop()

        logger.info(
            f"WalkForward: {self.strategy_name} / {symbol} "
            f"{start_year}→{end_year} "
            f"(train={self.train_years}y, test={self.test_years}y)"
        )

        df = await loop.run_in_executor(None, self._fetch_history, symbol, start_year, end_year)
        if df is None or df.empty:
            logger.error(f"WalkForward: no history for {symbol}")
            return []

        df = self._add_indicators(df)
        windows = self._make_windows(start_year, end_year)
        results: List[WindowResult] = []

        for ws, te, we in windows:
            # In-sample: find best params
            train_df = df[(df.index >= ws) & (df.index < te)].copy()
            if len(train_df) < 50:
                logger.warning(f"WalkForward: too little data for window {ws}→{te}, skipping.")
                continue

            best_params, is_metrics = self._optimize(train_df)

            is_result = WindowResult(
                strategy_name=self.strategy_name,
                symbol=symbol,
                window_start=ws.strftime("%Y-%m-%d"),
                train_end=te.strftime("%Y-%m-%d"),
                window_end=we.strftime("%Y-%m-%d"),
                is_oos=False,
                parameters=best_params,
                **is_metrics,
            )
            results.append(is_result)

            # Out-of-sample: evaluate best params on unseen data
            test_df = df[(df.index >= te) & (df.index < we)].copy()
            if len(test_df) < 10:
                continue

            oos_metrics = self._evaluate(test_df, best_params)
            oos_result = WindowResult(
                strategy_name=self.strategy_name,
                symbol=symbol,
                window_start=ws.strftime("%Y-%m-%d"),
                train_end=te.strftime("%Y-%m-%d"),
                window_end=we.strftime("%Y-%m-%d"),
                is_oos=True,
                parameters=best_params,
                **oos_metrics,
            )
            results.append(oos_result)

            logger.info(
                f"WalkForward [{symbol}] {ws.date()}→{we.date()} "
                f"| IS PF={is_metrics['profit_factor']:.2f}  "
                f"OOS PF={oos_metrics['profit_factor']:.2f}  "
                f"params={best_params}"
            )

        return results

    async def save(self, results: List[WindowResult]) -> None:
        """Persist results to the walk_forward_results table."""
        from src.database.connection import AsyncSessionLocal
        from src.database.models.walk_forward import WalkForwardResult
        from src.database.repositories.base import BaseRepository

        repo = BaseRepository(WalkForwardResult, AsyncSessionLocal)
        for r in results:
            await repo.create({
                "strategy_name": r.strategy_name,
                "symbol":        r.symbol,
                "window_start":  r.window_start,
                "train_end":     r.train_end,
                "window_end":    r.window_end,
                "is_oos":        1 if r.is_oos else 0,
                "parameters":    r.parameters,
                "profit_factor": r.profit_factor,
                "sharpe_ratio":  r.sharpe_ratio,
                "max_drawdown":  r.max_drawdown,
                "win_rate":      r.win_rate,
                "total_pnl":     r.total_pnl,
                "trade_count":   r.trade_count,
                "avg_pnl":       r.avg_pnl,
                "expectancy":    r.expectancy,
            })
        logger.info(f"WalkForward: saved {len(results)} window results.")

    def summary(self, results: List[WindowResult]) -> dict:
        """
        Aggregate IS vs OOS comparison — the key robustness check.

        A robust strategy has:
          OOS PF consistently above 1.0
          OOS Sharpe close to IS Sharpe (degradation ratio ≈ 0.7–1.0)
          OOS win rate not dramatically lower than IS win rate
        """
        is_results  = [r for r in results if not r.is_oos]
        oos_results = [r for r in results if r.is_oos]

        def agg(rs, field):
            vals = [getattr(r, field) for r in rs if getattr(r, field) is not None]
            return round(float(np.mean(vals)), 4) if vals else None

        is_pf  = agg(is_results,  "profit_factor")
        oos_pf = agg(oos_results, "profit_factor")
        is_sr  = agg(is_results,  "sharpe_ratio")
        oos_sr = agg(oos_results, "sharpe_ratio")

        degradation_ratio = round(oos_pf / is_pf, 3) if is_pf and oos_pf and is_pf > 0 else None

        verdict = "UNKNOWN"
        if degradation_ratio is not None and oos_pf is not None:
            if oos_pf >= 1.2 and degradation_ratio >= 0.65:
                verdict = "ROBUST"
            elif oos_pf >= 1.0 and degradation_ratio >= 0.50:
                verdict = "MARGINAL"
            else:
                verdict = "CURVE_FIT"

        return {
            "strategy":               self.strategy_name,
            "windows":                len(is_results),
            "avg_is_profit_factor":   is_pf,
            "avg_oos_profit_factor":  oos_pf,
            "avg_is_sharpe":          is_sr,
            "avg_oos_sharpe":         oos_sr,
            "degradation_ratio":      degradation_ratio,   # oos_pf / is_pf
            "avg_oos_win_rate":       agg(oos_results, "win_rate"),
            "avg_oos_drawdown":       agg(oos_results, "max_drawdown"),
            "verdict":                verdict,
            "verdict_explanation": {
                "ROBUST":     "OOS PF ≥ 1.2 and degradation ratio ≥ 0.65 — genuine edge.",
                "MARGINAL":   "OOS PF ≥ 1.0 but significant degradation — run more windows.",
                "CURVE_FIT":  "OOS metrics collapse vs IS — strategy is curve-fit. DO NOT trade live.",
                "UNKNOWN":    "Insufficient data to classify.",
            }.get(verdict, ""),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_history(self, symbol: str, start_year: int, end_year: int) -> Optional[pd.DataFrame]:
        import yfinance as yf
        start = f"{start_year}-01-01"
        end   = f"{end_year}-12-31"
        for suffix in (".NS", ".BO"):
            try:
                df = yf.download(
                    f"{symbol}{suffix}", start=start, end=end, interval="1d",
                    auto_adjust=True, progress=False,
                )
                if not df.empty:
                    df = df.rename(columns={
                        "Open": "open", "High": "high",
                        "Low": "low", "Close": "close", "Volume": "volume",
                    })
                    return df[["open", "high", "low", "close", "volume"]].dropna()
            except Exception:
                pass
        return None

    @staticmethod
    def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """Pre-compute all indicators used by any strategy."""
        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # Multiple EMA spans for grid search
        for span in [18, 19, 20, 21, 22, 45, 48, 50, 52, 55]:
            df[f"ema{span}"] = close.ewm(span=span, adjust=False).mean()

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr14"] = tr.rolling(14).mean()

        df["returns"] = close.pct_change()
        return df.dropna()

    def _make_windows(self, start_year: int, end_year: int):
        """Generate (window_start, train_end, window_end) tuples."""
        windows = []
        wy = date(start_year, 1, 1)
        while True:
            te = date(wy.year + self.train_years, wy.month, wy.day)
            we = date(te.year + self.test_years,  te.month,  te.day)
            if we.year > end_year:
                break
            windows.append((
                pd.Timestamp(wy),
                pd.Timestamp(te),
                pd.Timestamp(we),
            ))
            wy = date(wy.year + 1, 1, 1)   # slide by 1 year
        return windows

    def _optimize(self, df: pd.DataFrame) -> Tuple[Dict, dict]:
        """
        Grid search over param_grid on df. Returns (best_params, metrics).
        Optimizes by Profit Factor (good proxy for edge quality).
        """
        best_pf     = -1.0
        best_params = {}
        best_metrics: dict = {}

        for params in self._param_combinations():
            trades = self._simulate(df, params)
            m = self._calc_metrics(trades)
            if m["profit_factor"] is not None and m["profit_factor"] > best_pf:
                best_pf      = m["profit_factor"]
                best_params  = params
                best_metrics = m

        return best_params, best_metrics

    def _evaluate(self, df: pd.DataFrame, params: Dict) -> dict:
        trades = self._simulate(df, params)
        return self._calc_metrics(trades)

    def _param_combinations(self):
        """Cartesian product of param_grid."""
        import itertools
        keys   = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        for combo in itertools.product(*values):
            yield dict(zip(keys, combo))

    def _simulate(self, df: pd.DataFrame, params: Dict) -> List[Dict]:
        """
        Simple EMA crossover simulation on daily bars.
        Extend this to support credit spread / condor if needed.
        """
        fast = params.get("fast_period", 20)
        slow = params.get("slow_period", 50)

        fast_col = f"ema{fast}"
        slow_col = f"ema{slow}"

        if fast_col not in df.columns or slow_col not in df.columns:
            return []

        trades: List[Dict] = []
        position = None

        for i in range(1, len(df)):
            prev = df.iloc[i - 1]
            curr = df.iloc[i]

            prev_cross = prev[fast_col] - prev[slow_col]
            curr_cross = curr[fast_col] - curr[slow_col]

            # Entry: EMA fast crosses above slow
            if prev_cross < 0 and curr_cross >= 0 and position is None:
                position = {"side": "BUY", "entry": curr["close"], "idx": i}

            # Exit: EMA fast crosses below slow
            elif prev_cross >= 0 and curr_cross < 0 and position is not None:
                pnl = curr["close"] - position["entry"]
                trades.append({"pnl": pnl, "entry": position["entry"], "exit": curr["close"]})
                position = None

        # Force close at end
        if position is not None:
            pnl = df.iloc[-1]["close"] - position["entry"]
            trades.append({"pnl": pnl, "entry": position["entry"], "exit": df.iloc[-1]["close"]})

        return trades

    @staticmethod
    def _calc_metrics(trades: List[Dict]) -> dict:
        if not trades:
            return {
                "profit_factor": 0.0, "sharpe_ratio": None,
                "max_drawdown": 0.0, "win_rate": 0.0,
                "total_pnl": 0.0, "trade_count": 0,
                "avg_pnl": 0.0, "expectancy": 0.0,
            }

        pnls  = [t["pnl"] for t in trades]
        wins  = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        gross_wins   = sum(wins)   if wins   else 0.0
        gross_losses = abs(sum(losses)) if losses else 0.0
        pf = round(gross_wins / gross_losses, 4) if gross_losses > 0 else None

        win_rate  = len(wins) / len(pnls)
        avg_win   = np.mean(wins)   if wins   else 0.0
        avg_loss  = abs(np.mean(losses)) if losses else 0.0
        expectancy = avg_win * win_rate - avg_loss * (1 - win_rate)

        # Sharpe on daily PnL (annualized, 252 trading days)
        pnl_series = np.array(pnls)
        std        = np.std(pnl_series)
        sharpe     = round(float(np.mean(pnl_series) / std * np.sqrt(252)), 4) if std > 0 else None

        # Max drawdown from cumulative PnL
        cum   = np.cumsum(pnl_series)
        peak  = np.maximum.accumulate(cum)
        dd    = peak - cum
        max_dd = round(float(dd.max()), 4) if len(dd) > 0 else 0.0

        return {
            "profit_factor": pf,
            "sharpe_ratio":  sharpe,
            "max_drawdown":  max_dd,
            "win_rate":      round(win_rate, 4),
            "total_pnl":     round(sum(pnls), 2),
            "trade_count":   len(trades),
            "avg_pnl":       round(float(np.mean(pnls)), 2),
            "expectancy":    round(float(expectancy), 4),
        }
