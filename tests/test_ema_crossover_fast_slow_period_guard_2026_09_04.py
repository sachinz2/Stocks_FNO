"""
Live incident, 2026-09-04 (thorough cross-check of all strategies):
fast_period/slow_period were configurable in NAME only on EMACrossoverStrategy
-- the indicator pipeline (ltp_poller.py's _enrich()) hardcodes ema20/ema50,
and two other consumers of the same EMAs (the EMA-reversal exit check, the
engine's 15-min MTF filter) independently hardcode "ema20"/"ema50" too,
never reading fast_period/slow_period. Configuring any other pair silently
went dormant -- generate_signal() always saw fast_ema=None and returned HOLD
forever, no error, no log. Now fails loudly at initialize() instead.
"""
import pytest

from src.strategies.ema_crossover import EMACrossoverStrategy


def test_default_20_50_period_pair_initializes_fine():
    strategy = EMACrossoverStrategy("ema_test", {})
    strategy.initialize()
    assert strategy.fast_period == 20
    assert strategy.slow_period == 50


def test_non_default_period_pair_raises_instead_of_going_silently_dormant():
    strategy = EMACrossoverStrategy("ema_test", {"fast_period": 13, "slow_period": 21})
    with pytest.raises(ValueError, match="not supported"):
        strategy.initialize()


def test_live_config_in_main_py_uses_the_only_supported_period_pair():
    """Guard against the fix itself drifting out of sync with src/api/main.py's
    real strategy config -- if the live default ever changes to a pair this
    guard rejects, production would fail to start, loudly, which is the point."""
    import inspect
    import src.api.main as main_module
    src = inspect.getsource(main_module)
    idx = src.index('"ema_crossover_v1"')
    block = src[idx:idx + 200]
    assert '"fast_period": 20, "slow_period": 50,' in block
