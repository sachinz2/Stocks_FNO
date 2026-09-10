"""
New rule, 2026-09-10 (explicit user request): for intraday (single-leg)
positions under momentum_v1/ema_crossover_v1, once a position's absolute
profit (Rs, not %) first reaches Rs700, track its peak profit every cycle
(~1 min) and keep updating it while profit keeps rising -- but exit once
profit has given back 35% of that peak. Additional to (not a replacement
for) the existing pct-based trailing_stop_pct/breakeven_activation_pct.

Engine side: _check_open_option_exits() tracks peak_profit_rs per contract
(self._peak_profits, persisted like self._peak_premiums). Strategy side:
manage_position() in momentum.py/ema_crossover.py makes the actual exit
decision, checked FIRST (see each file's docstring for why).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.utils import now_ist
from src.live_trading.live_trading_engine import LiveTradingEngine
from src.strategies.ema_crossover import EMACrossoverStrategy
from src.strategies.momentum import MomentumStrategy


# ── Strategy-side: pure manage_position() behavior ──────────────────────────

def _position(avg_price=40.0, quantity=100, peak_profit_rs=None):
    return {
        "avg_price": avg_price, "peak_premium": avg_price, "quantity": quantity,
        "peak_profit_rs": peak_profit_rs, "current_adx": 30.0,
        "is_call": True,
    }


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_no_exit_when_not_yet_activated(strategy_cls, strategy_id):
    """peak_profit_rs is None (never crossed the activation floor) -- the
    new rule must not fire regardless of how much profit has given back.
    Uses a small, boring premium move so every OTHER exit check also stays
    quiet -- isolating that only the profit-booking rule could possibly be
    responsible for an EXIT here."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    result = strat.manage_position(_position(avg_price=40.0, quantity=100, peak_profit_rs=None), 40.5)
    assert result == "HOLD"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_exits_once_profit_gives_back_35_percent_of_peak(strategy_cls, strategy_id):
    """Peak profit was Rs1000 (activated); current profit has fallen to
    Rs650 -- exactly 65% of peak (35% given back) -- must exit."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    quantity = 100
    avg_price = 40.0
    peak_profit_rs = 1000.0
    # current profit = (current_premium - avg_price) * quantity = 650
    current_premium = avg_price + (650.0 / quantity)
    result = strat.manage_position(
        _position(avg_price=avg_price, quantity=quantity, peak_profit_rs=peak_profit_rs),
        current_premium,
    )
    assert result == "EXIT"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_holds_when_profit_still_above_the_giveback_floor(strategy_cls, strategy_id):
    """Peak profit Rs1000, current profit Rs700 (only 30% given back,
    still above the 65%-of-peak floor) -- must NOT exit via this rule."""
    strat = strategy_cls(strategy_id, {})
    strat.initialize()
    quantity = 100
    avg_price = 40.0
    peak_profit_rs = 1000.0
    current_premium = avg_price + (700.0 / quantity)
    result = strat.manage_position(
        _position(avg_price=avg_price, quantity=quantity, peak_profit_rs=peak_profit_rs),
        current_premium,
    )
    assert result == "HOLD"


@pytest.mark.parametrize("strategy_cls,strategy_id", [
    (MomentumStrategy, "momentum_v1"),
    (EMACrossoverStrategy, "ema_crossover_v1"),
])
def test_giveback_pct_is_configurable(strategy_cls, strategy_id):
    strat = strategy_cls(strategy_id, {"profit_booking_giveback_pct": 0.5})
    strat.initialize()
    quantity = 100
    avg_price = 40.0
    peak_profit_rs = 1000.0
    # 50% given back -> current profit = 500 -- exactly at the (now looser) floor.
    current_premium = avg_price + (500.0 / quantity)
    result = strat.manage_position(
        _position(avg_price=avg_price, quantity=quantity, peak_profit_rs=peak_profit_rs),
        current_premium,
    )
    assert result == "EXIT"


# ── Engine side: peak-profit tracking through the real _check_open_option_exits ──

class _FakeProfitBookingEngine:
    _check_open_option_exits      = LiveTradingEngine._check_open_option_exits
    _get_underlying_from_contract = LiveTradingEngine._get_underlying_from_contract

    def __init__(self):
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {
            "TATASTEEL26SEP190CE": {
                "date": now_ist().date().isoformat(),
                "strategy_name": "momentum_v1",
                "entry_regime": None,
            }
        }
        self._peak_premiums = {}
        self._peak_profits = {}
        self._PROFIT_BOOKING_ACTIVATION_RS = 700.0
        self._PROFIT_BOOKING_GIVEBACK_PCT = 0.35
        self._stale_data_since = {}
        self._stale_data_alerted = set()
        self._STALE_DATA_ALERT_SECONDS = 120
        self._get_market_data = AsyncMock(return_value={"atr14": 5.0})
        self._kite = None
        self._redis = None
        self._execute_single_leg_exit = AsyncMock()
        self._notify = AsyncMock()


@pytest.fixture
def momentum_strategy():
    strat = MomentumStrategy("momentum_v1", {})
    strat.initialize()
    return strat


def _position_list(qty=100, avg_price=10.0):
    return [{"symbol": "TATASTEEL26SEP190CE", "quantity": qty, "avg_price": avg_price}]


@pytest.mark.asyncio
async def test_peak_profit_tracked_only_after_crossing_activation_floor(monkeypatch, momentum_strategy):
    import src.market_data.option_chain as option_chain_module

    fake = _FakeProfitBookingEngine()

    # Cycle 1: premium 15.0 -> profit = (15-10)*100 = 500, BELOW Rs700 activation.
    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=15.0))
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    assert "TATASTEEL26SEP190CE" not in fake._peak_profits

    # Cycle 2: premium 18.0 -> profit = 800, crosses the floor -- now tracked.
    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=18.0))
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    assert fake._peak_profits["TATASTEEL26SEP190CE"] == pytest.approx(800.0)


@pytest.mark.asyncio
async def test_peak_profit_keeps_rising_with_price(monkeypatch, momentum_strategy):
    import src.market_data.option_chain as option_chain_module

    fake = _FakeProfitBookingEngine()

    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=18.0))  # profit=800
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    assert fake._peak_profits["TATASTEEL26SEP190CE"] == pytest.approx(800.0)

    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=22.0))  # profit=1200
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    assert fake._peak_profits["TATASTEEL26SEP190CE"] == pytest.approx(1200.0)


@pytest.mark.asyncio
async def test_peak_profit_does_not_fall_when_price_dips_without_triggering_exit(monkeypatch, momentum_strategy):
    """Peak stays at its high-water mark even as price pulls back, as long
    as the pullback hasn't yet breached the 35%-giveback floor."""
    import src.market_data.option_chain as option_chain_module

    fake = _FakeProfitBookingEngine()

    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=22.0))  # profit=1200 (peak)
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    assert fake._peak_profits["TATASTEEL26SEP190CE"] == pytest.approx(1200.0)

    # Pull back to profit=900 -- 25% given back, still above the 35% floor (peak*0.65=780).
    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=19.0))  # profit=900
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    assert fake._peak_profits["TATASTEEL26SEP190CE"] == pytest.approx(1200.0), "peak must not fall on a pullback"
    fake._execute_single_leg_exit.assert_not_called()


@pytest.mark.asyncio
async def test_exit_fires_end_to_end_through_the_real_engine_and_strategy(monkeypatch, momentum_strategy):
    """Full path: peak reaches Rs1200, then price drops enough that profit
    falls to Rs750 (37.5% given back, past the 35% floor: peak*0.65=780) --
    manage_position() must return EXIT and the engine must actually call
    _execute_single_leg_exit."""
    import src.market_data.option_chain as option_chain_module

    fake = _FakeProfitBookingEngine()

    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=22.0))  # profit=1200
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    fake._execute_single_leg_exit.assert_not_called()

    monkeypatch.setattr(option_chain_module, "get_option_quote", AsyncMock(return_value=17.5))  # profit=750
    await fake._check_open_option_exits(_position_list(), {"momentum_v1": momentum_strategy})
    fake._execute_single_leg_exit.assert_awaited_once()
