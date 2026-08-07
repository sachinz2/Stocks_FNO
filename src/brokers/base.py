from abc import ABC, abstractmethod

class AbstractBroker(ABC):
    @abstractmethod
    def place_order(self, symbol: str, side: str, quantity: int, price: float, is_exit_order: bool = False):
        """
        is_exit_order: added 2026-08-07. Real (ZerodhaBroker) exits use a
        MARKET order regardless of `price` -- a LIMIT exit order can sit
        unfilled if the market moves away, but every exit path in
        live_trading_engine.py treats "broker accepted the order"
        (order_status == "OPEN") as "position closed": journal popped,
        capital released, trade_journal written -- with nothing left
        watching the position if it never actually filled. `price` is
        still passed through and used for entries (still LIMIT) and for
        PaperBroker's slippage simulation either way.
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str):
        pass

    @abstractmethod
    def modify_order(self, order_id: str, new_price: float, new_quantity: int):
        pass

    @abstractmethod
    def get_positions(self):
        pass

    @abstractmethod
    def get_orders(self):
        pass
