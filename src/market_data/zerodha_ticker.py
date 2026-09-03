"""
Zerodha WebSocket Ticker — real-time LTP for all 40 F&O underlying stocks.

Overwrites only the 'close' (LTP) field in Redis on every tick, keeping
the historical indicators (EMA, ATR, VWAP) computed by LTPPoller intact.

Flow:
  1. fetch_instrument_tokens() — maps NSE symbols to Zerodha instrument_tokens
  2. start(loop) — launches KiteTicker in a daemon thread
  3. On connection: subscribe to all 40 tokens in MODE_QUOTE
  4. On each tick: read existing tick dict from Redis, update 'close', write back
  5. Automatic reconnection handled by KiteTicker (up to MAX_RECONNECT_ATTEMPTS)

MODE_QUOTE vs MODE_LTP (switched 2026-07-30): MODE_LTP ticks carry only
instrument_token + last_price — no volume. That silently broke the RVOL
entry filter (see the RVOL comment in live_trading_engine.py
_process_signal) and left every live-tick bar's volume hardcoded at 0.
MODE_QUOTE is the same WebSocket connection with one extra flag on
set_mode() — no separate subscription, plan, or Zerodha permission needed
— and additionally carries volume_traded (cumulative day volume),
average_traded_price, and OHLC per tick. MODE_FULL adds market depth (5
best bid/ask levels) and OI on top of that, which nothing here needs, so
MODE_QUOTE is the leaner choice for just fixing volume/RVOL.
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
        # Set by _on_connect / _on_ticks (background thread) — read by the
        # staleness watchdog in api/main.py's self-heal job (asyncio thread).
        # Single reference swaps of an immutable datetime, same cross-thread
        # access pattern already used for self._ticker/self._redis elsewhere
        # in this class — no extra locking needed.
        self._connected_at = None
        self._last_tick_at = None
        # Fixed 2026-09-03 (live incident): set in start(), BEFORE connect()
        # is even attempted -- unlike _connected_at (only set by _on_connect
        # actually firing). Confirmed live: a reconnect attempt at 08:30
        # logged "connecting..." and then NOTHING for 3.5+ hours -- no
        # on_connect, no on_error, no on_disconnect, exactly the failure
        # mode seconds_since_last_tick()'s docstring already describes from
        # the 2026-07-31 incident. But with only _last_tick_at/_connected_at
        # as fallbacks, BOTH stay None forever in this exact scenario (ticks
        # need a connect, and the reference the watchdog was built to use
        # for "connect never confirmed" requires... a confirmed connect) --
        # seconds_since_last_tick() returned None the entire time, so
        # `stale_for is not None` was never true and the watchdog's
        # os._exit(1) path was never reached. All 132 F&O symbols sat on
        # the historical-close bootstrap fallback the whole morning with
        # nothing able to detect or recover from it -- only an unrelated
        # manual deploy at 12:07 happened to restart the process and fix it.
        self._connect_attempted_at = None

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

    def set_instrument_tokens(self, tokens: Dict[str, int]) -> None:
        """
        Replace the tracked instrument-token map AND push the change to the
        LIVE WebSocket connection immediately (subscribe newly-added tokens,
        unsubscribe removed ones) -- not just update the local bookkeeping
        dicts.

        Fixed 2026-08-20 (code-review round 2, confirmed independently by 5
        of 6 finder angles as the review's most severe finding): the weekly
        active-universe recompute job used to mutate self._instrument_tokens/
        self._token_symbol directly from outside this class. That's correct
        bookkeeping, but subscribe()/set_mode() are ONLY ever called from
        _on_connect() -- an already-open WebSocket connection is never told
        about the change, so a newly-promoted symbol got real-time ticks
        only if the connection happened to reconnect for an unrelated
        reason (which, on a healthy connection, can be days or the rest of
        the session). Calling subscribe()/unsubscribe() directly here pushes
        the update immediately, the same way KiteConnect's own docs describe
        for dynamic subscription changes on an already-connected ticker.
        """
        old_tokens = set(self._instrument_tokens.values())
        new_tokens = set(tokens.values())

        self._instrument_tokens = dict(tokens)
        self._token_symbol = {v: k for k, v in tokens.items()}

        if self._ticker is None:
            return  # not started yet -- start()/_on_connect() will subscribe everything fresh

        added   = list(new_tokens - old_tokens)
        removed = list(old_tokens - new_tokens)
        try:
            if added:
                self._ticker.subscribe(added)
                self._ticker.set_mode(self._ticker.MODE_QUOTE, added)
            if removed:
                self._ticker.unsubscribe(removed)
            if added or removed:
                logger.info(
                    f"ZerodhaTicker: live-updated WebSocket subscription -- "
                    f"+{len(added)} / -{len(removed)} token(s), no reconnect needed"
                )
        except Exception as e:
            logger.error(f"ZerodhaTicker: failed to update live WebSocket subscription: {e}")

    def start(self) -> None:
        """Start KiteTicker in a background daemon thread (non-blocking)."""
        if not self._instrument_tokens:
            logger.error("ZerodhaTicker: no instrument tokens — call fetch_instrument_tokens() first.")
            return
        from src.core.utils import now_ist
        self._connect_attempted_at = now_ist()
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

    def seconds_since_last_tick(self) -> Optional[float]:
        """
        Seconds since the last real (market-hours) tick was processed, since
        the most recent successful connect if no tick has arrived yet, or
        since the most recent connection ATTEMPT if it never even confirmed
        connecting. None only if start() has genuinely never been called.

        Used by the staleness watchdog (api/main.py's self-heal job) to catch
        a connection that LOOKS fine — "WebSocket connected" logged, no
        error/disconnect callback ever fires — but has silently stopped
        delivering ticks. Confirmed live 2026-07-31: after a 403 → token
        refresh → reconnect cycle, a new connection attempt logged
        "connecting to Zerodha WebSocket..." and then nothing for 2+ hours —
        no on_connect, no on_error, no on_disconnect — leaving all 41 symbols
        on the historical-data bootstrap fallback all morning with nothing in
        the system able to detect or recover from it automatically. Only a
        full process restart (fresh KiteTicker, fresh reactor thread) fixed
        it; this watchdog gives the same recovery without needing a human to
        notice and SSH in.

        Fixed 2026-09-03 (live incident: the exact scenario above recurred
        for 3.5+ hours -- 08:30 reconnect attempt, never confirmed, all 132
        F&O symbols on the bootstrap fallback until an unrelated manual
        deploy happened to restart the process at 12:07). The fallback chain
        used to be _last_tick_at or _connected_at only -- both are set
        INSIDE callbacks (_on_ticks/_on_connect) that never fire in exactly
        this failure mode, so this returned None the entire 3.5 hours and
        the watchdog's `stale_for is not None` check was never true --
        os._exit(1) was never reached despite being built specifically for
        this. _connect_attempted_at (set in start(), before connect() is
        even attempted) closes that gap: even a connection that never
        confirms now has a real, growing "how long has this been stuck"
        duration to measure against.
        """
        from src.core.utils import now_ist
        reference = self._last_tick_at or self._connected_at or self._connect_attempted_at
        if reference is None:
            return None
        return (now_ist() - reference).total_seconds()

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
        from src.core.utils import now_ist
        self._connected_at = now_ist()
        tokens = list(self._instrument_tokens.values())
        self._ticker.subscribe(tokens)
        self._ticker.set_mode(self._ticker.MODE_QUOTE, tokens)
        logger.info(
            f"ZerodhaTicker: WebSocket connected — subscribed {len(tokens)} symbols in QUOTE mode"
        )

    def _on_ticks(self, ws, ticks) -> None:
        """Called on every tick. Updates 'close' plus the running day range
        (SECONDARY price source — see update_intraday_bar() in core/utils.py)
        in the existing Redis tick dict.

        Guarded on is_market_open(): confirmed live 2026-07-30 that Zerodha's
        WebSocket delivers ticks well outside market hours (an LTP snapshot
        appears to arrive on every reconnect regardless of whether the market
        is open — observed repeatedly ~19:30-20:00 IST, hours after the 15:30
        close). Without this guard, each of those stray ticks rolled the
        5-min bucket over and got recorded as a genuine "bar" in bars_today —
        a degenerate, flat (open=high=low=close) candle that doesn't
        represent real trading, permanently baked into that day's series
        (bounded by the day_range_date check resetting it fresh the next
        real trading day, but still live and readable for the rest of the
        evening in the meantime).
        """
        if not self._redis or not ticks:
            return
        from src.core.utils import is_market_open, now_ist, update_intraday_bar
        if not is_market_open():
            return
        self._last_tick_at = now_ist()
        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = self._token_symbol.get(token)
            if not symbol:
                continue
            ltp = tick.get("last_price", 0)
            if ltp <= 0:
                continue
            # Cumulative day volume — present since the MODE_QUOTE switch
            # (2026-07-30); None for any straggler MODE_LTP-shaped tick.
            volume_traded = tick.get("volume_traded")
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
                # Freshness stamp for ZerodhaLTPPoller's race-avoidance check
                # (see refresh_ltp()'s docstring) -- lets the REST poller tell
                # "WebSocket is actively healthy for this symbol right now"
                # from "WebSocket has gone quiet, genuine fallback needed",
                # instead of guessing from ltp_source alone (which stays
                # stuck at "zerodha_realtime" forever after the last tick,
                # even hours into an outage).
                data["_ws_last_tick_at"] = self._last_tick_at.isoformat()
                update_intraday_bar(data, ltp, volume_traded=volume_traded)
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
