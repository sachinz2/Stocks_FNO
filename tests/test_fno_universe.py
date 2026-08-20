"""src/market_data/fno_universe.py -- pure-function unit tests."""
from src.market_data.fno_universe import (
    INDEX_NAMES, MIN_ADTV_CR,
    extract_stock_underlyings, resolve_nse_tokens,
    compute_liquidity_turnover, qualifying_symbols,
)


def _inst(name, itype, tradingsymbol=None):
    return {"name": name, "instrument_type": itype, "tradingsymbol": tradingsymbol or name}


def test_extract_stock_underlyings_excludes_indices_and_dedupes():
    dump = [
        _inst("RELIANCE", "CE"), _inst("RELIANCE", "PE"),
        _inst("TCS", "CE"),
        _inst("NIFTY", "CE"), _inst("BANKNIFTY", "PE"),
        _inst("RELIANCE", "FUT"),  # not CE/PE -- ignored
    ]
    result = extract_stock_underlyings(dump)
    assert result == ["RELIANCE", "TCS"]


def test_extract_stock_underlyings_handles_empty_dump():
    assert extract_stock_underlyings([]) == []


def test_index_names_covers_known_indices():
    for idx in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYNXT50"):
        assert idx in INDEX_NAMES


def test_resolve_nse_tokens_filters_to_requested_symbols():
    nse_dump = [
        {"tradingsymbol": "RELIANCE", "instrument_token": 111},
        {"tradingsymbol": "TCS", "instrument_token": 222},
        {"tradingsymbol": "SOMEOTHERSTOCK", "instrument_token": 333},
    ]
    tokens = resolve_nse_tokens(nse_dump, {"RELIANCE", "TCS"})
    assert tokens == {"RELIANCE": 111, "TCS": 222}


def test_compute_liquidity_turnover_averages_volume_times_close():
    class _FakeKite:
        def historical_data(self, token, from_date, to_date, interval, continuous, oi):
            return [
                {"close": 100.0, "volume": 1_000_000},
                {"close": 110.0, "volume": 2_000_000},
            ]

    turnover = compute_liquidity_turnover(_FakeKite(), {"RELIANCE": 111}, lookback_days=30)
    expected = ((100.0 * 1_000_000) + (110.0 * 2_000_000)) / 2
    assert turnover["RELIANCE"] == expected


def test_compute_liquidity_turnover_skips_symbol_on_fetch_failure():
    class _FlakyKite:
        def historical_data(self, token, from_date, to_date, interval, continuous, oi):
            if token == 1:
                raise RuntimeError("Zerodha timeout")
            return [{"close": 50.0, "volume": 100}]

    turnover = compute_liquidity_turnover(_FlakyKite(), {"BAD": 1, "GOOD": 2})
    assert "BAD" not in turnover
    assert "GOOD" in turnover


def test_compute_liquidity_turnover_ignores_zero_volume_days():
    class _FakeKite:
        def historical_data(self, token, from_date, to_date, interval, continuous, oi):
            return [
                {"close": 100.0, "volume": 0},  # e.g. a trading holiday row -- must not count as a Rs0 day
                {"close": 100.0, "volume": 1000},
            ]

    turnover = compute_liquidity_turnover(_FakeKite(), {"X": 1})
    assert turnover["X"] == 100.0 * 1000  # only the real trading day counted


def test_qualifying_symbols_uses_the_fixed_threshold_by_default():
    turnover = {
        "LIQUID": MIN_ADTV_CR * 1e7 + 1,
        "EXACTLY_AT_FLOOR": MIN_ADTV_CR * 1e7,
        "THIN": MIN_ADTV_CR * 1e7 - 1,
    }
    result = qualifying_symbols(turnover)
    assert result == ["EXACTLY_AT_FLOOR", "LIQUID"]


def test_qualifying_symbols_respects_custom_threshold():
    turnover = {"A": 50e7, "B": 200e7}
    assert qualifying_symbols(turnover, min_adtv_cr=100.0) == ["B"]
    assert qualifying_symbols(turnover, min_adtv_cr=10.0) == ["A", "B"]
