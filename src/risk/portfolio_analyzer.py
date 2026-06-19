"""
PortfolioAnalyzer — Concern #5

Answers the question: "Are we accidentally taking one giant correlated bet?"

The NSE F&O universe has strong sector clustering:
  Banking:  HDFCBANK, ICICIBANK, SBIN, KOTAKBANK, AXISBANK, INDUSINDBK  (6 symbols)
  IT:       TCS, INFY, WIPRO, HCLTECH, TECHM                             (5 symbols)
  Pharma:   SUNPHARMA, DRREDDY, CIPLA, DIVISLAB                          (4 symbols)
  Metals:   JSWSTEEL, HINDALCO, TATASTEEL                                 (3 symbols)

These groups move together; two credit spreads on SBIN and HDFC are essentially
one trade with doubled exposure. This class surfaces:
  - sector_exposure: {sector: notional ₹ and count}
  - beta_exposure:   portfolio beta vs NIFTY (simple sum of position_betas)
  - correlation_flags: pairs of open positions with known high correlation

Usage:
    analyzer = PortfolioAnalyzer()
    report = await analyzer.get_report(positions)   # positions from broker
    flags  = report["correlation_flags"]            # list of warning strings
"""

import logging
from typing import Dict, List, Any, Optional

from src.core.constants import FNO_SECTORS, FNO_LOT_SIZES, MAX_SECTOR_POSITIONS

logger = logging.getLogger(__name__)

# ── Approximate NIFTY betas (1-year trailing; update quarterly) ──────────────
# Beta > 1.2 → high-beta (amplified NIFTY moves)
# Beta 0.8-1.2 → market-neutral zone
# Beta < 0.8 → defensive
SYMBOL_BETAS: Dict[str, float] = {
    "RELIANCE":    0.85,
    "TCS":         0.75,
    "INFY":        0.80,
    "HDFCBANK":    1.05,
    "ICICIBANK":   1.15,
    "SBIN":        1.30,
    "BAJFINANCE":  1.35,
    "KOTAKBANK":   0.95,
    "AXISBANK":    1.20,
    "LT":          1.00,
    "HINDUNILVR":  0.60,
    "ITC":         0.70,
    "WIPRO":       0.80,
    "HCLTECH":     0.78,
    "MARUTI":      0.90,
    "SUNPHARMA":   0.65,
    "TATAMOTORS":  1.40,
    "BHARTIARTL":  0.75,
    "ADANIPORTS":  1.25,
    "ASIANPAINT":  0.70,
    "TITAN":       0.95,
    "BAJAJ-AUTO":  0.85,
    "EICHERMOT":   1.00,
    "INDUSINDBK":  1.20,
    "DRREDDY":     0.60,
    "CIPLA":       0.62,
    "DIVISLAB":    0.65,
    "JSWSTEEL":    1.30,
    "HINDALCO":    1.25,
    "GRASIM":      0.90,
    "TATACONSUM":  0.75,
    "APOLLOHOSP":  0.70,
    "NESTLEIND":   0.55,
    "TECHM":       0.82,
    "BPCL":        0.85,
    "ONGC":        0.88,
    "NTPC":        0.72,
    "POWERGRID":   0.65,
    "ULTRACEMCO":  0.90,
    "TATASTEEL":   1.35,
}

# Notional threshold (₹) above which a single sector is considered over-concentrated
SECTOR_NOTIONAL_LIMIT = 60_000.0   # ₹60k per sector = 20% of ₹3L capital

# Portfolio-level beta threshold
PORTFOLIO_BETA_LIMIT = 5.0   # sum of |beta × notional| / total_notional

# Known highly-correlated pairs (Pearson r historically > 0.85)
HIGH_CORRELATION_PAIRS = [
    ("SBIN",      "HDFCBANK"),
    ("SBIN",      "ICICIBANK"),
    ("SBIN",      "AXISBANK"),
    ("SBIN",      "KOTAKBANK"),
    ("HDFCBANK",  "ICICIBANK"),
    ("HDFCBANK",  "AXISBANK"),
    ("ICICIBANK", "AXISBANK"),
    ("ICICIBANK", "KOTAKBANK"),
    ("AXISBANK",  "KOTAKBANK"),
    ("TCS",       "INFY"),
    ("TCS",       "WIPRO"),
    ("TCS",       "HCLTECH"),
    ("INFY",      "WIPRO"),
    ("INFY",      "HCLTECH"),
    ("WIPRO",     "TECHM"),
    ("DRREDDY",   "CIPLA"),
    ("DRREDDY",   "DIVISLAB"),
    ("CIPLA",     "DIVISLAB"),
    ("JSWSTEEL",  "TATASTEEL"),
    ("JSWSTEEL",  "HINDALCO"),
    ("HINDALCO",  "TATASTEEL"),
]


class PortfolioAnalyzer:
    """
    Stateless portfolio exposure analysis.
    All methods are pure functions of the positions list — no DB access needed.
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def get_report(self, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build a full exposure report for the current open positions.

        positions : list from broker.get_positions() — each entry must have:
                    'symbol' (str), 'quantity' (int), 'avg_price' (float)

        Returns:
            sector_exposure      : { sector: {count, notional, symbols} }
            beta_exposure        : { symbol: beta, 'portfolio_beta': float }
            correlation_flags    : [ "WARNING: SBIN + HDFCBANK both open …", … ]
            concentration_alerts : [ "Banking sector: 4 positions / ₹1,20,000 notional …", … ]
            total_notional       : float (₹)
        """
        active = [p for p in positions if p.get("quantity", 0) != 0]

        sector_exp    = self._sector_exposure(active)
        beta_exp      = self._beta_exposure(active)
        corr_flags    = self._correlation_flags(active)
        conc_alerts   = self._concentration_alerts(sector_exp)
        total_notional = sum(
            abs(p.get("quantity", 0)) * p.get("avg_price", 0.0)
            for p in active
        )

        return {
            "sector_exposure":       sector_exp,
            "beta_exposure":         beta_exp,
            "correlation_flags":     corr_flags,
            "concentration_alerts":  conc_alerts,
            "total_notional":        round(total_notional, 2),
            "open_position_count":   len(active),
        }

    def sector_count(self, positions: List[Dict[str, Any]], sector: str) -> int:
        """Return how many open positions belong to `sector`."""
        return sum(
            1 for p in positions
            if FNO_SECTORS.get(self._root_symbol(p.get("symbol", ""))) == sector
            and p.get("quantity", 0) != 0
        )

    def is_sector_at_limit(self, positions: List[Dict[str, Any]], symbol: str) -> bool:
        """
        Return True if adding `symbol` would breach MAX_SECTOR_POSITIONS.
        Mirrors RiskManager's Layer 4 check but without stateful caching,
        so it can be called with live positions at any time.
        """
        sector = FNO_SECTORS.get(symbol)
        if not sector:
            return False
        count = self.sector_count(positions, sector)
        return count >= MAX_SECTOR_POSITIONS

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _sector_exposure(self, positions: List[Dict[str, Any]]) -> Dict[str, dict]:
        exposure: Dict[str, dict] = {}
        for p in positions:
            sym    = self._root_symbol(p.get("symbol", ""))
            sector = FNO_SECTORS.get(sym, "Unknown")
            notional = abs(p.get("quantity", 0)) * p.get("avg_price", 0.0)
            if sector not in exposure:
                exposure[sector] = {"count": 0, "notional": 0.0, "symbols": []}
            exposure[sector]["count"]    += 1
            exposure[sector]["notional"] += notional
            exposure[sector]["symbols"].append(sym)
        # round notionals
        for s in exposure:
            exposure[s]["notional"] = round(exposure[s]["notional"], 2)
        return exposure

    def _beta_exposure(self, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        total_notional   = 0.0
        weighted_beta    = 0.0
        for p in positions:
            sym      = self._root_symbol(p.get("symbol", ""))
            beta     = SYMBOL_BETAS.get(sym, 1.0)   # default 1.0 if unknown
            notional = abs(p.get("quantity", 0)) * p.get("avg_price", 0.0)
            result[sym]     = beta
            total_notional += notional
            weighted_beta  += beta * notional

        portfolio_beta = (weighted_beta / total_notional) if total_notional > 0 else 0.0
        result["portfolio_beta"] = round(portfolio_beta, 3)
        result["high_beta_alert"] = portfolio_beta > PORTFOLIO_BETA_LIMIT
        return result

    def _correlation_flags(self, positions: List[Dict[str, Any]]) -> List[str]:
        open_syms = {
            self._root_symbol(p["symbol"])
            for p in positions
            if p.get("quantity", 0) != 0
        }
        flags: List[str] = []
        for a, b in HIGH_CORRELATION_PAIRS:
            if a in open_syms and b in open_syms:
                sector = FNO_SECTORS.get(a, "")
                flags.append(
                    f"HIGH CORRELATION: {a} + {b} both open "
                    f"({sector} sector — these move together)"
                )
        return flags

    def _concentration_alerts(self, sector_exp: Dict[str, dict]) -> List[str]:
        alerts: List[str] = []
        for sector, data in sector_exp.items():
            if data["count"] > MAX_SECTOR_POSITIONS:
                alerts.append(
                    f"SECTOR CONCENTRATION: {sector} has {data['count']} open positions "
                    f"(limit {MAX_SECTOR_POSITIONS}), notional ₹{data['notional']:,.0f} — "
                    f"symbols: {', '.join(data['symbols'])}"
                )
            if data["notional"] > SECTOR_NOTIONAL_LIMIT:
                alerts.append(
                    f"NOTIONAL LIMIT: {sector} exposure ₹{data['notional']:,.0f} "
                    f"> ₹{SECTOR_NOTIONAL_LIMIT:,.0f} threshold"
                )
        return alerts

    @staticmethod
    def _root_symbol(symbol: str) -> str:
        """
        Strip expiry/strike suffix from an options symbol to get the underlying.
        e.g. 'SBIN24JANFUT' → 'SBIN',  'HDFCBANK' → 'HDFCBANK'
        Handles: plain equity, futures (YYMONFUT), options (YYMONSTRIKEPE/CE).
        """
        for known in sorted(FNO_SECTORS.keys(), key=len, reverse=True):
            if symbol.upper().startswith(known):
                return known
        return symbol.upper()
