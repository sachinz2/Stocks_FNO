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

from src.backtesting.walk_forward import (
    StrategyNotSimulatableError,
    _SIMULATABLE_REGISTRY_KEYS,
    _INSTANCE_ID_TO_REGISTRY_KEY,
)

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
        kite=None,
        instrument_tokens: Dict[str, int] = None,
    ):
        self.strategy_name   = strategy_name
        self.param_grid      = param_grid
        self.initial_capital = initial_capital
        self._kite           = kite
        self._tokens         = instrument_tokens or {}

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

        # Fixed 2026-08-21 (deep review): check resolvability BEFORE
        # downloading any history -- credit_spread_v1/iron_condor_v1 (non-
        # directional multi-leg options structures) can't be meaningfully
        # simulated from underlying-only daily OHLCV. Returns an explicit
        # "not supported" result, never a numeric verdict for a strategy
        # this function didn't actually simulate -- see
        # StrategyNotSimulatableError / _simulate().
        if self._resolve_strategy_registry_key() is None:
            reason = (
                f"strategy_name={self.strategy_name!r} is not a directional single-leg "
                f"strategy this daily-bar robustness simulator can drive "
                f"({sorted(_SIMULATABLE_REGISTRY_KEYS)} only). credit_spread_v1/iron_condor_v1 "
                "are non-directional multi-leg options structures -- robustness analysis for "
                "them needs real historical option-chain data this tool doesn't fetch, and isn't "
                "supported. No simulation was run; no verdict was computed."
            )
            logger.warning(f"RobustnessAnalyzer: {reason}")
            return {
                "strategy_name": self.strategy_name, "symbol": symbol,
                "verdict": "NOT_SUPPORTED", "verdict_explanation": reason,
            }

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
        from datetime import datetime as dt, timedelta
        token = self._tokens.get(symbol)
        if not self._kite or not token:
            logger.warning(f"Robustness: no kite/token for {symbol} — cannot fetch history.")
            return None
        to_date   = dt.now()
        from_date = to_date - timedelta(days=years * 365)
        try:
            records = self._kite.historical_data(
                token, from_date, to_date, "day", continuous=False, oi=False
            )
            if not records:
                return None
            df = pd.DataFrame(records)
            return df[["open", "high", "low", "close", "volume"]].dropna().reset_index(drop=True)
        except Exception as e:
            logger.warning(f"Robustness: kite.historical_data failed for {symbol}: {e}")
            return None

    @staticmethod
    def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """
        Fixed 2026-08-21 (deep review): added adx14/rvol/rvol_valid/
        ohlc_bar_key/vwap -- same daily-bar approximations as
        WalkForwardTester._add_indicators() (see its docstring for the
        rationale), needed so a REAL strategy instance's generate_signal()/
        manage_position() can drive _simulate() below.
        """
        close = df["close"]
        high  = df["high"]
        low   = df["low"]
        vol   = df["volume"]
        for span in range(15, 60):   # pre-compute all spans in range
            df[f"ema{span}"] = close.ewm(span=span, adjust=False).mean()
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr14"] = tr.rolling(14).mean()

        up_move   = high.diff()
        down_move = -low.diff()
        plus_dm   = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm  = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr_wilder = tr.ewm(alpha=1 / 14, adjust=False).mean()
        plus_di  = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_wilder.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_wilder.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        df["adx14"] = dx.ewm(alpha=1 / 14, adjust=False).mean()

        avg_vol_20 = vol.rolling(20).mean()
        df["rvol"] = vol / avg_vol_20.replace(0, np.nan)
        df["rvol_valid"] = avg_vol_20.notna()

        df["ohlc_bar_key"] = df.index.astype(str)
        df["session_vwap"] = (high + low + close) / 3
        df["vwap"] = df["session_vwap"]

        _warmup_cols = {"rvol", "rvol_valid"}
        return df.dropna(subset=[c for c in df.columns if c not in _warmup_cols])

    def _param_combinations(self):
        keys   = list(self.param_grid.keys())
        values = list(self.param_grid.values())
        for combo in itertools.product(*values):
            yield dict(zip(keys, combo))

    def _resolve_strategy_registry_key(self) -> Optional[str]:
        """Same resolution as WalkForwardTester -- see its docstring."""
        from src.strategies.base import StrategyRegistry
        name = self.strategy_name
        candidates = [name, name.upper(), _INSTANCE_ID_TO_REGISTRY_KEY.get(name.lower(), "")]
        for candidate in candidates:
            if candidate in _SIMULATABLE_REGISTRY_KEYS and StrategyRegistry.get_strategy_class(candidate):
                return candidate
        return None

    def _simulate(self, df: pd.DataFrame, params: Dict) -> List[Dict]:
        """
        Drives a REAL registered strategy instance, same fix and same
        rationale as WalkForwardTester._simulate() -- see its docstring.
        Raises StrategyNotSimulatableError for credit_spread_v1/
        iron_condor_v1 or any unresolvable strategy_name.
        """
        from src.strategies.base import StrategyRegistry

        resolved = self._resolve_strategy_registry_key()
        if resolved is None:
            raise StrategyNotSimulatableError(
                f"strategy_name={self.strategy_name!r} does not resolve to a directional "
                f"single-leg strategy this daily-bar simulator can drive "
                f"({sorted(_SIMULATABLE_REGISTRY_KEYS)} only)."
            )

        instance_id = f"_robustness_sim_{id(self)}_{resolved}"
        strategy = StrategyRegistry.load_strategy(resolved, instance_id, dict(params))
        try:
            fast_col = f"ema{getattr(strategy, 'fast_period', params.get('fast_period', 20))}"
            slow_col = f"ema{getattr(strategy, 'slow_period', params.get('slow_period', 50))}"
            if fast_col not in df.columns or slow_col not in df.columns:
                return []

            trades: List[Dict] = []
            position: Optional[Dict] = None

            for i in range(len(df)):
                row = df.iloc[i]
                current_price = float(row["close"])

                if position is None:
                    data = {
                        "symbol": self.strategy_name,
                        fast_col: row[fast_col], slow_col: row[slow_col],
                        "adx14": row.get("adx14"), "ohlc_bar_key": row.get("ohlc_bar_key"),
                        "close": current_price, "atr14": row.get("atr14"),
                        "vwap": row.get("vwap"), "session_vwap": row.get("session_vwap"),
                        "rvol": row.get("rvol"),
                        "rvol_valid": bool(row.get("rvol_valid")),
                    }
                    signal = strategy.generate_signal(data)
                    if signal in ("BUY", "SELL"):
                        position = {
                            "side": signal, "entry": current_price,
                            "peak_premium": current_price, "entry_atr": row.get("atr14"),
                        }
                else:
                    if position["side"] == "BUY":
                        position["peak_premium"] = max(position["peak_premium"], current_price)
                    else:
                        position["peak_premium"] = min(position["peak_premium"], current_price)
                    cur_pos = {
                        "avg_price": position["entry"], "peak_premium": position["peak_premium"],
                        "current_adx": row.get("adx14"), "current_close": current_price,
                        "current_ema_fast": row[fast_col], "is_call": position["side"] == "BUY",
                        "entry_underlying_price": position["entry"], "entry_atr": position["entry_atr"],
                    }
                    action = strategy.manage_position(cur_pos, current_price)
                    if action == "EXIT":
                        pnl = (current_price - position["entry"]) if position["side"] == "BUY" \
                            else (position["entry"] - current_price)
                        trades.append({"pnl": pnl})
                        position = None

            if position is not None:
                current_price = float(df.iloc[-1]["close"])
                pnl = (current_price - position["entry"]) if position["side"] == "BUY" \
                    else (position["entry"] - current_price)
                trades.append({"pnl": pnl})

            return trades
        finally:
            StrategyRegistry.unload_strategy(instance_id)

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
