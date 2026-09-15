"""
MarketRegimeDetector — classify the current market environment.

Four regimes:
  TRENDING    — strong directional move; EMA crossover strategies shine
  RANGE_BOUND — low ATR, flat EMAs; iron condors + short strangles shine
  VOLATILE    — VIX spike / large ATR; credit spreads (IV crush plays) shine
  LOW_VOL     — very quiet; all premium-selling strategies work well

Detection logic:
  1. India VIX (from Redis, written by ZerodhaLTPPoller or estimated)
  2. Market-wide avg ATR% (mean across the 40 F&O underlyings, daily-scaled — no NIFTY50
     index tick is subscribed, so this is the proxy; see LTPPoller "market:trend_stats")
  3. Market-wide avg EMA20/50 spread% (same source)

Regime is published to Redis key `market:regime` (JSON) every cycle and
consumed by LiveTradingEngine for strategy regime-switching.

Hysteresis on the TRENDING/RANGE_BOUND boundary (added 2026-07-30): confirmed
live on 2026-07-30 that when market-wide ATR% sits right on ATR_TREND_THRESHOLD
(1.5%), the regime flips back and forth every 1-3 minutes — observed a 50-min
stretch (12:00-12:50) oscillating between 1.37% and 1.58%, flipping the regime
~15 times. Each flip pauses/resumes ema_crossover_v1; a strategy that only gets
1-3 min "active" windows (less than one 5-min bar most of the time) can barely
ever observe two consecutive confirmed bars, even if a real crossover were
forming right then. _classify() now takes the previous regime and applies a
lower exit threshold (ATR_TREND_EXIT_THRESHOLD) once already in TRENDING, so a
transient dip back toward 1.5% doesn't immediately kick it back out — the same
debouncing principle EMACrossoverStrategy already applies to its own signal
via signal_confirm_bars.

Usage:
    detector = MarketRegimeDetector(redis_client)
    regime   = await detector.detect()       # e.g. "TRENDING"
    mapping  = detector.strategy_map()       # {regime: [strategy_ids]}
"""
import asyncio
import json
import logging
from datetime import timedelta
from typing import Dict, Optional

import pandas as pd

from src.core.constants import FIVE_MIN_ATR_DAILY_SCALE
from src.core.utils import now_ist
from src.market_data.zerodha_ticker import REDIS_NIFTY_TICK_KEY

logger = logging.getLogger(__name__)

# ── Thresholds (tunable) ─────────────────────────────────────────────────────
VIX_LOW_THRESHOLD    = 12.0   # below = LOW_VOL
VIX_HIGH_THRESHOLD   = 20.0   # above = VOLATILE
# Compared against a DAILY-scaled ATR% (see FIVE_MIN_ATR_DAILY_SCALE) — do not compare
# this threshold directly against raw 5-min-bar ATR%, which runs an order of magnitude
# smaller (typically 0.2-0.4%) and would make TRENDING nearly unreachable.
ATR_TREND_THRESHOLD  = 1.5    # daily-equivalent ATR% above = trending (within mid-VIX band)
# Hysteresis: once already in TRENDING, require ATR% to drop below this LOWER
# bar before reverting — not the same 1.5% entry line — so noise oscillating
# around 1.5% doesn't flip the regime every cycle (confirmed live 2026-07-30:
# ATR% bounced 1.37%-1.58% for 50 minutes, flipping the regime ~15 times).
ATR_TREND_EXIT_THRESHOLD = 1.3
EMA_FLAT_THRESHOLD   = 0.15   # EMA spread% below = range-bound / flat

REDIS_REGIME_KEY      = "market:regime"
REDIS_TREND_STATS_KEY = "market:trend_stats"  # written by LTPPoller — market-wide avg ATR%/EMA-spread%,
                                                # kept for api/main.py's /health F&O-universe-liveness
                                                # reporting -- no longer the regime-classification input
                                                # (see REDIS_NIFTY_REGIME_INPUTS_KEY / 2026-09-15 fix)
REDIS_VIX_KEY         = "market:india_vix"    # matches option_chain.fetch_and_cache_vix()
REDIS_NIFTY_REGIME_INPUTS_KEY = "market:nifty_regime_inputs"  # written by refresh_nifty_regime_inputs()

# Strategy IDs that must exactly match what StrategyRegistry uses
STRATEGY_EMA       = "ema_crossover_v1"
STRATEGY_SPREAD    = "credit_spread_v1"
STRATEGY_CONDOR    = "iron_condor_v1"
STRATEGY_MOMENTUM  = "momentum_v1"

# Regime → which strategies should be ACTIVE
#
# Credit spreads are DIRECTIONAL — the engine picks BULL_PUT in an uptrend and
# BEAR_CALL in a downtrend, so the short strike is always placed *away* from the
# price move.  This makes credit spreads safer in TRENDING markets than condors.
# Iron condors are NEUTRAL — they need flat price action, so they are excluded
# from TRENDING and VOLATILE where one wing reliably gets blown out.
# Momentum (added 2026-07-30) only makes sense in TRENDING — its entire thesis
# is an already-strong, established trend (high ADX), which by definition isn't
# present in RANGE_BOUND/LOW_VOL.
#
# EMA crossover ran in VOLATILE (added 2026-07-31 — was excluded entirely
# before). India VIX is a fear/uncertainty gauge, not a directional one —
# VIX>20 empirically correlates with sharp SELLOFFS, not bull sprints
# (confirmed: this system's own ~4-week history never saw VIX above 15.03, so
# a genuine spike has never been observed live here — this is a deliberate
# risk decision, not something validated against this system's own data yet).
# live_trading_engine.py._process_signal() restricts entries to PE (bearish)
# only while VOLATILE is active — no CE/long-call entries — and gets a
# tightened, VOLATILE-specific reversal exit (see
# EMACrossoverStrategy.manage_position()'s EMA-reversal check) so a position
# opened to catch a crash gets closed fast if the move V-reverses, rather
# than riding out the same slower thresholds used in a normal trending market.
#
# Fixed 2026-08-21 (external PDF review, momentum_v1 redesign round 2):
# momentum_v1 was ALSO enabled in VOLATILE alongside EMA crossover, but the
# review argued a momentum-continuation strategy is a specifically bad fit
# for VOLATILE regardless of the PE-only/tightened-exit guardrails — VIX
# spikes are exactly the environment where "the trend that already ran is
# about to violently reverse" is most likely, the opposite of momentum_v1's
# entire thesis ("this trend will continue"). credit_spread_v1's short-premium
# structure profits from richer VIX premium regardless of direction, and
# EMA crossover's PE-only + reversal-exit guardrails were judged by the
# review as adequate for a moment-of-crossing signal; a trend-continuation
# strategy on an already-extended move was not. momentum_v1 is removed from
# VOLATILE entirely — it now only runs in TRENDING.
REGIME_STRATEGY_MAP: Dict[str, list] = {
    "TRENDING":    [STRATEGY_EMA, STRATEGY_SPREAD, STRATEGY_MOMENTUM],  # spread aligned with trend = low breach risk
    "RANGE_BOUND": [STRATEGY_CONDOR, STRATEGY_SPREAD],   # both premium sellers thrive in flat market
    "VOLATILE":    [STRATEGY_SPREAD, STRATEGY_EMA],      # high IV = rich premium for spreads;
                                                           # EMA is PE-only, crash-catching. momentum_v1
                                                           # excluded (see comment above) -- a trend-
                                                           # continuation thesis is the wrong bet on a
                                                           # regime defined by imminent violent reversal.
    # Fixed 2026-09-03 (live incident: iron_condor_v1 had only 4 trades ever,
    # last on 2026-07-13): iron_condor_v1 used to be listed here too, but
    # LOW_VOL is DEFINED as vix < VIX_LOW_THRESHOLD (12.0, see _classify()
    # above) while iron_condor_v1's own entry gate requires
    # vix_allows_selling() -- vix >= 12.0 -- to consider premium rich enough
    # to sell. Those two conditions are mutually exclusive by construction:
    # whenever the regime classifier puts the market in LOW_VOL, iron_condor_v1
    # is, by definition, looking at a VIX that will always fail its own entry
    # gate. "Eligible in LOW_VOL" was fiction -- confirmed live via 8 days of
    # logs (2026-08-12 to 08-21) with substantial LOW_VOL time every day and
    # ZERO minutes of RANGE_BOUND, during which 96% of iron_condor_v1's
    # skip-log lines were exactly this VIX-too-low block. RANGE_BOUND (VIX
    # >= 12, ATR% low) is the only regime where iron_condor_v1's own gate can
    # actually pass, so it's the only one listed now. credit_spread_v1 has
    # the same VIX>=12 entry gate but doesn't have this problem -- it's also
    # eligible in TRENDING/VOLATILE, where VIX tends to sit >=12 anyway, so
    # it has regimes to fall back on that iron_condor_v1 structurally lacks.
    "LOW_VOL":     [STRATEGY_SPREAD],   # quiet market = premium seller heaven (credit_spread_v1 only --
                                          # iron_condor_v1's own VIX>=12 gate can never pass here, see above)
}


async def refresh_nifty_regime_inputs(kite, redis, nifty_token: Optional[int]) -> bool:
    """
    Compute real NIFTY 50 ATR14%/EMA20-50-spread% and publish to
    REDIS_NIFTY_REGIME_INPUTS_KEY -- the real, index-specific input
    MarketRegimeDetector._get_market_indicators() now reads for GLOBAL
    regime classification, replacing the cross-sectional 40-stock average
    proxy (REDIS_TREND_STATS_KEY, still published separately by LTPPoller
    for its own unrelated F&O-universe-health purpose -- see
    api/main.py's /health endpoint).

    Blends a historical 5-min baseline (kite.historical_data() -- reliable
    for anything not-today; Zerodha confirmed it lags same-day intraday
    candles by 5+ hours, see ltp_poller.py's module docstring) with the
    real, WebSocket-accumulated intraday bars in market:nifty_tick (written
    by ZerodhaTicker._handle_nifty_tick(), same update_intraday_bar()
    machinery used per F&O stock -- deliberately NOT a single "today" blob
    bar, which this codebase already proved too weak to move EMA20/50
    meaningfully, see ltp_poller.py's own historical note).

    Returns True on success, False on any failure (missing token/kite,
    insufficient bars, API error) -- deliberately does NOT publish a
    fabricated fallback on failure. regime_detector.detect()'s UNKNOWN
    fallback (2026-09-15 fix) already handles a missing/stale key correctly
    by refusing to guess, which is the entire point of this being a
    real-data-or-nothing feed.
    """
    if not kite or not nifty_token:
        return False
    try:
        loop = asyncio.get_running_loop()
        to_date = now_ist().replace(tzinfo=None)
        from_date = to_date - timedelta(days=10)
        bars = await loop.run_in_executor(
            None,
            lambda: kite.historical_data(nifty_token, from_date, to_date, "5minute"),
        )
        df = pd.DataFrame(bars) if bars else pd.DataFrame()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            # Drop any of today's own bars historical_data() happens to
            # return -- unreliable/lagged for the CURRENT session (see
            # docstring above). Today's real bars come from the live-tick
            # feed below instead, same split ltp_poller.py uses per stock.
            today_str = now_ist().date().isoformat()
            df = df[df["date"].apply(lambda d: d.date().isoformat() != today_str)]

        raw = await redis.get(REDIS_NIFTY_TICK_KEY)
        live = json.loads(raw) if raw else None
        live_rows = []
        if live:
            for bar in (live.get("bars_today") or []):
                live_rows.append({
                    "date": bar["date"], "open": bar["open"],
                    "high": bar["high"], "low": bar["low"], "close": bar["close"],
                })
            if live.get("cur_bar_open") is not None:
                live_rows.append({
                    "date": now_ist().isoformat(),
                    "open": live.get("cur_bar_open"), "high": live.get("cur_bar_high"),
                    "low": live.get("cur_bar_low"), "close": live.get("close"),
                })
        if live_rows:
            df = pd.concat([df, pd.DataFrame(live_rows)], ignore_index=True)

        if len(df) < 50:
            logger.warning(
                f"NIFTY regime feed: insufficient bars ({len(df)}, need 50) "
                "-- not publishing, global regime stays UNKNOWN until this resolves."
            )
            return False

        close, high, low = df["close"], df["high"], df["low"]
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr14 = float(tr.ewm(alpha=1.0 / 14, adjust=False).mean().iloc[-1])
        last_close = float(close.iloc[-1])

        atr_pct_daily = round((atr14 / last_close * 100) * FIVE_MIN_ATR_DAILY_SCALE, 4) if last_close > 0 else 0.0
        ema_spread_pct = round(abs(ema20 - ema50) / ema50 * 100, 4) if ema50 > 0 else 0.0

        await redis.set(REDIS_NIFTY_REGIME_INPUTS_KEY, json.dumps({
            "atr_pct_daily":  atr_pct_daily,
            "ema_spread_pct": ema_spread_pct,
            "close":          last_close,
            "timestamp":      now_ist().replace(tzinfo=None).isoformat(),
        }), ex=180)
        return True
    except Exception as e:
        logger.warning(f"NIFTY regime feed refresh failed (non-critical, global regime stays UNKNOWN): {e}")
        return False


class MarketRegimeDetector:
    """
    Classifies the current market regime and optionally enforces
    strategy activation/deactivation via StrategyRegistry.
    """

    def __init__(self, redis_client):
        self._redis = redis_client

    # ── Public API ────────────────────────────────────────────────────────────

    async def detect(self) -> str:
        """
        Classify the current regime. Writes result to Redis and returns it.

        Fixed 2026-09-15 (external review): used to fall back to RANGE_BOUND
        whenever real VIX and/or market:trend_stats were missing, by
        silently classifying against the SAME hardcoded default indicator
        values (vix=15.0, atr_pct=1.0, ema_spread=0.15) that
        _get_market_indicators()/_get_vix() have always used as fill-ins.
        RANGE_BOUND is not a neutral "don't know" -- it's the one regime
        that auto-permits credit_spread_v1/iron_condor_v1 entries. This is a
        DIFFERENT bug from the already-fixed get_cached_regime() (which only
        protects against Redis itself being unreachable): detect() runs
        periodically and ALWAYS wrote a real, confidently-classified-looking
        regime to Redis even when the indicators feeding it were fabricated
        defaults, so get_cached_regime() would then correctly read back a
        real (but bogus) "RANGE_BOUND" -- passing right through that
        earlier fix. Now publishes the real, explicit "UNKNOWN" when either
        input is missing; REGIME_STRATEGY_MAP.get("UNKNOWN", []) is
        naturally empty, so enforce_regime_switching() pauses every
        currently-active strategy's NEW ENTRIES (exits are unaffected, per
        its own docstring) until real data resumes -- no separate wiring
        needed anywhere else.
        """
        vix, atr_pct, ema_spread_pct, data_known = await self._get_market_indicators()
        prev_regime = await self.get_cached_regime()
        if not data_known:
            regime = "UNKNOWN"
            logger.warning(
                "MarketRegimeDetector: VIX and/or market:trend_stats "
                "unavailable -- publishing UNKNOWN regime (blocks new "
                "entries for all regime-gated strategies; exits unaffected) "
                "instead of guessing against fabricated default indicators."
            )
        else:
            regime = self._classify(vix, atr_pct, ema_spread_pct, prev_regime)

        payload = {
            "regime":            regime,
            "vix":               vix,
            "market_atr_pct":    atr_pct,
            "market_ema_spread": ema_spread_pct,
            # IST-naive (was datetime.utcnow() until 2026-08-06) — the
            # dashboard's Strategies page displays this labelled "IST"
            # (app.py: f"...as of {regime_ts} IST") with no conversion, so it
            # was showing a time 5.5h behind actual IST. Same bug class as
            # orders.created_at/audit_logs.timestamp, just in a different
            # module that grep missed the first pass.
            "timestamp":         now_ist().replace(tzinfo=None).isoformat(),
        }
        await self._redis.set(REDIS_REGIME_KEY, json.dumps(payload))
        logger.info(
            f"Market regime: {regime} | VIX={vix:.1f} "
            f"ATR%={atr_pct:.2f} EMA_spread%={ema_spread_pct:.2f}"
        )
        return regime

    async def enforce_regime_switching(self) -> None:
        """
        Read current regime and enable/disable strategies accordingly.
        Paused strategies continue to run exit logic — only new entries are blocked.
        """
        from src.strategies.base import StrategyRegistry

        regime = await self.get_cached_regime()
        if regime is None:
            logger.warning(
                "RegimeSwitching: regime unknown (Redis miss/error) -- "
                "skipping enforcement this cycle rather than guessing. "
                "No strategy paused or resumed on unverified data."
            )
            return
        active   = StrategyRegistry.get_active_strategies()
        should_run = set(REGIME_STRATEGY_MAP.get(regime, []))

        for sid, instance in active.items():
            should_be_active = sid in should_run
            if instance.is_active and not should_be_active:
                StrategyRegistry.pause_strategy(
                    sid, reason=f"Regime is {regime} — strategy not active in this regime", source="regime",
                )
                logger.warning(
                    f"RegimeSwitching: PAUSED {sid} — "
                    f"regime={regime} not in its allowed set"
                )
            # Fixed 2026-08-28 (live incident): only resume a strategy THIS
            # mechanism paused. Without the paused_by check, a
            # StrategyMonitor auto-kill (real, statistically proven poor
            # performance -- e.g. ema_crossover_v1's rolling PF 0.063) got
            # immediately un-done here every cycle the regime happened to
            # still allow the strategy, since this only ever checked
            # is_active/should_be_active with no regard for WHY it was
            # paused. Confirmed live: pause/resume fired every ~60s for 90+
            # consecutive minutes -- evaluate_all() and this both run BEFORE
            # the entry-signal loop each cycle, so the strategy was actually
            # active by the time new entries were evaluated, completely
            # defeating the circuit breaker rather than just flapping
            # cosmetically. A monitor- or manually-paused strategy now stays
            # paused regardless of regime until explicitly resumed via the
            # API (StrategyMonitor's own established convention).
            elif not instance.is_active and should_be_active and getattr(instance, "paused_by", None) == "regime":
                StrategyRegistry.resume_strategy(sid)
                logger.info(
                    f"RegimeSwitching: RESUMED {sid} — "
                    f"regime={regime} is in its allowed set"
                )

    async def get_cached_regime(self) -> Optional[str]:
        """
        Read regime from Redis. Returns None if no data yet or on error.

        Fixed 2026-09-03 (external review): used to default to "RANGE_BOUND"
        on any miss/error -- silently turning "we genuinely don't know the
        regime" into "we know it's RANGE_BOUND", the specific regime that
        auto-permits iron_condor_v1/credit_spread_v1 entries. A Redis blip
        (or, more narrowly, the brief window inside enforce_regime_switching()
        between detect()'s write and this method's own independent read)
        would silently un-pause a regime-ineligible strategy with zero real
        basis for it -- the opposite of this codebase's fail-closed
        convention used everywhere else (RS, MTF, lot size, contract
        resolution, ADX validity). Both call sites already handle None
        correctly: _classify()'s prev_regime=="TRENDING" comparison is
        naturally False for None (same safe, stricter threshold as before),
        and enforce_regime_switching() now explicitly skips enforcement
        this cycle rather than guessing.
        """
        try:
            raw = await self._redis.get(REDIS_REGIME_KEY)
            if raw:
                return json.loads(raw).get("regime")
        except Exception:
            pass
        return None

    async def get_regime_report(self) -> dict:
        """Full regime payload for the API."""
        try:
            raw = await self._redis.get(REDIS_REGIME_KEY)
            if raw:
                data = json.loads(raw)
                data["strategy_map"] = REGIME_STRATEGY_MAP
                data["thresholds"] = {
                    "vix_low":            VIX_LOW_THRESHOLD,
                    "vix_high":           VIX_HIGH_THRESHOLD,
                    "atr_trend_enter":    ATR_TREND_THRESHOLD,
                    "atr_trend_exit":     ATR_TREND_EXIT_THRESHOLD,
                    "ema_flat":           EMA_FLAT_THRESHOLD,
                }
                return data
        except Exception:
            pass
        return {"regime": "UNKNOWN", "message": "No regime data yet — run detect() first."}

    @staticmethod
    def strategy_map() -> Dict[str, list]:
        return REGIME_STRATEGY_MAP

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _get_market_indicators(self):
        """Return (vix, market_atr_pct, market_ema_spread_pct, data_known).

        Fixed 2026-09-15 (external review, "replace the 40-stock average
        proxy with actual NIFTY"): ATR%/EMA-spread% now come from
        REDIS_NIFTY_REGIME_INPUTS_KEY -- real NIFTY 50 index indicators (see
        refresh_nifty_regime_inputs()) -- instead of REDIS_TREND_STATS_KEY,
        a cross-sectional average across ~130+ F&O stocks. That average was
        never actually NIFTY: on 2026-09-15 itself, Nifty fell ~1.19% while
        IT stocks rose strongly the same session -- a broad average can sit
        near-flat while the index it was standing in for moves sharply.
        REDIS_TREND_STATS_KEY is untouched and still published by LTPPoller
        for its own, unrelated purpose (api/main.py's /health F&O-universe-
        liveness reporting).

        data_known is False whenever EITHER real VIX or real NIFTY inputs
        couldn't be read, so detect() can tell a genuine classification
        apart from one built on fill-in defaults (see detect()'s 2026-09-15
        UNKNOWN-regime fix for why that distinction matters)."""
        vix, vix_known = await self._get_vix()
        atr_pct      = 1.0   # safe default = mid-zone
        ema_spread   = 0.15
        nifty_known  = False

        try:
            raw = await self._redis.get(REDIS_NIFTY_REGIME_INPUTS_KEY)
            if raw:
                stats = json.loads(raw)
                atr_pct     = stats.get("atr_pct_daily", atr_pct)
                ema_spread  = stats.get("ema_spread_pct", ema_spread)
                nifty_known = True
        except Exception as e:
            logger.debug(f"RegimeDetector: NIFTY regime inputs read error: {e}")

        return vix, round(atr_pct, 3), round(ema_spread, 3), (vix_known and nifty_known)

    async def _get_vix(self):
        """Read VIX from Redis (written by ZerodhaLTPPoller or engine).
        Returns (vix, is_real) -- is_real is False when falling back to the
        15.0 middle-of-road default, so callers can tell a genuine VIX
        reading apart from a guess (see detect()'s 2026-09-15 fix)."""
        try:
            raw = await self._redis.get(REDIS_VIX_KEY)
            if raw:
                return float(raw), True
        except Exception:
            pass
        # Fixed 2026-08-28 (code review): removed the "estimate from
        # market-wide avg ATR%" fallback that used to sit here. Its own
        # comment claimed avg_atr_pct_daily is "roughly comparable to VIX
        # points 1:1" -- confirmed live this is flatly wrong: real VIX sat
        # around 11 for most of 2026-08-28 while this fallback was silently
        # returning 1.5-2.0 (the day's actual ATR%) as if it WERE the VIX,
        # for an extended stretch where the real cached value (written by
        # fetch_and_cache_vix(), which correctly fetches NSE:INDIA VIX
        # directly from Zerodha every 5 min) had gone stale. VIX is an
        # annualized volatility measure; a raw daily ATR% is a completely
        # different quantity on a different scale -- there is no valid 1:1
        # relationship to lean on. Silently mislabeling one as the other
        # corrupted the regime classification's most consequential input
        # with no error logged anywhere. Go straight to the existing sane
        # 15.0 middle-of-road default instead, now with a visible warning
        # so a real-VIX outage is observable rather than silently patched
        # over with a wrong number.
        logger.warning(
            "RegimeDetector: real India VIX unavailable (cache empty/stale) "
            "-- using 15.0 middle-of-road default, NOT an ATR%-based guess."
        )
        return 15.0, False   # middle-of-road default, not a real reading

    @staticmethod
    def _classify(vix: float, atr_pct: float, ema_spread_pct: float,
                  prev_regime: Optional[str] = None) -> str:
        """
        Decision tree:

                         VIX > 20?
                        /         \\
                    YES             NO
                VOLATILE      ATR% >= threshold?
                              /                \\
                           YES                  NO
                       TRENDING              VIX < 12?
                                             /          \\
                                          YES            NO
                                        LOW_VOL      RANGE_BOUND
                                    (ATR moderate — whether EMA_spread is flat
                                     or moderately directional, neither is a
                                     strong trend, and premium sellers are the
                                     appropriate regime for either)

        ATR% is checked BEFORE the VIX<12 branch (reordered 2026-07-31 — was
        VIX<12 first). Rationale: VIX<12 means "index-level option premium is
        too cheap to be worth SELLING" — a real, correct concern for
        credit_spread_v1/iron_condor_v1 (which already have their own,
        unchanged, separate VIX>=12 entry gate — this reorder does not touch
        that). But the old ordering let a low VIX force LOW_VOL regardless of
        ATR%, which also excludes ema_crossover_v1/momentum_v1 — strategies
        that BUY naked long options and have no logical dependency on
        index-level premium richness; what they need is a genuinely trending
        stock, which ATR%/EMA-spread already measures directly. Confirmed
        live 2026-07-31: VIX sat at ~11.8-12.0 essentially all session while
        several stocks' ATR% was well above the TRENDING threshold (up to
        ~1.8%) — the old ordering meant ema_crossover_v1/momentum_v1 were
        paused the entire day for a reason that was never theirs. LOW_VOL and
        RANGE_BOUND allow the exact same two strategies
        (REGIME_STRATEGY_MAP), so this reorder changes zero behavior for the
        premium-selling regimes in any scenario — the only thing that changes
        is TRENDING becoming reachable on a low-VIX-but-genuinely-trending
        day. As a side effect, this also closes a gap in the hysteresis
        added the day before: previously a low VIX alone could still kick an
        already-TRENDING day out to LOW_VOL even while ATR% stayed well above
        the (lower) exit threshold, which defeated the point of hysteresis
        for that path.

        The ATR% comparison uses a lower threshold (ATR_TREND_EXIT_THRESHOLD)
        when prev_regime is already "TRENDING" — hysteresis so noise
        oscillating around ATR_TREND_THRESHOLD doesn't flip the regime every
        cycle (see module docstring).

        EMA_FLAT_THRESHOLD/ema_spread_pct are accepted for the API/reporting
        payload (get_regime_report()) but don't branch here — found
        2026-07-30 that an "EMA flat" vs "EMA not flat" split both returned
        RANGE_BOUND, so the comparison was dead code with no observable
        effect on regime selection. Collapsed rather than left as a
        misleading no-op; a genuinely distinct outcome for the moderate-ATR/
        wide-EMA-spread case (if ever wanted) is a separate, deliberate
        design decision, not a bug fix.
        """
        if vix > VIX_HIGH_THRESHOLD:
            return "VOLATILE"
        atr_threshold = ATR_TREND_EXIT_THRESHOLD if prev_regime == "TRENDING" else ATR_TREND_THRESHOLD
        if atr_pct >= atr_threshold:
            return "TRENDING"
        # ATR is moderate (below the trend threshold) — quiet market. VIX<12
        # here still correctly labels it LOW_VOL for reporting, but doesn't
        # change which strategies are eligible (see rationale above).
        if vix < VIX_LOW_THRESHOLD:
            return "LOW_VOL"
        return "RANGE_BOUND"
