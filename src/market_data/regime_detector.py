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

Usage:
    detector = MarketRegimeDetector(redis_client)
    regime   = await detector.detect()       # e.g. "TRENDING"
    mapping  = detector.strategy_map()       # {regime: [strategy_ids]}
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ── Thresholds (tunable) ─────────────────────────────────────────────────────
VIX_LOW_THRESHOLD    = 12.0   # below = LOW_VOL
VIX_HIGH_THRESHOLD   = 20.0   # above = VOLATILE
# Compared against a DAILY-scaled ATR% (see FIVE_MIN_ATR_DAILY_SCALE) — do not compare
# this threshold directly against raw 5-min-bar ATR%, which runs an order of magnitude
# smaller (typically 0.2-0.4%) and would make TRENDING nearly unreachable.
ATR_TREND_THRESHOLD  = 1.5    # daily-equivalent ATR% above = trending (within mid-VIX band)
EMA_FLAT_THRESHOLD   = 0.15   # EMA spread% below = range-bound / flat

REDIS_REGIME_KEY      = "market:regime"
REDIS_TREND_STATS_KEY = "market:trend_stats"  # written by LTPPoller — market-wide avg ATR%/EMA-spread%
REDIS_VIX_KEY         = "market:india_vix"    # matches option_chain.fetch_and_cache_vix()

# Strategy IDs that must exactly match what StrategyRegistry uses
STRATEGY_EMA       = "ema_crossover_v1"
STRATEGY_SPREAD    = "credit_spread_v1"
STRATEGY_CONDOR    = "iron_condor_v1"

# Regime → which strategies should be ACTIVE
#
# Credit spreads are DIRECTIONAL — the engine picks BULL_PUT in an uptrend and
# BEAR_CALL in a downtrend, so the short strike is always placed *away* from the
# price move.  This makes credit spreads safer in TRENDING markets than condors.
# Iron condors are NEUTRAL — they need flat price action, so they are excluded
# from TRENDING and VOLATILE where one wing reliably gets blown out.
REGIME_STRATEGY_MAP: Dict[str, list] = {
    "TRENDING":    [STRATEGY_EMA, STRATEGY_SPREAD],       # spread aligned with trend = low breach risk
    "RANGE_BOUND": [STRATEGY_CONDOR, STRATEGY_SPREAD],   # both premium sellers thrive in flat market
    "VOLATILE":    [STRATEGY_SPREAD],                     # high IV = rich premium; condor wings blow
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
        regime = self._classify(vix, atr_pct, ema_spread_pct)

        payload = {
            "regime":            regime,
            "vix":               vix,
            "market_atr_pct":    atr_pct,
            "market_ema_spread": ema_spread_pct,
            "timestamp":         datetime.utcnow().isoformat(),
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
                    "vix_low":      VIX_LOW_THRESHOLD,
                    "vix_high":     VIX_HIGH_THRESHOLD,
                    "atr_trend":    ATR_TREND_THRESHOLD,
                    "ema_flat":     EMA_FLAT_THRESHOLD,
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
    def _classify(vix: float, atr_pct: float, ema_spread_pct: float) -> str:
        """
        Decision tree:

                         VIX > 20?
                        /         \\
                    YES             NO
                VOLATILE        VIX < 12?
                              /          \\
                           YES            NO
                          LOW_VOL     ATR% >= 1.5%?
                                     /             \\
                                  YES               NO
                               TRENDING         EMA_spread <= 0.15%?
                                               /                    \\
                                            YES                      NO
                                        RANGE_BOUND             RANGE_BOUND (default)
                                    (explicit flat)           (moderate/borderline —
                                                               treat conservatively)
        """
        if vix > VIX_HIGH_THRESHOLD:
            return "VOLATILE"
        if vix < VIX_LOW_THRESHOLD:
            return "LOW_VOL"
        if atr_pct >= ATR_TREND_THRESHOLD:
            return "TRENDING"
        # ATR is moderate — EMAs are flat or borderline flat.
        # Default to RANGE_BOUND (not TRENDING) so iron condors and credit
        # spreads are available in quiet markets where they perform best.
        # Changed < to <= so EMA_spread exactly at threshold rounds to flat.
        if ema_spread_pct <= EMA_FLAT_THRESHOLD:
            return "RANGE_BOUND"
        return "RANGE_BOUND"
