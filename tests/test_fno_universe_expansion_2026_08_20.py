"""
FNO_SYMBOLS expansion, 41 -> 132 (2026-08-20).

Real, measured liquidity data (scripts/diagnostic_universe_liquidity.py --
20-day average daily turnover via live kite.historical_data(), not a guess)
found the real 208-symbol F&O universe includes 91 more symbols at least as
liquid as TATACONSUM, the least-liquid symbol already traded. Purely
additive: nothing previously traded was removed. Prerequisites landed
earlier the same day: FNO_SECTORS covers all 208 (sector-concentration
check), get_real_strike_interval() derives strike spacing from real
contracts (no per-symbol static-table entry needed), and LTPPoller's OHLC
prefetch is concurrent (cold-start cycle time stays well under budget).
"""
from src.core.constants import FNO_SYMBOLS, FNO_SECTORS, FNO_LOT_SIZES


_ORIGINAL_41 = {
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "BAJFINANCE", "KOTAKBANK", "AXISBANK", "LT",
    "HINDUNILVR", "ITC", "WIPRO", "HCLTECH", "MARUTI",
    "SUNPHARMA", "M&M", "BHARTIARTL", "ADANIPORTS", "ASIANPAINT",
    "TITAN", "BAJAJ-AUTO", "EICHERMOT", "INDUSINDBK", "DRREDDY",
    "CIPLA", "DIVISLAB", "JSWSTEEL", "HINDALCO", "GRASIM",
    "TATACONSUM", "APOLLOHOSP", "NESTLEIND", "TECHM", "BPCL",
    "ONGC", "NTPC", "POWERGRID", "ULTRACEMCO", "TATASTEEL",
    "COALINDIA",
}


def test_fno_symbols_grew_to_132_with_no_duplicates():
    assert len(FNO_SYMBOLS) == 132
    assert len(FNO_SYMBOLS) == len(set(FNO_SYMBOLS)), "duplicate symbol in FNO_SYMBOLS"


def test_original_41_symbols_all_retained():
    # The expansion must be purely additive -- nothing previously traded
    # should have been dropped.
    assert _ORIGINAL_41 <= set(FNO_SYMBOLS)


def test_every_traded_symbol_has_a_sector_mapping():
    # Real regression target: a symbol added to FNO_SYMBOLS without a
    # FNO_SECTORS entry would silently disable the sector-concentration
    # risk check for it (see _process_credit_spread/_process_iron_condor's
    # `if _sym_sector:` guard -- None just skips the check, doesn't fail).
    missing = set(FNO_SYMBOLS) - set(FNO_SECTORS.keys())
    assert not missing, f"{len(missing)} traded symbol(s) have no sector mapping: {sorted(missing)}"


def test_fno_lot_sizes_has_no_dead_entries_for_untraded_symbols():
    # FNO_LOT_SIZES is fallback-only (Redis cache is authoritative, see
    # test_core_utils.py's updated invariant) and doesn't need to cover the
    # new 91 -- but every entry it DOES have must still reference an
    # actually-traded symbol, not a stale leftover.
    assert set(FNO_LOT_SIZES.keys()) <= set(FNO_SYMBOLS)


def test_new_symbols_include_expected_high_liquidity_names_not_in_original_list():
    # Spot-check a few of the real, measured additions found by the
    # diagnostic (BSE ranked more liquid than TCS; several newer/renamed
    # listings that arrived after the original 41 was built).
    for symbol in ("BSE", "KALYANKJIL", "ETERNAL", "PAYTM", "MCX", "LICI", "SWIGGY"):
        assert symbol in FNO_SYMBOLS
        assert symbol not in _ORIGINAL_41
