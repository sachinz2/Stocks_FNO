import logging
from typing import Dict, List, Any
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)

class BacktestMetrics:
    @staticmethod
    def calculate(trades: List[Dict[str, Any]], initial_capital: float) -> Dict[str, float]:
        if not trades:
            return {"net_profit": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0}

        df = pd.DataFrame(trades)
        wins = df[df['pnl'] > 0]
        losses = df[df['pnl'] <= 0]

        gross_profit = wins['pnl'].sum() if not wins.empty else 0.0
        gross_loss = abs(losses['pnl'].sum()) if not losses.empty else 0.0

        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
        win_rate = (len(wins) / len(df)) * 100

        # Equity curve for drawdown
        df['equity'] = initial_capital + df['pnl'].cumsum()
        df['peak'] = df['equity'].cummax()
        df['drawdown'] = (df['peak'] - df['equity']) / df['peak']
        max_drawdown = df['drawdown'].max() * 100

        net_profit = df['pnl'].sum()

        return {
            "net_profit": float(net_profit),
            "win_rate": float(win_rate),
            "profit_factor": float(profit_factor),
            "max_drawdown": float(max_drawdown),
            "total_trades": len(df)
        }

class BacktestEngine:
    """
    Event-driven historical backtesting engine.
    """
    def __init__(self, strategy_name: str, parameters: Dict[str, Any], initial_capital: float = 300000.0):
        from src.strategies.base import StrategyRegistry
        self.strategy = StrategyRegistry.load_strategy(strategy_name, "backtest_instance", parameters)
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.open_position: Optional[Dict[str, Any]] = None
        self.trades_history: List[Dict[str, Any]] = []

    def run(self, historical_data: pd.DataFrame) -> Dict[str, Any]:
        """
        Runs the simulation row by row. 
        Expects a pandas DataFrame with OHLCV and pre-calculated indicators.
        """
        logger.info(f"Starting backtest for {self.strategy.name} over {len(historical_data)} periods.")
        
        for index, row in historical_data.iterrows():
            data_dict = row.to_dict()
            current_price = data_dict['close']
            timestamp = data_dict.get('timestamp', index)

            # 1. Manage open position first (Check stop losses / trailing stops)
            if self.open_position:
                # Update highest/lowest price for trailing stops
                if self.open_position['side'] == 'BUY':
                    self.open_position['highest_price_reached'] = max(self.open_position.get('highest_price_reached', 0), data_dict['high'])
                else:
                    self.open_position['lowest_price_reached'] = min(self.open_position.get('lowest_price_reached', float('inf')), data_dict['low'])

                action = self.strategy.manage_position(self.open_position, current_price)
                if action == "EXIT":
                    self._close_position(current_price, timestamp)
                    continue # Skip signal generation if we just stopped out this tick

            # 2. Generate new signals
            signal = self.strategy.generate_signal(data_dict)

            # 3. Execute signals
            if signal == "BUY":
                if self.open_position and self.open_position['side'] == "SELL":
                    self._close_position(current_price, timestamp)
                if not self.open_position:
                    self._open_position("BUY", current_price, timestamp, data_dict)

            elif signal == "SELL":
                if self.open_position and self.open_position['side'] == "BUY":
                    self._close_position(current_price, timestamp)
                if not self.open_position:
                    self._open_position("SELL", current_price, timestamp, data_dict)

        # Force close open position at the end of the backtest
        if self.open_position:
            last_row = historical_data.iloc[-1]
            self._close_position(last_row['close'], last_row.get('timestamp', historical_data.index[-1]))

        logger.info(f"Backtest complete. Processed {len(self.trades_history)} trades.")
        return BacktestMetrics.calculate(self.trades_history, self.initial_capital)

    def _open_position(self, side: str, price: float, timestamp: Any, data: Dict[str, Any]):
        # Assuming 1 lot sizing for simplicity as per PRD phase 1
        qty = 1
        self.open_position = {
            "side": side,
            "avg_price": price,
            "quantity": qty,
            "entry_time": timestamp,
            "atr_at_entry": data.get("atr14", 0) # specifically for VWAP SL logic
        }

    def _close_position(self, exit_price: float, timestamp: Any):
        if not self.open_position:
            return

        entry_price = self.open_position["avg_price"]
        side = self.open_position["side"]
        qty = self.open_position["quantity"]

        # Calculate PnL (ignoring slippage/brokerage for this basic implementation)
        if side == "BUY":
            pnl = (exit_price - entry_price) * qty
        else:
            pnl = (entry_price - exit_price) * qty

        self.current_capital += pnl

        trade_record = {
            "side": side,
            "entry_time": self.open_position["entry_time"],
            "exit_time": timestamp,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": qty,
            "pnl": pnl
        }
        self.trades_history.append(trade_record)
        self.open_position = None
