"""
Two low-confidence findings from the 2026-09-18 second deep-review pass,
hardened:

1. bs_delta() was missing the S<=0/K<=0 guard bs_price() already has --
   not reachable from any live call site today, but a latent landmine for
   a future one.
2. get_iv_rank()'s "current" IV used to be the last entry of the POSITIVE-
   only-filtered history, not the actual most recent day's reading -- a
   genuinely zero/invalid latest entry silently fell back to whatever the
   last valid day was, with nothing to flag that this had happened.
"""
import json

import pytest

from src.market_data.option_chain import bs_delta, get_iv_rank


# ── bs_delta() S<=0/K<=0 guard ───────────────────────────────────────────────

def test_bs_delta_does_not_raise_on_non_positive_underlying():
    # Previously: math.log(S/K) with S<=0 raises ValueError (domain error).
    # S <= 0 < K is always "ITM" for a put by the same S<K convention the
    # T<=0/sigma<=0 branch already uses -- the point of this test is that
    # it returns a value at all, not a crash.
    assert bs_delta(S=0.0, K=100.0, T=0.02, sigma=0.3, option_type="CE") == 0.0
    assert bs_delta(S=-5.0, K=100.0, T=0.02, sigma=0.3, option_type="PE") == -1.0


def test_bs_delta_does_not_raise_on_zero_strike():
    # Previously: math.log(S/K) with K==0 raises ZeroDivisionError.
    assert bs_delta(S=100.0, K=0.0, T=0.02, sigma=0.3, option_type="CE") == 1.0


def test_bs_delta_non_positive_inputs_use_the_same_intrinsic_convention_as_bs_price():
    # Deep ITM call (S > K) -> full delta, matching the T<=0/sigma<=0
    # branch's own existing convention just below.
    assert bs_delta(S=150.0, K=100.0, T=0.02, sigma=0.0, option_type="CE") == 1.0
    assert bs_delta(S=150.0, K=0.0, T=0.02, sigma=0.3, option_type="PE") == 0.0  # OTM put (S > K)


def test_bs_delta_still_computes_normally_for_valid_inputs():
    # Guard against over-fixing -- a completely ordinary ATM call must still
    # get a real, non-edge-case delta near 0.5.
    d = bs_delta(S=100.0, K=100.0, T=30 / 365, sigma=0.25, option_type="CE")
    assert 0.4 < d < 0.6


# ── get_iv_rank() -- "current" must be the actual latest reading ────────────

class _FakeRedis:
    def __init__(self, history):
        self._raw = json.dumps(history)

    async def get(self, key):
        return self._raw


def _history(ivs):
    """ivs: list of IV values, chronological, last = most recent day."""
    return [{"d": f"2026-08-{i+1:02d}", "iv": v} for i, v in enumerate(ivs)]


@pytest.mark.asyncio
async def test_iv_rank_fails_closed_when_the_latest_reading_is_zero():
    # 20 days of real history, but TODAY's entry (the last one) is a genuine
    # 0.0 (e.g. atr_to_annualised_vol() got atr==0 on a thin session).
    # Fixed 2026-09-18: this used to silently fall back to the last VALID
    # day as "current" instead of failing closed.
    ivs = [0.20 + 0.01 * i for i in range(19)] + [0.0]
    redis = _FakeRedis(_history(ivs))

    result = await get_iv_rank("TESTSYM", redis)

    assert result is None


@pytest.mark.asyncio
async def test_iv_rank_uses_the_actual_latest_reading_as_current_not_an_older_one():
    # All 20 days valid -- "current" must be day 20's IV (the max, here),
    # not some earlier value.
    ivs = [0.20 + 0.01 * i for i in range(20)]  # 0.20 .. 0.39, strictly rising
    redis = _FakeRedis(_history(ivs))

    result = await get_iv_rank("TESTSYM", redis)

    # current=0.39 (last day), lo=0.20, hi=0.39 -> rank = 1.0 (current IS the high)
    assert result == 1.0


@pytest.mark.asyncio
async def test_iv_rank_still_returns_none_for_short_history():
    ivs = [0.25] * 5
    redis = _FakeRedis(_history(ivs))

    result = await get_iv_rank("TESTSYM", redis)

    assert result is None


@pytest.mark.asyncio
async def test_iv_rank_flat_history_returns_midpoint():
    ivs = [0.30] * 20
    redis = _FakeRedis(_history(ivs))

    result = await get_iv_rank("TESTSYM", redis)

    assert result == 0.5
