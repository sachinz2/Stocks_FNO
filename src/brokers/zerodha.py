import logging
from typing import Dict, Any, List, Optional
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# Assuming kiteconnect is installed (`pip install kiteconnect`)
try:
    from kiteconnect import KiteConnect
    import kiteconnect.exceptions as kite_exc
except ImportError:
    KiteConnect = None
    kite_exc = None

from src.brokers.base import AbstractBroker

logger = logging.getLogger(__name__)

class ZerodhaBroker(AbstractBroker):
    """
    Zerodha Kite Connect Broker Adapter.
    """
    def __init__(self, api_key: str, api_secret: str):
        if KiteConnect is None:
            raise ImportError("kiteconnect package is required to use ZerodhaBroker.")
            
        self.api_key = api_key
        self.api_secret = api_secret
        self.kite = KiteConnect(api_key=self.api_key)
        self.access_token: Optional[str] = None
        logger.info("Initialized ZerodhaBroker adapter.")

    def authenticate(self, request_token: str) -> bool:
        """
        Authenticate with the request token obtained from the login flow.
        """
        try:
            data = self.kite.generate_session(request_token, api_secret=self.api_secret)
            self.access_token = data["access_token"]
            self.kite.set_access_token(self.access_token)
            logger.info("Zerodha authentication successful.")
            return True
        except Exception as e:
            logger.error(f"Zerodha authentication failed: {e}")
            return False

    def refresh_token(self):
        """Kite Connect doesn't use refresh tokens exactly like OAuth, but rather a daily access token."""
        logger.warning("Zerodha access tokens are valid for one day. Full re-authentication required.")
        pass

    # Retry logic for network or rate limit issues
    @retry(
        stop=stop_after_attempt(3), 
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((kite_exc.NetworkException, kite_exc.DataException)) if kite_exc else None
    )
    async def place_order(self, symbol: str, side: str, quantity: int, price: float) -> str:
        """
        Places a limit order on NSE.
        """
        logger.info(f"Placing Zerodha order: {side} {quantity} {symbol} @ {price}")
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY if side == "BUY" else self.kite.TRANSACTION_TYPE_SELL,
                quantity=quantity,
                product=self.kite.PRODUCT_MIS, # Intraday for phase 1
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=price
            )
            logger.info(f"Successfully placed Zerodha order {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"Failed to place Zerodha order: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def cancel_order(self, order_id: str) -> bool:
        try:
            self.kite.cancel_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id
            )
            logger.info(f"Successfully cancelled Zerodha order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel Zerodha order {order_id}: {e}")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def modify_order(self, order_id: str, new_price: float, new_quantity: int) -> bool:
        try:
            self.kite.modify_order(
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id,
                quantity=new_quantity,
                price=new_price
            )
            logger.info(f"Successfully modified Zerodha order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to modify Zerodha order {order_id}: {e}")
            return False

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def get_positions(self) -> List[Dict[str, Any]]:
        try:
            positions = self.kite.positions()
            # Net positions combine overnight and intraday
            return positions.get("net", [])
        except Exception as e:
            logger.error(f"Failed to fetch Zerodha positions: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def get_orders(self) -> List[Dict[str, Any]]:
        try:
            return self.kite.orders()
        except Exception as e:
            logger.error(f"Failed to fetch Zerodha orders: {e}")
            raise
