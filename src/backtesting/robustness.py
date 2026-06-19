"""
Parameter Robustness Analyzer

The classic curve-fitting test: if EMA 20/50 works but 21/51 and 19/49 don't,
the strategy has no edge — it was overfit to historical data.

A robust strategy should:
  - Be profitable across a wide range of nearby parameter values
  - Show a smooth PnL surface (no isolated "lucky" peaks)
  - Have a robustness ratio ≥ 0.60 (60%+ of param combos profitable)

Algorithm:
  1. Define a grid of parameter values near the chosen optimum
  2. Run a fast backtest on each combination
  3. Compute: robustness_ratio = n_profitable / n_total
  4. Identify the stability zone (contiguous profitable region)
  5. Flag curve-fitting if: only 1-2 combos are profitable

Usage:
    analyzer = ParameterRobustnessAnalyzer(
        strategy_name="EMA_CROSSOVER",
        param_grid={
            "fast_period": [18, 19, 20, 21, 22],
            "slow_period": [45, 48, 50, 52, 55],
        },
        initial_capital=300_000,
    )
    result = await analyzer.analyze(symbol="RELIANCE", years=3)
    print(result["verdict"])          # ROBUST / MARGINAL / CURVE_FIT
    print(result["robustness_ratio"]) # 0.78 = 78% of combos profitable
"""

import itertools
import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Verdict thresholds
ROBUST_THRESHOLD   = 0.60   # ≥60% combos profitable → ROBUST
MARGINAL_THRESHOLD = 0.35   # 35–60% → MARGINAL
# < 35% → CURVE_FIT


class ParameterRobustnessAnalyzer:
    """
    Grid-searches nearby parameters and measures what fraction are profitable.
    Stateless: downloads fresh history for each analysis run.
    """

    def __init__(
        self,
        strategy_name: str,
        param_grid: Dict[str, List[Any]],
        initial_capital: float = 300_000.0,
    ):
        self.strategy_name   = strategy_name
        self.param_grid      = param_grid
        self.initial_capital = initial_capital

    # ── Public API ────────────────────────────────────────────────────────────

    async def analyze(
        self,
        symbol: str,
        years: int = 3,
    ) -> Dict:
        """
        Full robustness analysis for `symbol` over the last `years` years.

        Returns:
          {
            strategy_name   : str
            symbol          : str
            total_combos    : int
            profitable      : int
            robustness_ratio: float  (0.0–1.0)
            verdict         : "ROBUST" | "MARGINAL" | "CURVE_FIT"
            best_params     : dict
            worst_params    : dict
            results         : [ {params, profit_factor, total_pnl, trade_count}, ... ]
            pnl_surface     : dict   (for 2-param grids: rows=param1, cols=param2)
          }
        """
        import asyncio
        loop = asyncio.get_event_loop()

        df = await loop.run_in_executor(None, self._fetch_history, symbol, years)
        if df is None or df.empty:
            return {"error": f"No history found for {symbol}"}

        df = self._add_indicators(df)
        combos = list(self._param_combinations())
        logger.info(
            f"RobustnessAnalyzer: {self.strategy_name} / {symbol} "
            f"| {len(combos)} parameter combos over {years}y history"
        )

        results: List[Dict] = []
        for params in combos:
            trades  = self._simulate(df, params)
            metrics = self._calc_metrics(trades)
            results.append({
                "params":        params,
                "profit_factor": metrics["profit_factor"],
                "total_pnl":     metrics["total_pnl"],
                "win_rate":      metrics["win_rate"],
                "trade_count":   metrics["trade_count"],
                "sharpe":        metrics["sharpe_ratio"],
            })

        profitable = [r for r in results if (r["profit_factor"] or 0) > 1.0]
        ratio = len(profitable) / len(results) if results else 0.0

        if ratio >= ROBUST_THRESHOLD:
            verdict = "ROBUST"
        elif ratio >= MARGINAL_THRESHOLD:
            verdict = "MARGINAL"
        else:
            verdict = "CURVE_FIT"

        best = max(results, key=lambda r: r["profit_factor"] or 0)
        worst = min(results, key=lambda r: r["profit_factor"] or 0)

        # PnL surface (for 2D grids — heatmap-ready)
        pnl_surface = self._build_surface(results)

        logger.info(
            f"Robustness [{symbol}]: {len(profitable)}/{len(results)} profitable "
            f"({ratio:.1%}) → {verdict}"
        )

        return {
            "strategy_name":    self.strategy_name,
            "symbol":           symbol,
            "years_of_history": years,
            "total_combos":     len(results),
            "profitable":       len(profitable),
            "robustness_ratio": round(ratio, 4),
            "verdict":          verdict,
            "verdict_explanation": {
                "ROBUST":     f"≥{ROBUST_THRESHOLD:.0%} of parameter combos are profitable. Genuine edge.",
                "MARGINAL":   f"{MARGINAL_THRESHOLD:.0%}–{ROBUST_THRESHOLD:.0%} profitable. Use cautiously, widen grid.",
                "CURVE_FIT":  f"<{MARGINAL_THRESHOLD:.0%} profitable. Only the 'lucky' params work. DO NOT trade live.",
            }[verdict],
            "best_params":   best["params"],
            "worst_params":  worst["params"],
            "pnl_surface":   pnl_surface,
            "results":       results,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_history(self, symbol: str, years: int) -> Optional[pd.DataFrame]:
        import yfinance as yf
        from datetime import date, timedelta
        end   = date.today().strftime("%Y-%m-%d")
        start = (date.today() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
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
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        for span in range(15, 60):   # pre-compute all spans in range
            df[f"ema{span}"] = close.ewm(span=span, adjust=False).mean()
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr14"] = tr.rolling(14).mean()
        return df.dropna()

    def _param_combinations(self):
        keys   = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        for combo in itertools.product(*values):
            yield dict(zip(keys, combo))

    def _simulate(self, df: pd.DataFrame, params: Dict) -> List[Dict]:
        """EMA crossover simulation — same as WalkForwardTester._simulate."""
        fast_col = f"ema{params.get('fast_period', 20)}"
        slow_col = f"ema{params.get('slow_period', 50)}"
        if fast_col not in df.columns or slow_col not in df.columns:
            return []

        trades = []
        position = None
        for i in range(1, len(df)):
            prev = df.iloc[i - 1]
            curr = df.iloc[i]
            prev_cross = prev[fast_col] - prev[slow_col]
            curr_cross = curr[fast_col] - curr[slow_col]

            if prev_cross < 0 and curr_cross >= 0 and position is None:
                position = {"entry": curr["close"]}
            elif prev_cross >= 0 and curr_cross < 0 and position is not None:
                trades.append({"pnl": curr["close"] - position["entry"]})
                position = None

        if position is not None:
            trades.append({"pnl": df.iloc[-1]["close"] - position["entry"]})
        return trades

    @staticmethod
    def _calc_metrics(trades: List[Dict]) -> dict:
        if not trades:
            return {"profit_factor": 0.0, "total_pnl": 0.0, "win_rate": 0.0,
                    "trade_count": 0, "sharpe_ratio": None}
        pnls  = [t["pnl"] for t in trades]
        wins  = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gw = sum(wins)
        gl = abs(sum(losses)) if losses else 0
        pf = round(gw / gl, 4) if gl > 0 else None
        arr = np.array(pnls)
        std = np.std(arr)
        sharpe = round(float(np.mean(arr) / std * np.sqrt(252)), 4) if std > 0 else None
        return {
            "profit_factor": pf,
            "total_pnl":     round(sum(pnls), 2),
            "win_rate":      round(len(wins) / len(pnls), 4),
            "trade_count":   len(trades),
            "sharpe_ratio":  sharpe,
        }

    def _build_surface(self, results: List[Dict]) -> dict:
        """
        Build a 2D PnL surface if the grid has exactly 2 parameters.
        Returns {param1_value: {param2_value: total_pnl}} for heatmap rendering.
        """
        keys = list(self.param_grid.keys())
        if len(keys) != 2:
            return {}   # only 2-param grids produce a clean surface
        k1, k2 = keys
        surface: Dict[Any, Dict[Any, float]] = {}
        for r in results:
            v1 = r["params"][k1]
            v2 = r["params"][k2]
            surface.setdefault(v1, {})[v2] = r["total_pnl"]
        # Convert keys to strings for JSON serialisation
        return {str(k): {str(kk): vv for kk, vv in v.items()} for k, v in surface.items()}
