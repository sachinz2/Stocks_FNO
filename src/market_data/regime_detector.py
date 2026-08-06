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
from typing import Dict, Optional

from src.core.utils import now_ist

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
REDIS_TREND_STATS_KEY = "market:trend_stats"  # written by LTPPoller — market-wide avg ATR%/EMA-spread%
REDIS_VIX_KEY         = "market:india_vix"    # matches option_chain.fetch_and_cache_vix()

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
# EMA crossover / Momentum now also run in VOLATILE (added 2026-07-31 — were
# excluded entirely before). India VIX is a fear/uncertainty gauge, not a
# directional one — VIX>20 empirically correlates with sharp SELLOFFS, not
# bull sprints (confirmed: this system's own ~4-week history never saw VIX
# above 15.03, so a genuine spike has never been observed live here — this is
# a deliberate risk decision, not something validated against this system's
# own data yet). live_trading_engine.py._process_signal() restricts entries
# to PE (bearish) only while VOLATILE is active — no CE/long-call entries —
# and both strategies get a tightened, VOLATILE-specific reversal exit (see
# EMACrossoverStrategy.manage_position()'s EMA-reversal check and
# MomentumStrategy's adx_exit_threshold_volatile) so a position opened to
# catch a crash gets closed fast if the move V-reverses, rather than riding
# out the same slower thresholds used in a normal trending market.
REGIME_STRATEGY_MAP: Dict[str, list] = {
    "TRENDING":    [STRATEGY_EMA, STRATEGY_SPREAD, STRATEGY_MOMENTUM],  # spread aligned with trend = low breach risk
    "RANGE_BOUND": [STRATEGY_CONDOR, STRATEGY_SPREAD],   # both premium sellers thrive in flat market
    "VOLATILE":    [STRATEGY_SPREAD, STRATEGY_EMA, STRATEGY_MOMENTUM],  # high IV = rich premium for spreads;
                                                                          # EMA/Momentum PE-only, crash-catching
    "LOW_VOL":     [STRATEGY_SPREAD, STRATEGY_CONDOR],   # quiet market = premium seller heaven
}


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
        Falls back to RANGE_BOUND (most conservative) if data is missing.
        """
        vix, atr_pct, ema_spread_pct = await self._get_market_indicators()
        prev_regime = await self.get_cached_regime()
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

        regime   = await self.get_cached_regime()
        active   = StrategyRegistry.get_active_strategies()
        should_run = set(REGIME_STRATEGY_MAP.get(regime, []))

        for sid, instance in active.items():
            should_be_active = sid in should_run
            if instance.is_active and not should_be_active:
                StrategyRegistry.pause_strategy(sid, reason=f"Regime is {regime} — strategy not active in this regime")
                logger.warning(
                    f"RegimeSwitching: PAUSED {sid} — "
                    f"regime={regime} not in its allowed set"
                )
            elif not instance.is_active and should_be_active:
                StrategyRegistry.resume_strategy(sid)
                logger.info(
                    f"RegimeSwitching: RESUMED {sid} — "
                    f"regime={regime} is in its allowed set"
                )

    async def get_cached_regime(self) -> str:
        """Read regime from Redis. Returns RANGE_BOUND if no data yet."""
        try:
            raw = await self._redis.get(REDIS_REGIME_KEY)
            if raw:
                return json.loads(raw).get("regime", "RANGE_BOUND")
        except Exception:
            pass
        return "RANGE_BOUND"

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
        """Return (vix, market_atr_pct, market_ema_spread_pct). Falls back to safe
        mid-zone defaults if LTPPoller hasn't published market:trend_stats yet
        (e.g. market just opened, or market is closed)."""
        vix          = await self._get_vix()
        atr_pct      = 1.0   # safe default = mid-zone
        ema_spread   = 0.15

        try:
            raw = await self._redis.get(REDIS_TREND_STATS_KEY)
            if raw:
                stats = json.loads(raw)
                if stats.get("n_symbols", 0) > 0:
                    atr_pct    = stats.get("avg_atr_pct_daily", atr_pct)
                    ema_spread = stats.get("avg_ema_spread_pct", ema_spread)
        except Exception as e:
            logger.debug(f"RegimeDetector: trend stats read error: {e}")

        return vix, round(atr_pct, 3), round(ema_spread, 3)

    async def _get_vix(self) -> float:
        """Read VIX from Redis (written by ZerodhaLTPPoller or engine)."""
        try:
            raw = await self._redis.get(REDIS_VIX_KEY)
            if raw:
                return float(raw)
        except Exception:
            pass
        # Fallback: estimate from market-wide avg ATR% (very rough — this path is
        # rarely hit since fetch_and_cache_vix() is the primary, working source).
        # avg_atr_pct_daily is already scaled to daily-equivalent terms, so it's
        # roughly comparable to VIX points 1:1 (both express expected daily move %).
        try:
            raw = await self._redis.get(REDIS_TREND_STATS_KEY)
            if raw:
                stats = json.loads(raw)
                atr_pct_daily = stats.get("avg_atr_pct_daily", 0)
                if atr_pct_daily > 0:
                    return atr_pct_daily
        except Exception:
            pass
        return 15.0   # middle-of-road default

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
