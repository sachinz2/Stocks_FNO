from abc import ABC, abstractmethod
from typing import Optional

class AbstractBroker(ABC):
    @abstractmethod
    def place_order(
        self, symbol: str, side: str, quantity: int, price: float,
        is_exit_order: bool = False, strategy_name: Optional[str] = None,
        product_override: Optional[str] = None,
    ):
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

        strategy_name: added 2026-08-07. ZerodhaBroker uses this to select
        MIS (auto-squared-off by the exchange) vs NRML (positional) --
        see its _product_for(). A CLOSING order's product type must match
        the ORIGINAL position's at Zerodha, so exit calls must pass the
        same strategy_name the entry used, not just entries.

        product_override: added 2026-08-07. For the one case where
        strategy_name genuinely can't be recovered -- closing an ORPHANED
        position (engine lost tracking, e.g. a Redis flush/crash) -- pass
        the real product string straight from the broker's own position
        record (e.g. position["product"] from Zerodha's positions() API)
        instead of guessing from a strategy name we no longer have. Takes
        precedence over strategy_name when both are given.
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
