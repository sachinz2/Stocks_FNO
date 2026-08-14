import pytest


@pytest.fixture(autouse=True)
def _reset_positions_cache():
    """
    positions_router._positions_from_engine() caches its result for a few
    seconds, keyed by id(engine) (2026-08-13 fix for redundant repeated
    fetches across /positions, /pnl-summary, /capital-periods on the same
    dashboard render -- see its own docstring). id() is only unique among
    objects alive at the same time; two different tests' short-lived fake
    engines running within the same TTL window could plausibly reuse the
    same address after the first is garbage collected, letting one test's
    cached result leak into another. Clearing the cache before every test
    removes that possibility instead of relying on timing.
    """
    from src.api.routers import positions_router
    positions_router._positions_cache.clear()
    yield
    positions_router._positions_cache.clear()
