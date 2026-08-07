import logging
from typing import Dict, Any, List, Optional
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

try:
    from kiteconnect import KiteConnect
    import kiteconnect.exceptions as kite_exc
except ImportError:
    KiteConnect = None
    kite_exc = None

from src.brokers.base import AbstractBroker
from src.core.constants import INTRADAY_PRODUCT_STRATEGIES

logger = logging.getLogger(__name__)

# Only retry on transient/network-ish failures — anything else (bad params,
# insufficient margin, invalid order id, auth errors) will just fail the same
# way 3 times in a row, wasting up to ~7s of retry delay before reporting the
# real error. Shared by every retry-decorated method below.
_RETRYABLE_KITE_EXC = (
    (kite_exc.NetworkException, kite_exc.DataException) if kite_exc else Exception
)


class ZerodhaBroker(AbstractBroker):
    """
    Zerodha Kite Connect Broker Adapter.

    Fixes vs original:
      - Orders go to EXCHANGE_NFO (F&O segment), not EXCHANGE_NSE (cash)
      - Product type is PRODUCT_NRML (overnight margin, supports multi-day holds)
      - `from_redis_token()` classmethod to construct broker from the token
        stored by the daily auth script — used by the lifespan when TRADING_MODE=live
    """

    def __init__(self, api_key: str, api_secret: str):
        if KiteConnect is None:
            raise ImportError("kiteconnect package is required — pip install kiteconnect")
        self.api_key = api_key
        self.api_secret = api_secret
        self.kite = KiteConnect(api_key=self.api_key)
        self.access_token: Optional[str] = None
        logger.info("Initialized ZerodhaBroker.")

    @classmethod
    def from_redis_token(cls, api_key: str, api_secret: str, access_token: str) -> "ZerodhaBroker":
        """
        Construct and authenticate a ZerodhaBroker from a pre-fetched access token.
        Used at startup: the daily auth script stores the token in Redis at 08:30 IST,
        and the lifespan reads it here so the engine is ready at market open (09:15).
        """
        broker = cls(api_key, api_secret)
        broker.access_token = access_token
        broker.kite.set_access_token(access_token)
        logger.info("ZerodhaBroker: authenticated from Redis token.")
        return broker

    def authenticate(self, request_token: str) -> bool:
        """Full OAuth exchange — used for manual re-authentication."""
        try:
            data = self.kite.generate_session(request_token, api_secret=self.api_secret)
            self.access_token = data["access_token"]
            self.kite.set_access_token(self.access_token)
            logger.info("Zerodha authentication successful.")
            return True
        except Exception as e:
            logger.error(f"Zerodha authentication failed: {e}")
            return False

    def _exchange_for(self, symbol: str) -> str:
        """Options (CE/PE suffix) trade on NFO; equity underlyings trade on NSE."""
        return self.kite.EXCHANGE_NFO if symbol.endswith(("CE", "PE")) else self.kite.EXCHANGE_NSE

    def _product_for(
        self, symbol: str, strategy_name: Optional[str] = None, product_override: Optional[str] = None,
    ) -> str:
        """
        NRML for everything by default, so credit_spread_v1/iron_condor_v1
        (held for days/weeks to collect theta decay) can be held overnight.

        Fixed 2026-08-07: ema_crossover_v1/momentum_v1 are intraday-only by
        design (single-leg long options, always closed at 15:20 -- see
        _square_off_all()'s "ALWAYS close (overnight gap risk)") but were
        ALSO using NRML, meaning that guarantee rested entirely on our own
        scheduler actually running _square_off_all() that day, with no
        backstop at the exchange if it didn't (already observed multiple
        scheduler/watchdog failure modes this session). MIS gives Zerodha's
        own auto-square-off as a real exchange-level backstop for exactly
        that risk, at no margin cost -- these two strategies only ever BUY
        options, and option-buying margin is premium-only regardless of
        MIS vs NRML (the usual MIS leverage benefit is for futures/short
        options, unused here).

        product_override wins when given -- the orphan-position auto-close
        path (engine lost tracking of which strategy owns a position) has
        no strategy_name to look up, but does have the real product string
        straight from Zerodha's own position record, which is more
        reliable than any strategy-based guess anyway.
        """
        if product_override in (self.kite.PRODUCT_MIS, self.kite.PRODUCT_NRML):
            return product_override
        if strategy_name in INTRADAY_PRODUCT_STRATEGIES:
            return self.kite.PRODUCT_MIS
        return self.kite.PRODUCT_NRML

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE_KITE_EXC),
    )
    async def place_order(
        self, symbol: str, side: str, quantity: int, price: float,
        is_exit_order: bool = False, strategy_name: Optional[str] = None,
        product_override: Optional[str] = None,
    ) -> str:
        # Fixed 2026-08-07: exits always used ORDER_TYPE_LIMIT, same as
        # entries -- but every exit path in live_trading_engine.py treats
        # order_status == "OPEN" (broker accepted it) as "position closed"
        # (journal popped, capital released, trade_journal written), not
        # "position confirmed filled". A LIMIT exit order can sit unfilled
        # or partially filled if the market moves away before it crosses --
        # especially when its price came from a synthetic ATR estimate
        # rather than a real quote (a scenario confirmed to happen
        # repeatedly earlier this session). That would leave a real,
        # still-open position at the broker with nothing in the system
        # left watching it (popped from _single_leg_journals, no further
        # exit checks). MARKET orders guarantee an immediate, certain fill
        # -- standard practice for stop-loss/risk exits, where getting out
        # matters more than price. Entries are unaffected, still LIMIT.
        order_type = self.kite.ORDER_TYPE_MARKET if is_exit_order else self.kite.ORDER_TYPE_LIMIT
        logger.info(f"Zerodha: {side} {quantity} {symbol} @ {'MARKET' if is_exit_order else price}")
        try:
            kwargs = dict(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self._exchange_for(symbol),
                tradingsymbol=symbol,
                transaction_type=(
                    self.kite.TRANSACTION_TYPE_BUY
                    if side == "BUY"
                    else self.kite.TRANSACTION_TYPE_SELL
                ),
                quantity=quantity,
                product=self._product_for(symbol, strategy_name, product_override),
                order_type=order_type,
            )
            if not is_exit_order:
                kwargs["price"] = price  # MARKET orders execute at best available price, no price param
            order_id = self.kite.place_order(**kwargs)
            logger.info(f"Zerodha: order placed — id={order_id}")
            return order_id
        except Exception as e:
            logger.error(f"Zerodha: place_order failed: {e}")
            raise

    # cancel_order/modify_order must never raise — OrderManager.cancel_order()
    # calls broker.cancel_order() with no try/except around it and checks the
    # returned bool directly. Previously the @retry decorator sat on the outer
    # async method while the method's own try/except caught every exception
    # internally and returned False — so tenacity's wrapper never saw a
    # failure to retry on, and the decorator was a complete no-op (found
    # 2026-07-30). Fixed by putting @retry on an inner call that's allowed to
    # raise (so tenacity can actually retry transient failures), with the
    # outer method still catching everything after retries are exhausted and
    # returning bool exactly as before — external behavior is unchanged.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE_KITE_EXC),
    )
    def _cancel_order_call(self, order_id: str) -> None:
        self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)

    async def cancel_order(self, order_id: str) -> bool:
        try:
            self._cancel_order_call(order_id)
            return True
        except Exception as e:
            logger.error(f"Zerodha: cancel_order failed: {e}")
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE_KITE_EXC),
    )
    def _modify_order_call(self, order_id: str, new_price: float, new_quantity: int) -> None:
        self.kite.modify_order(
            variety=self.kite.VARIETY_REGULAR,
            order_id=order_id,
            quantity=new_quantity,
            price=new_price,
        )

    async def modify_order(self, order_id: str, new_price: float, new_quantity: int) -> bool:
        try:
            self._modify_order_call(order_id, new_price, new_quantity)
            return True
        except Exception as e:
            logger.error(f"Zerodha: modify_order failed: {e}")
            return False

    # get_positions/get_orders already re-raise on failure (callers wrap them
    # in their own try/except — e.g. LiveTradingEngine._safe_get_positions(),
    # OrderManager.sync_orders()), so retry already worked here — only the
    # retry scope needed narrowing to match place_order (was retrying on any
    # exception, including non-transient ones that will just fail identically
    # 3 times in a row).
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE_KITE_EXC),
    )
    async def get_positions(self) -> List[Dict[str, Any]]:
        try:
            return self.kite.positions().get("net", [])
        except Exception as e:
            logger.error(f"Zerodha: get_positions failed: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE_KITE_EXC),
    )
    async def get_orders(self) -> List[Dict[str, Any]]:
        try:
            return self.kite.orders()
        except Exception as e:
            logger.error(f"Zerodha: get_orders failed: {e}")
            raise
