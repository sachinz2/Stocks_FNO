"""
Zerodha REST-based LTP refresher — fallback when WebSocket is unavailable.

Calls kite.ltp() for all F&O symbols in a single batch request every
POLL_INTERVAL_SECONDS. Updates only the 'close' field in each symbol's
Redis tick entry, leaving indicators (EMA, ATR, VWAP) computed by LTPPoller
intact.

One kite.ltp() call handles up to 500 symbols — so all 40 F&O underlyings
cost a single API request per cycle. Well within the 10 req/sec REST limit.
"""
import asyncio
import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
_NSE_PREFIX = "NSE:"


REDIS_TOKEN_KEY = "zerodha:access_token"


class ZerodhaLTPPoller:
    """
    Near-real-time LTP refresh using kite.ltp() REST API.
    Use when KiteTicker WebSocket is unavailable.
    """

    def __init__(self, kite, redis_client, symbols: List[str]) -> None:
        self._kite   = kite
        self._redis  = redis_client
        self._instruments = [f"{_NSE_PREFIX}{s}" for s in symbols]
        self._symbol_map  = {f"{_NSE_PREFIX}{s}": s for s in symbols}
        self._permission_ok = True   # set False on first "Insufficient permission"
        self._last_known_token: Optional[str] = None

    async def _try_refresh_token(self) -> bool:
        """
        On auth failure, check Redis for a newer token (written by the 8:30 scheduler job).
        If a different token is found, update the shared kite instance so all callers benefit.
        Returns True if the token was refreshed.
        """
        try:
            token = await self._redis.get(REDIS_TOKEN_KEY)
            if token and token != self._last_known_token:
                self._kite.set_access_token(token)
                self._last_known_token = token
                logger.info("ZerodhaLTPPoller: access token refreshed from Redis — resuming.")
                return True
        except Exception as e:
            logger.debug(f"ZerodhaLTPPoller: token refresh check failed: {e}")
        return False

    async def refresh_ltp(self) -> int:
        """
        Fetch latest LTP for all symbols and update Redis.
        Returns number of symbols updated.
        Called by APScheduler every POLL_INTERVAL_SECONDS.
        """
        from src.core.utils import is_market_open
        if not self._permission_ok or not is_market_open():
            return 0

        try:
            loop  = asyncio.get_event_loop()
            quotes = await loop.run_in_executor(
                None, self._kite.ltp, self._instruments
            )
        except Exception as e:
            err = str(e)
            if "Insufficient permission" in err or "permission" in err.lower():
                self._permission_ok = False
                logger.warning(
                    "ZerodhaLTPPoller: kite.ltp() not permitted on this Zerodha plan. "
                    "LTP REST polling disabled — check Zerodha plan permissions."
                )
            elif "api_key" in err.lower() or "access_token" in err.lower():
                # Token expired — silently try Redis; log only if no fresh token yet
                refreshed = await self._try_refresh_token()
                if not refreshed:
                    logger.warning(f"ZerodhaLTPPoller: kite.ltp() failed: {e}")
            else:
                logger.warning(f"ZerodhaLTPPoller: kite.ltp() failed: {e}")
            return 0

        updated = 0
        for instrument, data in quotes.items():
            symbol = self._symbol_map.get(instrument)
            if not symbol:
                continue
            ltp = data.get("last_price", 0)
            if ltp <= 0:
                continue

            redis_key = f"tick:{symbol}"
            try:
                raw = await self._redis.get(redis_key)
                if raw:
                    tick = json.loads(raw)
                    tick["close"]      = ltp
                    tick["ltp_source"] = "zerodha_rest"
                else:
                    tick = {
                        "symbol":     symbol,
                        "close":      ltp,
                        "ltp_source": "zerodha_rest",
                    }
                await self._redis.set(redis_key, json.dumps(tick))
                updated += 1
            except Exception as e:
                logger.debug(f"ZerodhaLTPPoller: Redis write failed [{symbol}]: {e}")

        if updated:
            logger.debug(f"ZerodhaLTPPoller: refreshed LTP for {updated} symbols")
        return updated
