"""
Zerodha WebSocket Ticker — real-time LTP for all 40 F&O underlying stocks.

Replaces yfinance's 15-minute delayed LTP with Zerodha's live data stream.
The LTPPoller still runs every 60 s to refresh EMA, ATR, VWAP from yfinance
history. This ticker overwrites only the 'close' (LTP) field in Redis on
every tick, keeping historical indicators fresh and LTP real-time.

Flow:
  1. fetch_instrument_tokens() — maps NSE symbols to Zerodha instrument_tokens
  2. start(loop) — launches KiteTicker in a daemon thread
  3. On connection: subscribe to all 40 tokens in MODE_LTP
  4. On each tick: read existing tick dict from Redis, update 'close', write back
  5. Automatic reconnection handled by KiteTicker (up to MAX_RECONNECT_ATTEMPTS)

Graceful degradation: if the ticker fails to connect or loses connection,
yfinance data (with 15-min delay) remains in Redis — signals still fire,
just with older LTP until reconnection succeeds.
"""
import json
import logging
import threading
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 5   # 403 is an auth error; stop fast rather than spamming
RECONNECT_DELAY_SECONDS = 10


class ZerodhaTicker:
    """Real-time NSE equity LTP via Zerodha KiteTicker WebSocket."""

    def __init__(self, api_key: str, access_token: str, redis_url: str, symbols: Set[str]):
        self._api_key = api_key
        self._access_token = access_token
        self._redis_url = redis_url
        self._symbols = symbols

        self._instrument_tokens: Dict[str, int] = {}   # symbol → token
        self._token_symbol: Dict[int, str] = {}         # token → symbol
        self._ticker = None
        self._redis = None   # sync redis client (in background thread)

    def fetch_instrument_tokens(self) -> int:
        """
        Map FNO symbol names to Zerodha instrument_tokens for subscription.
        Fetches NSE equity instruments — we track underlying prices, not options.
        Returns the number of symbols successfully mapped.
        """
        try:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=self._api_key)
            kite.set_access_token(self._access_token)
            instruments = kite.instruments("NSE")
            for inst in instruments:
                sym = inst.get("tradingsymbol", "")
                if sym in self._symbols:
                    tok = inst["instrument_token"]
                    self._instrument_tokens[sym] = tok
                    self._token_symbol[tok] = sym
            logger.info(
                f"ZerodhaTicker: mapped {len(self._instrument_tokens)}/{len(self._symbols)} instrument tokens"
            )
            missing = self._symbols - set(self._instrument_tokens.keys())
            if missing:
                logger.warning(f"ZerodhaTicker: tokens not found for: {sorted(missing)}")
            return len(self._instrument_tokens)
        except Exception as e:
            logger.error(f"ZerodhaTicker: failed to fetch instrument tokens: {e}")
            return 0

    def start(self) -> None:
        """Start KiteTicker in a background daemon thread (non-blocking)."""
        if not self._instrument_tokens:
            logger.error("ZerodhaTicker: no instrument tokens — call fetch_instrument_tokens() first.")
            return
        t = threading.Thread(target=self._run_ticker, daemon=True, name="ZerodhaTicker")
        t.start()
        logger.info("ZerodhaTicker: background thread started.")

    def stop(self) -> None:
        if self._ticker:
            try:
                self._ticker.close()
                logger.info("ZerodhaTicker: stopped.")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal — runs inside the background thread
    # ------------------------------------------------------------------

    def _run_ticker(self) -> None:
        """Entry point for the background thread. Runs KiteTicker (blocking)."""
        try:
            import redis as sync_redis
            from kiteconnect import KiteTicker

            self._redis = sync_redis.from_url(self._redis_url, decode_responses=True)

            ticker = KiteTicker(
                self._api_key,
                self._access_token,
                reconnect=True,
                reconnect_max_tries=MAX_RECONNECT_ATTEMPTS,
                reconnect_max_delay=RECONNECT_DELAY_SECONDS,
            )
            ticker.on_connect = self._on_connect
            ticker.on_ticks = self._on_ticks
            ticker.on_disconnect = self._on_disconnect
            ticker.on_error = self._on_error
            ticker.on_reconnect = self._on_reconnect
            ticker.on_noreconnect = self._on_noreconnect
            self._ticker = ticker

            logger.info("ZerodhaTicker: connecting to Zerodha WebSocket...")
            ticker.connect(threaded=False)  # blocks until close()

        except ImportError:
            logger.error("ZerodhaTicker: kiteconnect package not installed — pip install kiteconnect")
        except Exception as e:
            logger.error(f"ZerodhaTicker: unexpected error in background thread: {e}")

    def _on_connect(self, ws, response) -> None:
        tokens = list(self._instrument_tokens.values())
        self._ticker.subscribe(tokens)
        self._ticker.set_mode(self._ticker.MODE_LTP, tokens)
        logger.info(
            f"ZerodhaTicker: WebSocket connected — subscribed {len(tokens)} symbols in LTP mode"
        )

    def _on_ticks(self, ws, ticks) -> None:
        """Called on every tick. Updates only 'close' in the existing Redis tick dict."""
        if not self._redis or not ticks:
            return
        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = self._token_symbol.get(token)
            if not symbol:
                continue
            ltp = tick.get("last_price", 0)
            if ltp <= 0:
                continue
            redis_key = f"tick:{symbol}"
            try:
                raw = self._redis.get(redis_key)
                if raw:
                    data = json.loads(raw)
                    data["close"] = ltp
                    data["ltp_source"] = "zerodha_realtime"
                else:
                    # LTPPoller hasn't run yet — write minimal tick
                    data = {
                        "symbol": symbol,
                        "close": ltp,
                        "ltp_source": "zerodha_realtime",
                    }
                self._redis.set(redis_key, json.dumps(data))
            except Exception as e:
                logger.debug(f"ZerodhaTicker: Redis write failed [{symbol}]: {e}")

    def _on_disconnect(self, ws, code, reason) -> None:
        logger.warning(f"ZerodhaTicker: disconnected (code={code}): {reason}")

    def _on_error(self, ws, code, reason) -> None:
        logger.error(f"ZerodhaTicker: error (code={code}): {reason}")
        if code == 1006 and "403" in str(reason):
            logger.critical(
                "ZerodhaTicker: 403 Forbidden — WebSocket auth rejected by Zerodha. "
                "Check: (1) app streaming permissions on kite.trade, "
                "(2) re-run zerodha_auto_auth.py to refresh the access token."
            )
            if self._ticker:
                self._ticker.close()   # stop reconnecting — 403 won't fix itself

    def _on_reconnect(self, ws, attempts_count) -> None:
        logger.info(f"ZerodhaTicker: reconnecting (attempt {attempts_count})...")

    def _on_noreconnect(self, ws) -> None:
        logger.critical(
            f"ZerodhaTicker: max reconnect attempts ({MAX_RECONNECT_ATTEMPTS}) reached. "
            "Falling back to yfinance data — signals will have 15-min delay."
        )
