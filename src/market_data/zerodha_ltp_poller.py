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
from datetime import timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
_NSE_PREFIX = "NSE:"
# Fixed 2026-09-02 (live incident: momentum_v1 producing zero signals for 8
# days despite the market trending hard -- traced to RVOL being unreliable
# at the exact moment of breakout confirmation). This poller's own docstring
# claims it "updates only the close field... leaving indicators intact",
# but the implementation does a full read-modify-write of the whole tick
# JSON blob every POLL_INTERVAL_SECONDS for every symbol -- while
# ZerodhaTicker (WebSocket) does the SAME full read-modify-write on every
# tick, many times a second, to build cur_bar_volume/bars_today (see
# update_intraday_bar() in core/utils.py). A classic race: if this poller
# reads a symbol's tick dict, then WebSocket writes a fresher version
# (advancing cur_bar_volume/_last_cum_volume), then THIS poller writes back
# its own now-stale full snapshot, it silently reverts WebSocket's progress
# -- rolling _last_cum_volume backward. The next real WebSocket tick then
# computes volume_traded (now) minus the reverted _last_cum_volume as a
# single tick's "delta", potentially dumping hours of cumulative volume
# into one bar (confirmed live: KALYANKJIL's cur_bar_volume reached
# 21,195,761 -- essentially the entire day's cumulative volume -- while
# every other bar that day was in the tens/hundreds of thousands). The
# 2026-07-30 fix ("WebSocket is the sole bar-BUILDER") reduced how often
# this happened (poller no longer calls update_intraday_bar() itself) but
# did not eliminate the underlying read-modify-write race, since poller's
# blind whole-dict write can still clobber fields it never intended to
# touch. Skipping the write entirely whenever WebSocket has ticked for this
# symbol within the last few poll cycles removes the race for the ~99% of
# the session WebSocket is healthy, while still allowing this poller to act
# as a genuine fallback (the original, documented purpose) once WebSocket
# has gone quiet for real.
_WS_FRESH_WINDOW = timedelta(seconds=POLL_INTERVAL_SECONDS * 3)


REDIS_TOKEN_KEY = "zerodha:access_token"


_NFO_PREFIX = "NFO:"
# Redis key prefix for active option contract LTPs (written every 5 s)
REDIS_OPTLTP_PREFIX = "optltp:"


class ZerodhaLTPPoller:
    """
    Near-real-time LTP refresh using kite.ltp() REST API.
    Use when KiteTicker WebSocket is unavailable.

    Tracks two categories of instruments:
      • F&O underlying stocks (NSE:SYMBOL)   — fixed list, set at startup
      • Active option contracts (NFO:CONTRACT) — dynamic, added/removed as positions open/close
    """

    def __init__(self, kite, redis_client, symbols: List[str]) -> None:
        self._kite   = kite
        self._redis  = redis_client
        self._instruments = [f"{_NSE_PREFIX}{s}" for s in symbols]
        self._symbol_map  = {f"{_NSE_PREFIX}{s}": s for s in symbols}
        # Active option contracts (added dynamically when positions open)
        self._option_instruments: set = set()
        self._permission_ok = True   # set False on first "Insufficient permission"
        self._last_known_token: Optional[str] = None

    def set_symbols(self, symbols: List[str]) -> None:
        """
        Replace the tracked F&O underlying-stock set (NSE:SYMBOL instruments)
        -- these were "fixed list, set at startup" per this class's own
        docstring until 2026-08-20's dynamic active-universe recompute job
        needed to update them without a restart. Does not touch
        self._option_instruments (unaffected, tracked separately).

        Added 2026-08-20 (code review): the weekly recompute job used to
        push fresh tokens into LTPPoller/RSRanker but leave this REST
        fallback (and ZerodhaTicker, the WebSocket client) frozen at their
        startup-time symbol list -- a symbol newly promoted into the active
        universe had no real-time tick coverage at all if the WebSocket
        ever needed this fallback.
        """
        self._instruments = [f"{_NSE_PREFIX}{s}" for s in symbols]
        self._symbol_map  = {f"{_NSE_PREFIX}{s}": s for s in symbols}
        logger.info(f"ZerodhaLTPPoller: now tracking {len(symbols)} underlying(s) (active universe updated)")

    def register_option_contracts(self, contracts: List[str]) -> None:
        """
        Start tracking option contracts in real time (every 5 s).
        Call when a spread or condor position is opened.
        contracts: bare NSE F&O symbols, e.g. ['BPCL26JUL315CE', 'BPCL26JUL325CE']
        """
        for c in contracts:
            self._option_instruments.add(f"{_NFO_PREFIX}{c}")
        logger.info(f"ZerodhaLTPPoller: now tracking {len(self._option_instruments)} option contract(s)")

    def unregister_option_contracts(self, contracts: List[str]) -> None:
        """Stop tracking option contracts after a position is closed."""
        for c in contracts:
            self._option_instruments.discard(f"{_NFO_PREFIX}{c}")
        logger.info(f"ZerodhaLTPPoller: tracking {len(self._option_instruments)} option contract(s) after removal")

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
            loop  = asyncio.get_running_loop()
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
                # Only updates "close" here, deliberately NOT update_intraday_bar() —
                # ZerodhaTicker (WebSocket) is the sole bar-builder (see its _on_ticks
                # docstring). Both this REST poller and the WebSocket ran
                # update_intraday_bar() concurrently on the same Redis key from
                # 2026-07-27 to 07-30: a classic read-modify-write race (both read
                # the same JSON blob, mutate their own copy, write back — whichever
                # writes last silently discards the other's update). day_high/day_low
                # mostly self-heal (next tick re-derives the same max/min), but a lost
                # update to bars_today would silently drop a completed candle from
                # that day's series with no error, corrupting the EMA/ATR calc it
                # feeds. Single-writer (WebSocket only) removes the race entirely; the
                # cost is bars_today not advancing during a WebSocket outage (close
                # still updates via this poller) — a visible, bounded gap instead of
                # a silent, unbounded one.
                raw = await self._redis.get(redis_key)
                if raw:
                    tick = json.loads(raw)
                    # Fixed 2026-09-02: "single-writer removes the race entirely"
                    # (above) was incomplete -- this poller's own full read-
                    # modify-write of the SAME key still races with WebSocket's,
                    # and can silently revert cur_bar_volume/_last_cum_volume/
                    # bars_today to a stale snapshot even though this poller
                    # never touches those fields itself (see module-level
                    # _WS_FRESH_WINDOW comment for the full incident). Skip the
                    # write entirely when WebSocket has ticked for this symbol
                    # recently -- there is nothing for this poller to usefully
                    # add, and every write is a chance to clobber newer state.
                    _ws_stamp = tick.get("_ws_last_tick_at")
                    if _ws_stamp:
                        try:
                            from datetime import datetime
                            from src.core.utils import now_ist
                            _ws_age = now_ist() - datetime.fromisoformat(_ws_stamp)
                            if _ws_age < _WS_FRESH_WINDOW:
                                continue
                        except Exception:
                            pass
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

        # ── Active option contracts — polled every 5 s once a position is open ──
        # Fixed 2026-08-13: was kite.ltp() (last-traded-price only, no way to
        # tell if that trade was seconds or weeks ago). Confirmed live:
        # TITAN26SEP4650PE's last_price sat frozen at Rs82 (last actually
        # traded 2026-07-29, two weeks earlier) while the real, current,
        # tradeable price -- visible only in the order book -- was far
        # lower. This feeds both the dashboard's displayed PnL AND
        # credit_spread_v1/iron_condor_v1's own profit-target/stop-loss exit
        # checks, so a stale price here isn't cosmetic -- it can make a
        # strategy's exit logic blind to a position's real value. kite.quote()
        # returns last_trade_time + order book depth so staleness can
        # actually be detected; only used for the small, bounded set of
        # currently-open positions (not all 40+ underlyings above), so the
        # heavier call is not a rate-limit concern.
        if self._option_instruments:
            try:
                from src.market_data.option_chain import resolve_reliable_option_price
                opt_quotes = await loop.run_in_executor(
                    None, self._kite.quote, list(self._option_instruments)
                )
                for nfo_key, data in opt_quotes.items():
                    price = resolve_reliable_option_price(data)
                    if not price or price <= 0:
                        continue
                    contract = nfo_key.removeprefix(_NFO_PREFIX)
                    await self._redis.set(
                        f"{REDIS_OPTLTP_PREFIX}{contract}",
                        str(price),
                        ex=15,   # 15-second TTL — auto-expire stale data
                    )
                    updated += 1
            except Exception as e:
                logger.debug(f"ZerodhaLTPPoller: option LTP refresh failed: {e}")

        if updated:
            logger.debug(f"ZerodhaLTPPoller: refreshed LTP for {updated} instruments")
        return updated
