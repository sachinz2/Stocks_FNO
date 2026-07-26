"""
Zerodha WebSocket Ticker — real-time LTP for all 40 F&O underlying stocks.

Overwrites only the 'close' (LTP) field in Redis on every tick, keeping
the historical indicators (EMA, ATR, VWAP) computed by LTPPoller intact.

Flow:
  1. fetch_instrument_tokens() — maps NSE symbols to Zerodha instrument_tokens
  2. start(loop) — launches KiteTicker in a daemon thread
  3. On connection: subscribe to all 40 tokens in MODE_LTP
  4. On each tick: read existing tick dict from Redis, update 'close', write back
  5. Automatic reconnection handled by KiteTicker (up to MAX_RECONNECT_ATTEMPTS)
"""
import json
import logging
import threading
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 5   # 403 is an auth error; stop fast rather than spamming
RECONNECT_DELAY_SECONDS = 10

REDIS_TOKEN_KEY = "zerodha:access_token"  # written by scripts/zerodha_auto_auth.py at 08:30 IST
# After a 403 or exhausted reconnects, poll Redis for a fresher token instead of
# giving up for the rest of the day. 60s x 480 = 8h, covers a full trading day.
TOKEN_REFRESH_CHECK_INTERVAL_SECONDS = 60
TOKEN_REFRESH_CHECK_MAX_ATTEMPTS     = 480


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
        self._retry_lock = threading.Lock()
        self._retry_in_progress = False

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
            # threaded=True: we're already inside our own background thread (see start()
            # above), not the interpreter's main thread. KiteTicker.connect() only skips
            # installing Twisted's SIGTERM/SIGINT handlers (installSignalHandlers=False)
            # when threaded=True — with threaded=False it assumes it owns the main thread
            # and crashes with "signal only works in main thread of the main interpreter"
            # on every startup. This call becomes non-blocking (KiteTicker spawns its own
            # daemon thread for the reactor), which is fine — nothing runs after it here.
            ticker.connect(threaded=True)

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
        """Called on every tick. Updates 'close' plus the running day range
        (SECONDARY price source — see update_intraday_bar() in core/utils.py)
        in the existing Redis tick dict."""
        if not self._redis or not ticks:
            return
        from src.core.utils import update_intraday_bar
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
                update_intraday_bar(data, ltp)
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
                self._ticker.close()   # stop reconnecting on this stale token
            self._schedule_token_refresh_retry()

    def _on_reconnect(self, ws, attempts_count) -> None:
        logger.info(f"ZerodhaTicker: reconnecting (attempt {attempts_count})...")

    def _on_noreconnect(self, ws) -> None:
        logger.critical(
            f"ZerodhaTicker: max reconnect attempts ({MAX_RECONNECT_ATTEMPTS}) reached. "
            "WebSocket unavailable — ZerodhaLTPPoller REST fallback (5 s delay) remains active "
            "while we watch for a fresh token."
        )
        self._schedule_token_refresh_retry()

    # ------------------------------------------------------------------
    # Token-refresh recovery — without this, a 403/exhausted-reconnect before
    # the 08:30 daily auth job has run (e.g. yesterday's token still being used
    # at market pre-open) leaves the WebSocket permanently down for the rest of
    # the day, even once a fresh token is written to Redis. ZerodhaLTPPoller
    # (the REST fallback) already does this same check on every cycle; the
    # WebSocket ticker had no equivalent — confirmed 2026-07-17: died at 06:23
    # on a stale token and silently stayed on REST-only for the entire session.
    # ------------------------------------------------------------------

    def _schedule_token_refresh_retry(self) -> None:
        """Spawn a background thread that polls Redis for a fresher access
        token and reconnects once one appears, instead of giving up for good.
        Guarded so _on_error (403) and _on_noreconnect firing for the same
        outage can't spin up two concurrent retry threads / two WebSockets."""
        with self._retry_lock:
            if self._retry_in_progress:
                return
            self._retry_in_progress = True
        t = threading.Thread(
            target=self._retry_with_fresh_token, daemon=True, name="ZerodhaTicker-TokenRetry"
        )
        t.start()

    def _retry_with_fresh_token(self) -> None:
        import time
        try:
            redis_client = self._redis
            if redis_client is None:
                try:
                    import redis as sync_redis
                    redis_client = sync_redis.from_url(self._redis_url, decode_responses=True)
                except Exception as e:
                    logger.error(f"ZerodhaTicker: token-retry could not open Redis: {e}")
                    return

            for attempt in range(1, TOKEN_REFRESH_CHECK_MAX_ATTEMPTS + 1):
                time.sleep(TOKEN_REFRESH_CHECK_INTERVAL_SECONDS)
                try:
                    token = redis_client.get(REDIS_TOKEN_KEY)
                except Exception as e:
                    logger.debug(f"ZerodhaTicker: token-retry check failed: {e}")
                    continue
                if token and token != self._access_token:
                    logger.info(
                        f"ZerodhaTicker: fresh access token found in Redis (check #{attempt}) "
                        "— reconnecting."
                    )
                    self._access_token = token
                    self.start()
                    return

            logger.warning(
                f"ZerodhaTicker: no fresh access token after {TOKEN_REFRESH_CHECK_MAX_ATTEMPTS} "
                f"checks over {TOKEN_REFRESH_CHECK_MAX_ATTEMPTS * TOKEN_REFRESH_CHECK_INTERVAL_SECONDS // 3600}h "
                "— giving up until next process restart."
            )
        finally:
            with self._retry_lock:
                self._retry_in_progress = False
