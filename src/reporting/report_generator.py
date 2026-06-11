import pandas as pd
import numpy as np
import logging
from typing import Dict, Any, List
from datetime import datetime, timedelta

from src.database.repositories.base import BaseRepository
from src.database.models.trade import Trade

logger = logging.getLogger(__name__)

class ReportGenerator:
    """
    Reporting engine to generate daily, weekly, monthly, and strategy-specific reports.
    Computes institutional metrics: Sharpe, Profit Factor, Drawdown, Expectancy.
    """
    def __init__(self, trade_repo: BaseRepository[Trade]):
        self.trade_repo = trade_repo

    async def _fetch_trades(self, start_date: datetime, end_date: datetime, strategy_name: str = None) -> pd.DataFrame:
        trades = await self.trade_repo.get_all()
        # In production, use repo.filter() or custom async queries for date ranges.
        # Filtering in pandas for simplicity in this implementation.
        
        trade_dicts = []
        for t in trades:
            if t.exit_time and start_date <= t.exit_time <= end_date:
                if strategy_name and t.strategy_name != strategy_name:
                    continue
                trade_dicts.append({
                    "id": t.id,
                    "strategy": t.strategy_name,
                    "symbol": t.symbol,
                    "pnl": float(t.pnl),
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time
                })
        return pd.DataFrame(trade_dicts)

    def _calculate_metrics(self, df: pd.DataFrame) -> Dict[str, float]:
        if df.empty:
            return {
                "total_trades": 0, "net_profit": 0.0, "win_rate": 0.0, 
                "profit_factor": 0.0, "expectancy": 0.0, "max_drawdown": 0.0, "sharpe_ratio": 0.0
            }

        wins = df[df['pnl'] > 0]
        losses = df[df['pnl'] <= 0]

        total_trades = len(df)
        net_profit = df['pnl'].sum()
        
        gross_profit = wins['pnl'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['pnl'].sum()) if not losses.empty else 0.0

        win_rate = len(wins) / total_trades
        loss_rate = len(losses) / total_trades

        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

        avg_win = wins['pnl'].mean() if not wins.empty else 0.0
        avg_loss = abs(losses['pnl'].mean()) if not losses.empty else 0.0
        
        # Expectancy = (Win % * Avg Win) - (Loss % * Avg Loss)
        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)

        # Drawdown calculation
        df = df.sort_values(by='exit_time')
        df['cumulative_pnl'] = df['pnl'].cumsum()
        df['peak'] = df['cumulative_pnl'].cummax()
        df['drawdown'] = df['peak'] - df['cumulative_pnl']
        max_drawdown = df['drawdown'].max()

        # Simplified Sharpe Ratio (assuming Risk Free Rate = 0, using trade returns)
        # In reality, this requires percentage returns over capital, but we use absolute PnL proxy here
        mean_pnl = df['pnl'].mean()
        std_pnl = df['pnl'].std()
        # Annualization factor assumes approx 252 trading days, multiple trades a day
        sharpe_ratio = 0.0
        if std_pnl > 0 and not pd.isna(std_pnl):
            sharpe_ratio = (mean_pnl / std_pnl) * np.sqrt(252) # Proxy annualized

        return {
            "total_trades": total_trades,
            "net_profit": round(float(net_profit), 2),
            "win_rate": round(float(win_rate * 100), 2),
            "profit_factor": round(float(profit_factor), 2),
            "expectancy": round(float(expectancy), 2),
            "max_drawdown": round(float(max_drawdown), 2),
            "sharpe_ratio": round(float(sharpe_ratio), 2)
        }

    async def daily_report(self, date: datetime = None) -> Dict[str, Any]:
        if date is None:
            date = datetime.utcnow()
        start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        
        df = await self._fetch_trades(start, end)
        metrics = self._calculate_metrics(df)
        metrics["report_type"] = "DAILY"
        metrics["date"] = start.strftime("%Y-%m-%d")
        return metrics

    async def monthly_report(self, year: int, month: int) -> Dict[str, Any]:
        start = datetime(year, month, 1)
        if month == 12:
            end = datetime(year + 1, 1, 1)
        else:
            end = datetime(year, month + 1, 1)
            
        df = await self._fetch_trades(start, end)
        metrics = self._calculate_metrics(df)
        metrics["report_type"] = "MONTHLY"
        metrics["month"] = f"{year}-{month:02d}"
        return metrics

    async def strategy_report(self, strategy_name: str, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        df = await self._fetch_trades(start_date, end_date, strategy_name)
        metrics = self._calculate_metrics(df)
        metrics["report_type"] = "STRATEGY"
        metrics["strategy_name"] = strategy_name
        return metrics
