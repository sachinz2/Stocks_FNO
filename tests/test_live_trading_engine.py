"""
LiveTradingEngine behavioral tests. Most of these call the real unbound
methods against a lightweight duck-typed stand-in (constructing the real
engine needs a live DB/broker/redis stack) -- this exercises the actual
production code, not a reimplementation of it.
"""
import inspect
import types
import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.risk.risk_manager import RiskManager
from src.core.enums import SignalType


# ── Bootstrap-fallback skip (2026-08-06) ────────────────────────────────────
#
# _process_signal() must skip generate_signal() entirely while a symbol is
# still on the historical-data bootstrap fallback (ltp_source ==
# "zerodha_historical") -- otherwise stateful strategies (EMA crossover /
# momentum track prev_fast_ema/prev_slow_ema and pending-confirmation bar
# counts across cycles) silently seed their state from frozen, non-moving
# data, corrupting real signal detection once live ticks start flowing.

def test_bootstrap_fallback_skip_placed_before_generate_signal():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    skip_idx = src.index('market_data.get("ltp_source") == "zerodha_historical"')
    gen_idx = src.index("strategy.generate_signal(market_data)")
    assert skip_idx < gen_idx


@pytest.mark.parametrize("market_data,expected_skip", [
    ({"ltp_source": "zerodha_historical"}, True),
    ({"ltp_source": "zerodha_live_ticks"}, False),
    ({}, False),  # missing field fails open -- matches the conservative default elsewhere
])
def test_bootstrap_fallback_guard_logic(market_data, expected_skip):
    would_skip = market_data.get("ltp_source") == "zerodha_historical"
    assert would_skip is expected_skip


# ── Entry pricing (2026-08-06, tightened 2026-08-21) ─────────────────────────
#
# Single-leg entries (ema_crossover_v1/momentum_v1) must try a real Zerodha
# quote. Originally (2026-08-06) fell back to the ATR-heuristic estimate if
# unavailable, matching the exit paths' pattern -- but exits place MARKET
# orders (the estimate only ever informs a HOLD/EXIT decision, the real fill
# always comes from the market), while entries place LIMIT orders at exactly
# this price. Fixed 2026-08-21 (external review): trading a real LIMIT order
# on a crude ATR-derived guess can genuinely misprice the fill (or never
# fill at all) -- entries now skip (fail closed) instead of estimating,
# matching this function's convention for other explicitly-chosen
# entry-blocking data (lot size, contract resolution).

def test_entry_path_tries_real_quote_before_estimate_fallback():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert "get_option_quote(contract" in src
    entry_block = src[src.index("get_option_quote(contract"):src.index("option_p = _real_p") + 40]
    assert "estimate_option_premium" not in entry_block, (
        "entries must fail closed on a missing real quote, not fall back to an estimate"
    )
    assert 'if not (_real_p and _real_p > 0):' in entry_block
    assert "return" in entry_block

    # Fixed 2026-08-13: contract construction now goes through _resolve_contract()
    # (validates/corrects the computed strike + symbol against Zerodha's real,
    # daily-cached instrument list -- see _resolve_contract's docstring) instead
    # of calling build_option_symbol() directly.
    contract_idx = src.index("self._resolve_contract(symbol, expiry, strike, option_type)")
    quote_idx = src.index("get_option_quote(contract")
    price_idx = src.index("option_p = _real_p")
    assert contract_idx < quote_idx < price_idx, (
        "must resolve the real contract, then fetch quote, then assign option_p, in that order"
    )


def test_exit_path_quote_pattern_unchanged():
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    assert "_live_p = await get_option_quote(contract" in src
    assert "current_p = _live_p" in src


# ── Dead Position-table code removed (2026-08-13) ────────────────────────────
#
# update_position_market_price() only ever UPDATED a row matching an already-
# existing Position DB record -- nothing in the live trading path has ever
# CREATED one (single-leg entries go through trade_journal, spreads/condors
# through _active_spreads/_active_condors, neither touches the Position
# model). Confirmed live: the table's most recent row was last updated
# 2026-07-24, every row quantity=0. Every call site (a dedicated per-cycle
# _refresh_all_position_market_prices() plus one inline call in
# _check_open_option_exits) was therefore doing real work -- a live
# kite.ltp() API call every single minute in live mode -- to feed a method
# that silently did nothing. Removed entirely rather than fixed, since the
# one real consumer of position market prices (dashboard unrealized PnL) was
# separately fixed to read live engine state instead (see
# analytics_router.get_pnl_summary).

def test_refresh_all_position_market_prices_removed():
    assert not hasattr(LiveTradingEngine, "_refresh_all_position_market_prices")


def test_run_signal_cycle_no_longer_calls_removed_refresh():
    src = inspect.getsource(LiveTradingEngine.run_signal_cycle)
    assert "_refresh_all_position_market_prices" not in src


def test_check_open_option_exits_no_longer_calls_dead_portfolio_method():
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    assert "update_position_market_price" not in src


def test_portfolio_manager_no_longer_has_dead_method():
    from src.portfolio.portfolio_manager import PortfolioManager
    assert not hasattr(PortfolioManager, "update_position_market_price")


# ── per-position error isolation in _check_open_option_exits (2026-08-13) ───
#
# Matches the _square_off_all fix (see test_square_off_one_bad_position_does_
# not_block_the_rest below) -- an exception evaluating ONE position's exit
# rules must not prevent every OTHER open position's exit rules (or, since
# this runs before the entry-signal loop in run_signal_cycle, every entry
# signal that cycle) from being evaluated. No full behavioral harness exists
# for this method yet (it needs active_strategies + portfolio_manager +
# option-quote mocking), so this checks the structural property directly.

def test_check_open_option_exits_wraps_per_position_body_in_try_except():
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    loop_idx   = src.index("for pos in positions:")
    try_idx    = src.index("try:", loop_idx)
    except_idx = src.index("except Exception", try_idx)
    exit_call_idx = src.index("await self._execute_single_leg_exit(", try_idx)
    # The try must start right after entering the loop, and the actual exit
    # execution (the call most likely to raise, per the 2026-08-12 live
    # crash) must be inside the try/except span, not after it.
    assert loop_idx < try_idx < exit_call_idx < except_idx


# ── Kill-switch bypass for spread/condor exits (2026-08-06) ─────────────────
#
# risk_manager.validate_trade()'s is_exit_order=True bypasses the kill
# switch/circuit breaker entirely (needed so an existing position can always
# be closed); is_spread_leg=True alone does NOT (intentional for entry hedge
# legs) -- credit-spread and iron-condor exit legs were only passing
# is_spread_leg=True, meaning a tripped kill switch could block closing an
# existing spread/condor position.

def test_spread_and_condor_exit_legs_all_carry_is_exit_order():
    # Fixed 2026-08-20 (deep review): each function's per-leg place_order()
    # calls were consolidated into a shared _close_leg() inner helper (so a
    # leg that already closed on a prior partial-exit attempt isn't
    # resubmitted on retry -- see _close_leg()'s own docstring/comment).
    # is_spread_leg=True/is_exit_order=True now appear once each, on the
    # single place_order() call inside _close_leg(), but _close_leg() itself
    # is invoked once per leg (2 for a spread, 4 for a condor).
    spread_src = inspect.getsource(LiveTradingEngine._check_spread_exits)
    condor_src = inspect.getsource(LiveTradingEngine._check_condor_exits)

    for name, src, n_legs in [("_check_spread_exits", spread_src, 2), ("_check_condor_exits", condor_src, 4)]:
        assert "is_spread_leg=True" in src, name
        assert "is_exit_order=True" in src, name
        assert src.count("await _close_leg(") >= n_legs, name


def test_kill_switch_blocks_spread_leg_alone_but_not_with_exit_order():
    rm = RiskManager(initial_capital=300_000.0)
    rm.activate_kill_switch("test: simulated daily loss breach")

    blocked = rm.validate_trade("TESTCE", "BUY", 25, 10.0, is_spread_leg=True, is_exit_order=False)
    assert blocked is False, "is_spread_leg alone must still be blocked by an active kill switch"

    allowed = rm.validate_trade("TESTCE", "BUY", 25, 10.0, is_spread_leg=True, is_exit_order=True)
    assert allowed is True, "is_exit_order=True must bypass the kill switch"


# ── Fixture shared by the fake-engine behavioral tests below ────────────────

class _FakeOrder:
    def __init__(self, order_status="OPEN", fill_price=None):
        self.order_status = order_status
        self.fill_price = fill_price


class _FakeOrderManager:
    def __init__(self, fill_price=None):
        self.fill_price = fill_price
        self.calls = []

    async def place_order(self, contract, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None):
        self.calls.append((contract, side, qty, price, is_exit_order, strategy_name, product_override))
        return _FakeOrder(order_status="OPEN", fill_price=self.fill_price)


class _FakeRiskManager:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


class _FakeEngine:
    """Duck-typed stand-in -- only the attributes/methods the target methods
    actually touch."""
    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self, fill_price=None):
        self.order_manager = _FakeOrderManager(fill_price)
        self.risk_manager = _FakeRiskManager()
        self._peak_premiums = {"TITAN26AUG4975CE": 50.0}
        self._single_leg_journals = {
            "TITAN26AUG4975CE": {
                "journal_id": 154, "underlying": "TITAN",
                "strategy_name": "momentum_v1", "entry_regime": "TRENDING",
            }
        }
        self._notifications = []
        self._closed_journals = []
        self._kite = None
        self._redis = None

    async def _get_market_data(self, symbol):
        return {"atr14": 8.8737}

    async def _safe_get_positions(self):
        return [{"symbol": "TITAN26AUG4975CE", "quantity": 175, "avg_price": 46.59}]

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, market_data, total_slippage_pts, **_kw):
        self._closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state(self):
        pass

    async def _notify(self, msg):
        self._notifications.append(msg)


# ── Manual close position (2026-08-06) ───────────────────────────────────────
#
# The admin capability used to correct positions entered on an invalid
# contract symbol (e.g. the TITAN26AUG4975CE phantom-strike incident).

@pytest.mark.asyncio
async def test_close_single_leg_position_places_correct_exit_order():
    fake = _FakeEngine()  # fill_price=None -> _fill_p falls back to current_p (ATR estimate)
    fake._execute_single_leg_exit = types.MethodType(LiveTradingEngine._execute_single_leg_exit, fake)

    closed = await LiveTradingEngine.close_single_leg_position(
        fake, "TITAN26AUG4975CE",
        "Invalid contract symbol -- strike doesn't exist on exchange.",
    )

    assert closed is True
    assert "TITAN26AUG4975CE" not in fake._single_leg_journals, "journal entry should be popped on close"
    assert len(fake.order_manager.calls) == 1
    contract, side, qty, price, is_exit, strategy_name, product_override = fake.order_manager.calls[0]
    assert contract == "TITAN26AUG4975CE"
    assert side == "SELL"
    assert qty == 175
    assert is_exit is True
    # price should be the ATR estimate (no live quote available), NOT 0 and NOT entry price
    assert price > 0 and price != 46.59
    # closing order's product must match the original position's -- peeked from
    # the journal's strategy_name (momentum_v1, set in _FakeEngine's fixture)
    assert strategy_name == "momentum_v1"


@pytest.mark.asyncio
async def test_close_single_leg_position_writes_journal_and_releases_capital():
    fake = _FakeEngine()
    fake._execute_single_leg_exit = types.MethodType(LiveTradingEngine._execute_single_leg_exit, fake)

    await LiveTradingEngine.close_single_leg_position(fake, "TITAN26AUG4975CE", "reason")

    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]
    assert journal_id == 154
    expected_pnl = (exit_price - 46.59) * 175
    assert abs(pnl - expected_pnl) < 0.01

    assert fake.risk_manager.released == [("momentum_v1", 46.59 * 175)]
    assert len(fake._notifications) == 1 and "POSITION CLOSED" in fake._notifications[0]


@pytest.mark.asyncio
async def test_close_single_leg_position_on_unknown_contract_returns_false():
    fake = _FakeEngine()
    fake._execute_single_leg_exit = types.MethodType(LiveTradingEngine._execute_single_leg_exit, fake)
    await LiveTradingEngine.close_single_leg_position(fake, "TITAN26AUG4975CE", "first close")

    closed_again = await LiveTradingEngine.close_single_leg_position(fake, "TITAN26AUG4975CE", "retry")
    assert closed_again is False, "closing an already-closed/unknown contract must not crash"


# ── PaperBroker position rebuild across restart (2026-08-06) ────────────────
#
# The most severe bug found this session: broker.get_positions() returned
# nothing for open single-leg positions after any API restart, meaning
# _check_open_option_exits() had zero visibility into them -- exit rules
# silently stopped being evaluated for real, live, open positions.

class _FakeJournal:
    def __init__(self, entry_price, quantity):
        self.entry_price = entry_price
        self.quantity = quantity


class _FakeJournalRepo:
    def __init__(self, model, session):
        pass

    async def get_by_id(self, jid):
        return {154: _FakeJournal(46.59, 175), 155: _FakeJournal(4.95, 1900)}.get(jid)


class _FakeBroker:
    def __init__(self):
        self._positions = {"STALE_LEFTOVER": {"symbol": "STALE_LEFTOVER", "quantity": 100, "avg_price": 1.0}}
        self.margin_blocked = {}


class _FakeRebuildEngine:
    def __init__(self):
        self.broker = _FakeBroker()
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {
            "TITAN26AUG4975CE": {"journal_id": 154, "strategy_name": "momentum_v1"},
            "POWERGRID26AUG270PE": {"journal_id": 155, "strategy_name": "momentum_v1"},
        }


@pytest.mark.asyncio
async def test_rebuild_paper_broker_positions_restores_all_single_leg_positions(monkeypatch):
    import src.database.repositories.base as base_mod
    monkeypatch.setattr(base_mod, "BaseRepository", _FakeJournalRepo)

    fake = _FakeRebuildEngine()
    await LiveTradingEngine._rebuild_paper_broker_positions(fake)

    positions = fake.broker._positions
    assert "STALE_LEFTOVER" not in positions, "clear() should wipe the old dict first"
    assert positions["TITAN26AUG4975CE"]["quantity"] == 175
    assert positions["TITAN26AUG4975CE"]["avg_price"] == 46.59
    assert positions["POWERGRID26AUG270PE"]["quantity"] == 1900
    assert positions["POWERGRID26AUG270PE"]["avg_price"] == 4.95


@pytest.mark.asyncio
async def test_rebuild_paper_broker_positions_skips_missing_journal_safely(monkeypatch):
    import src.database.repositories.base as base_mod
    monkeypatch.setattr(base_mod, "BaseRepository", _FakeJournalRepo)

    fake = _FakeRebuildEngine()
    fake._single_leg_journals["GHOST26AUG100CE"] = {"journal_id": 9999, "strategy_name": "momentum_v1"}

    await LiveTradingEngine._rebuild_paper_broker_positions(fake)

    assert "GHOST26AUG100CE" not in fake.broker._positions
    assert "TITAN26AUG4975CE" in fake.broker._positions  # the other two still work fine


@pytest.mark.asyncio
async def test_rebuild_paper_broker_positions_is_idempotent_across_a_restart_crash_loop(monkeypatch):
    # Realistic pre-live scenario: a container crash-loop (e.g. a bad deploy,
    # OOM, or the ticker watchdog's os._exit(1) firing repeatedly) restarts
    # the process several times in a row before it stabilizes. Rebuilding
    # from the same trade_journal state twice must NOT duplicate quantity,
    # double-block margin, or otherwise diverge from a single rebuild.
    import src.database.repositories.base as base_mod
    monkeypatch.setattr(base_mod, "BaseRepository", _FakeJournalRepo)

    fake = _FakeRebuildEngine()
    await LiveTradingEngine._rebuild_paper_broker_positions(fake)
    first_pass = {k: dict(v) for k, v in fake.broker._positions.items()}

    await LiveTradingEngine._rebuild_paper_broker_positions(fake)
    second_pass = fake.broker._positions

    assert second_pass == first_pass, "a second rebuild must produce identical state, not accumulate/duplicate"


# ── Entry gates fail-open-on-missing-data (2026-08-06) ───────────────────────
#
# The RVOL gate (momentum/ema entries) and all 3 ADX gates (momentum/ema,
# credit_spread [15-30], iron_condor [<20]) used `if metric > 0` before
# applying their threshold -- a metric that couldn't be computed yet
# (insufficient bar history) silently fell back to 0, which failed the
# `> 0` guard and let the entry through completely unchecked.

def test_momentum_ema_entry_gates_check_validity_flags_and_block():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert '_rvol_valid = bool(market_data.get("rvol_valid"' in src
    assert "if not _rvol_valid:" in src
    assert '_adx_ema_valid = bool(market_data.get("adx_valid"' in src
    assert "if not _adx_ema_valid:" in src


def test_credit_spread_adx_gate_checks_validity_flag_and_blocks():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert '_adx_cs_valid = bool(market_data.get("adx_valid"' in src
    assert "if not _adx_cs_valid:" in src


def test_iron_condor_adx_gate_checks_validity_flag_and_blocks():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert '_adx_ic_valid = bool(market_data.get("adx_valid"' in src
    assert "if not _adx_ic_valid:" in src


# ── trade_journal P&L used the real fill price, not the quote (2026-08-07) ──
#
# Three exit paths read getattr(order_obj, "avg_price", ...) to get the real,
# slippage-adjusted fill price -- but the Order model's real column is
# fill_price; "avg_price" never existed on it, so this always silently fell
# back to the pre-slippage quoted price instead of the real PaperBroker fill.

@pytest.mark.asyncio
async def test_single_leg_exit_pnl_uses_real_fill_price_not_quote():
    entry_fill = 5.29
    quoted_exit_price = 5.05
    real_exit_fill = 4.90
    qty = 1900

    fake = _FakeEngine(fill_price=real_exit_fill)
    fake._single_leg_journals["POWERGRID26AUG270PE"] = {
        "journal_id": 155, "underlying": "POWERGRID", "strategy_name": "momentum_v1",
    }

    async def _get_market_data(symbol):
        return {}
    fake._get_market_data = _get_market_data

    await LiveTradingEngine._execute_single_leg_exit(
        fake, "POWERGRID26AUG270PE", qty, entry_fill, quoted_exit_price,
        "momentum_v1 entry=Rs4.95 now=Rs5.05 (+2.0%)",
    )

    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]

    wrong_pnl_if_quote_based = (quoted_exit_price - entry_fill) * qty
    correct_pnl = (real_exit_fill - entry_fill) * qty

    assert abs(pnl - correct_pnl) < 0.01
    assert abs(pnl - wrong_pnl_if_quote_based) > 100, "pnl must not match the old quote-based calculation"
    assert exit_price == real_exit_fill


@pytest.mark.asyncio
async def test_single_leg_exit_pnl_falls_back_to_quote_when_fill_unavailable():
    fake = _FakeEngine(fill_price=None)
    fake._single_leg_journals["POWERGRID26AUG270PE"] = {
        "journal_id": 155, "underlying": "POWERGRID", "strategy_name": "momentum_v1",
    }

    async def _get_market_data(symbol):
        return {}
    fake._get_market_data = _get_market_data

    await LiveTradingEngine._execute_single_leg_exit(
        fake, "POWERGRID26AUG270PE", 1900, 5.29, 5.05, "fallback test",
    )

    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]
    expected_fallback_pnl = (5.05 - 5.29) * 1900
    assert abs(pnl - expected_fallback_pnl) < 0.01


# ── fill_price Decimal/float TypeError crashed the whole signal cycle (2026-08-12) ──
#
# db_order.fill_price is a SQLAlchemy Numeric(18,4) column -- reads back as
# decimal.Decimal, not float (see database/models/order.py). Every fill_price
# call site mixed it with plain floats in subtraction, which raises an
# uncaught TypeError. Confirmed live: 5 crashes across 2026-08-10/11/12 (4 in
# _execute_single_leg_exit, 1 in _square_off_all), each one aborting the
# ENTIRE run_signal_cycle mid-iteration -- the SELL order had already filled
# by that point, but everything after the crash (journal close, capital
# release, and for _square_off_all specifically, every OTHER position still
# left to close that cycle) never ran. Left 6 trade_journal rows permanently
# stuck with NULL exit_time/exit_price/pnl. All prior fill_price tests in
# this file used a plain float, which is why this slipped through -- these
# use a real Decimal to match what SQLAlchemy actually returns.

@pytest.mark.asyncio
async def test_single_leg_exit_handles_decimal_fill_price_without_crashing():
    fake = _FakeEngine(fill_price=Decimal("4.90"))
    fake._single_leg_journals["POWERGRID26AUG270PE"] = {
        "journal_id": 155, "underlying": "POWERGRID", "strategy_name": "momentum_v1",
    }

    async def _get_market_data(symbol):
        return {}
    fake._get_market_data = _get_market_data

    closed = await LiveTradingEngine._execute_single_leg_exit(
        fake, "POWERGRID26AUG270PE", 1900, 5.29, 5.05,
        "momentum_v1 entry=Rs5.29 now=Rs5.05 (-4.5%)",
    )

    assert closed is True, "a Decimal fill_price must not crash the exit"
    assert len(fake._closed_journals) == 1, "trade_journal close must still run after the crash site"
    _, exit_price, pnl, _ = fake._closed_journals[0]
    assert abs(pnl - (4.90 - 5.29) * 1900) < 0.01
    assert exit_price == 4.90


# ── EOD square-off pnl uses the real fill price (2026-08-07) ────────────────
#
# A fourth instance of the same fill_price bug: _square_off_all() (single-leg
# positions "ALWAYS close" at EOD per its own docstring, so this runs every
# trading day) computed pnl from the pre-slippage quote/ATR-estimate and
# didn't even capture place_order()'s return value.

class _FakeSquareOffOrder:
    def __init__(self, fill_price):
        self.fill_price = fill_price
        self.order_status = "OPEN"


class _FakeSquareOffOrderManager:
    def __init__(self, fill_price):
        self.fill_price = fill_price
        self.calls = []

    async def place_order(self, contract, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None):
        self.calls.append((contract, side, qty, price, is_exit_order, strategy_name, product_override))
        return _FakeSquareOffOrder(self.fill_price)


class _FakeSquareOffEngine:
    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self, fill_price):
        self.order_manager = _FakeSquareOffOrderManager(fill_price)
        self.risk_manager = _FakeRiskManager()
        self._peak_premiums = {"TITAN26AUG4950CE": 50.0}
        self._single_leg_journals = {
            "TITAN26AUG4950CE": {"journal_id": 200, "strategy_name": "ema_crossover_v1"},
        }
        self._active_spreads = {}
        self._active_condors = {}
        self._closed_journals = []
        self._notifications = []
        self._eod_notified_today = False
        self._kite = None
        self._redis = None

    async def _safe_get_positions(self):
        return [{"symbol": "TITAN26AUG4950CE", "quantity": 175, "avg_price": 46.59}]

    async def _get_market_data(self, symbol):
        return {"atr14": 5.0}  # feeds the ATR-estimate fallback since kite/redis are None

    def _get_underlying_from_contract(self, contract):
        return "TITAN"

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, **_kw):
        self._closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state(self):
        pass

    async def _notify(self, msg):
        self._notifications.append(msg)


@pytest.mark.asyncio
async def test_square_off_uses_real_fill_price_not_quote_estimate():
    real_exit_fill = 44.10  # distinct from whatever the ATR estimate computes

    fake = _FakeSquareOffEngine(fill_price=real_exit_fill)
    await LiveTradingEngine._square_off_all(fake)

    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]

    entry_p = 46.59
    qty = 175
    correct_pnl = (real_exit_fill - entry_p) * qty

    assert exit_price == real_exit_fill
    assert abs(pnl - correct_pnl) < 0.01
    assert exit_reason == "EOD square-off"


@pytest.mark.asyncio
async def test_square_off_falls_back_to_estimate_when_fill_unavailable():
    fake = _FakeSquareOffEngine(fill_price=None)
    await LiveTradingEngine._square_off_all(fake)

    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]
    # Falls back to the ATR estimate (a positive, computed value), not None/0
    assert exit_price is not None and exit_price > 0


@pytest.mark.asyncio
async def test_square_off_handles_decimal_fill_price_without_crashing():
    # This is the exact site that crashed live on 2026-08-12 (see the
    # Decimal/float TypeError note above _execute_single_leg_exit's Decimal
    # test) -- aborted _square_off_all mid-loop, leaving every remaining
    # position that cycle unclosed too.
    fake = _FakeSquareOffEngine(fill_price=Decimal("44.10"))
    await LiveTradingEngine._square_off_all(fake)

    assert len(fake._closed_journals) == 1, "a Decimal fill_price must not crash square-off"
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]

    entry_p = 46.59
    qty = 175
    correct_pnl = (44.10 - entry_p) * qty

    assert exit_price == 44.10
    assert abs(pnl - correct_pnl) < 0.01


# ── per-position error isolation in _square_off_all (2026-08-13) ────────────
#
# The 2026-08-12 live Decimal/float crash didn't just fail to close ONE
# position -- it aborted the entire square-off loop, since nothing caught
# the exception before it reached run_signal_cycle's caller. Confirmed with
# 2 positions: the first raises, the second must still get closed.

class _FakeMultiSquareOffOrderManager:
    def __init__(self, fill_price, raise_for_contract):
        self.fill_price = fill_price
        self.raise_for_contract = raise_for_contract
        self.calls = []

    async def place_order(self, contract, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None):
        self.calls.append(contract)
        if contract == self.raise_for_contract:
            raise TypeError("simulated Decimal/float crash for this contract only")
        return _FakeSquareOffOrder(self.fill_price)


class _FakeMultiSquareOffEngine:
    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self):
        self.order_manager = _FakeMultiSquareOffOrderManager(
            fill_price=44.10, raise_for_contract="TITAN26AUG4950CE",
        )
        self.risk_manager = _FakeRiskManager()
        self._peak_premiums = {"TITAN26AUG4950CE": 50.0, "RELIANCE26AUG1400CE": 20.0}
        self._single_leg_journals = {
            "TITAN26AUG4950CE":    {"journal_id": 200, "strategy_name": "ema_crossover_v1"},
            "RELIANCE26AUG1400CE": {"journal_id": 201, "strategy_name": "momentum_v1"},
        }
        self._active_spreads = {}
        self._active_condors = {}
        self._closed_journals = []
        self._notifications = []
        self._eod_notified_today = False
        self._kite = None
        self._redis = None

    async def _safe_get_positions(self):
        return [
            {"symbol": "TITAN26AUG4950CE",    "quantity": 175, "avg_price": 46.59},
            {"symbol": "RELIANCE26AUG1400CE", "quantity": 100, "avg_price": 18.20},
        ]

    async def _get_market_data(self, symbol):
        return {"atr14": 5.0}

    def _get_underlying_from_contract(self, contract):
        return "TITAN" if "TITAN" in contract else "RELIANCE"

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, **_kw):
        self._closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state(self):
        pass

    async def _notify(self, msg):
        self._notifications.append(msg)


@pytest.mark.asyncio
async def test_square_off_one_bad_position_does_not_block_the_rest():
    fake = _FakeMultiSquareOffEngine()

    await LiveTradingEngine._square_off_all(fake)  # must not raise

    # Both positions must have been attempted...
    assert fake.order_manager.calls == ["TITAN26AUG4950CE", "RELIANCE26AUG1400CE"]
    # ...but only the one that didn't crash actually got its journal closed.
    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]
    assert journal_id == 201  # RELIANCE, not the crashing TITAN (200)
    assert exit_price == 44.10
    # The crashing position's journal entry must survive (not popped) so
    # it's retried next cycle instead of silently vanishing.
    assert "TITAN26AUG4950CE" in fake._single_leg_journals
    assert "RELIANCE26AUG1400CE" not in fake._single_leg_journals


# ── expiry-day journal-close failure alerts, doesn't leave a phantom
# active entry (2026-08-13) ──────────────────────────────────────────────
#
# By the time the spread/condor journal-close loop runs on expiry day, the
# REAL broker position is already flat (closed by real orders in the
# per-position loop above). If _log_trade_close() then fails for one
# structure, the entry must NOT be left in _active_spreads/_active_condors
# "for retry" -- that would make future exit-check cycles treat an
# already-closed structure as still open, risking a real order against a
# flat position. It must still be cleared, but with a loud CRITICAL alert
# carrying enough detail to fix the trade_journal row by hand, instead of
# silently losing it forever with just a log line.

class _FakeExpiryJournalFailOrderManager:
    def __init__(self, fill_price):
        self.fill_price = fill_price

    async def place_order(self, contract, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None):
        return _FakeSquareOffOrder(self.fill_price)


class _FakeExpiryJournalFailEngine:
    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self):
        self.order_manager = _FakeExpiryJournalFailOrderManager(fill_price=5.0)
        self.risk_manager = _FakeRiskManager()
        self._peak_premiums = {}
        self._single_leg_journals = {}
        self._active_spreads = {
            "GOODSPR": {
                "journal_id": 300, "short_contract": "GOODSPR26AUG100PE",
                "long_contract": "GOODSPR26AUG90PE", "short_premium": 10.0,
                "long_premium": 4.0, "lot_size": 100, "short_strike": 100,
                "long_strike": 90, "net_credit": 6.0, "strategy_name": "credit_spread_v1",
            },
            "BADSPR": {
                "journal_id": 301, "short_contract": "BADSPR26AUG200PE",
                "long_contract": "BADSPR26AUG190PE", "short_premium": 8.0,
                "long_premium": 3.0, "lot_size": 50, "short_strike": 200,
                "long_strike": 190, "net_credit": 5.0, "strategy_name": "credit_spread_v1",
            },
        }
        self._active_condors = {}
        self._closed_journals = []
        self._notifications = []
        self._eod_notified_today = False
        self._kite = None
        self._redis = None

    async def _safe_get_positions(self):
        # Fixed 2026-08-20 (deep review): the expiry-day consolidation block
        # now only journals/clears a structure whose legs are ALL confirmed
        # closed in the per-position loop above (present in _exit_prices) --
        # so this fixture must supply real broker positions for every leg of
        # both spreads for that loop to actually run and succeed, letting
        # this test isolate the downstream journal-write failure it's meant
        # to exercise, rather than a leg never having been attempted at all.
        return [
            {"symbol": "GOODSPR26AUG100PE", "quantity": -100, "avg_price": 10.0},
            {"symbol": "GOODSPR26AUG90PE",  "quantity": 100,  "avg_price": 4.0},
            {"symbol": "BADSPR26AUG200PE",  "quantity": -50,  "avg_price": 8.0},
            {"symbol": "BADSPR26AUG190PE",  "quantity": 50,   "avg_price": 3.0},
        ]

    async def _get_market_data(self, symbol):
        return {"atr14": 5.0}

    def _get_underlying_from_contract(self, contract):
        return contract[:7]

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, **_kw):
        if journal_id == 301:  # BADSPR
            raise RuntimeError("simulated DB write failure")
        self._closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state(self):
        pass

    async def _notify(self, msg):
        self._notifications.append(msg)

    async def _cancel_gtt(self, gtt_id, contract=""):
        pass


@pytest.mark.asyncio
async def test_expiry_journal_close_failure_alerts_and_still_clears_the_entry():
    fake = _FakeExpiryJournalFailEngine()

    with pytest.MonkeyPatch.context() as mp:
        from datetime import timedelta
        from src.core.utils import now_ist
        mp.setattr(
            "src.live_trading.live_trading_engine.get_near_month_expiry",
            lambda: now_ist().replace(tzinfo=None) + timedelta(days=1),  # DTE=1 -> is_expiry
        )
        await LiveTradingEngine._square_off_all(fake)  # must not raise

    # The good spread closed normally.
    assert len(fake._closed_journals) == 1
    assert fake._closed_journals[0][0] == 300

    # The bad spread's journal write failed -> a loud, actionable alert, not just a log line.
    critical_alerts = [m for m in fake._notifications if "CRITICAL" in m]
    assert len(critical_alerts) == 1
    assert "journal_id=301" in critical_alerts[0]
    assert "MANUAL INTERVENTION REQUIRED" in critical_alerts[0]
    assert "BADSPR" in critical_alerts[0]

    # Both entries are cleared regardless -- the real broker position is
    # already flat either way, so a "failed" entry must not be left behind
    # looking like it's still an open, active structure.
    assert fake._active_spreads == {}


# ── _exit_all_options_for (2026-08-07) ───────────────────────────────────────
#
# Currently dead code in practice (no active strategy's generate_signal()
# returns SignalType.EXIT -- only manage_position() does, handled elsewhere),
# but SignalType.EXIT is part of the formal signal contract, so a future
# strategy could reach this path. It was missing trade_journal logging
# entirely (a closed position would show as permanently open forever) and
# used the pre-slippage quote/estimate instead of the real fill.

class _FakeExitAllOrder:
    def __init__(self, fill_price):
        self.fill_price = fill_price
        self.order_status = "OPEN"


class _FakeExitAllOrderManager:
    def __init__(self, fill_price):
        self.fill_price = fill_price
        self.calls = []

    async def place_order(self, contract, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None):
        self.calls.append((contract, side, qty, price, is_exit_order, strategy_name, product_override))
        return _FakeExitAllOrder(self.fill_price)


class _FakeExitAllEngine:
    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self, fill_price):
        self.order_manager = _FakeExitAllOrderManager(fill_price)
        self.risk_manager = _FakeRiskManager()
        self._peak_premiums = {"CIPLA26AUG1250CE": 30.0}
        self._single_leg_journals = {
            "CIPLA26AUG1250CE": {"journal_id": 300, "strategy_name": "ema_crossover_v1"},
        }
        self._active_spreads = {}
        self._active_condors = {}
        self._exited_today = set()
        self._closed_journals = []
        self._kite = None
        self._redis = None

    async def _safe_get_positions(self):
        return [{"symbol": "CIPLA26AUG1250CE", "quantity": 650, "avg_price": 22.00}]

    async def _get_market_data(self, symbol):
        return {"atr14": 3.0}

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, market_data=None, total_slippage_pts=None, **_kw):
        self._closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state(self):
        pass


@pytest.mark.asyncio
async def test_exit_all_options_for_writes_trade_journal_with_real_fill():
    real_exit_fill = 25.50

    fake = _FakeExitAllEngine(fill_price=real_exit_fill)
    await LiveTradingEngine._exit_all_options_for(fake, "CIPLA")

    assert "CIPLA26AUG1250CE" not in fake._single_leg_journals, "journal entry must be popped on close"
    assert len(fake._closed_journals) == 1, "must write to trade_journal, not leave it permanently open"

    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]
    entry_p, qty = 22.00, 650
    assert exit_price == real_exit_fill
    assert abs(pnl - (real_exit_fill - entry_p) * qty) < 0.01
    assert "CIPLA" in fake._exited_today


@pytest.mark.asyncio
async def test_exit_all_options_for_falls_back_when_fill_unavailable():
    fake = _FakeExitAllEngine(fill_price=None)
    await LiveTradingEngine._exit_all_options_for(fake, "CIPLA")

    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]
    assert exit_price is not None and exit_price > 0


# ── GTT backstop failure alerting (2026-08-07) ───────────────────────────────
#
# _place_gtt_backstop() places a server-independent, exchange-level stop-loss
# on a short option leg -- explicitly documented as protecting the position
# "even if the server crashes entirely". Gated to TradingMode.LIVE only, so
# it has never once executed against a real broker in this project's paper-
# trading history. Both failure paths (no kite client, or the placement
# itself raising) used to only log a warning -- for a feature whose entire
# point is "protect the position when nobody's watching", that's the wrong
# failure mode. Must now notify.

from src.core.enums import TradingMode


class _FakeGttEngine:
    def __init__(self, mode, kite):
        self.mode = mode
        self._kite = kite
        self._notifications = []

    async def _notify(self, msg):
        self._notifications.append(msg)


@pytest.mark.asyncio
async def test_gtt_backstop_paper_mode_is_a_silent_noop():
    # Not LIVE mode is the normal, expected case (this whole project's
    # history so far) -- must NOT alert, that would be constant noise.
    fake = _FakeGttEngine(mode=TradingMode.PAPER, kite=None)
    gtt_id = await LiveTradingEngine._place_gtt_backstop(fake, "SBIN26AUG800PE", 750, 20.0)
    assert gtt_id is None
    assert fake._notifications == []


@pytest.mark.asyncio
async def test_gtt_backstop_live_mode_no_kite_client_notifies():
    fake = _FakeGttEngine(mode=TradingMode.LIVE, kite=None)
    gtt_id = await LiveTradingEngine._place_gtt_backstop(fake, "SBIN26AUG800PE", 750, 20.0)
    assert gtt_id is None
    assert len(fake._notifications) == 1
    assert "GTT backstop" in fake._notifications[0]


@pytest.mark.asyncio
async def test_gtt_backstop_live_mode_placement_failure_notifies():
    class _FailingKite:
        def place_gtt(self, **kwargs):
            raise RuntimeError("simulated Zerodha API error")

    fake = _FakeGttEngine(mode=TradingMode.LIVE, kite=_FailingKite())
    gtt_id = await LiveTradingEngine._place_gtt_backstop(fake, "SBIN26AUG800PE", 750, 20.0)
    assert gtt_id is None
    assert len(fake._notifications) == 1
    assert "GTT backstop" in fake._notifications[0]
    assert "simulated Zerodha API error" in fake._notifications[0]


@pytest.mark.asyncio
async def test_gtt_backstop_live_mode_success_returns_id_no_notification():
    class _SucceedingKite:
        def place_gtt(self, **kwargs):
            assert kwargs["trigger_values"] == [50.0]  # 2.5x entry(20.0)
            assert kwargs["orders"][0]["transaction_type"] == "BUY"
            assert kwargs["orders"][0]["quantity"] == 750
            return {"trigger_id": 12345}

    fake = _FakeGttEngine(mode=TradingMode.LIVE, kite=_SucceedingKite())
    gtt_id = await LiveTradingEngine._place_gtt_backstop(fake, "SBIN26AUG800PE", 750, 20.0)
    assert gtt_id == 12345
    assert fake._notifications == []


# ── Orphan-position auto-close passes product_override (2026-08-07) ─────────
#
# strategy_name is fundamentally unavailable for an orphaned position (the
# engine lost tracking, that's the whole problem) -- so the closing order
# must instead carry Zerodha's own "product" field straight from the
# position record, or a MIS position closed with a guessed/defaulted NRML
# would be rejected by the exchange (product mismatch on a closing order).

class _FakeReconcileBroker:
    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self):
        return self._positions


class _FakeReconcileOrderManager:
    def __init__(self):
        self.calls = []

    async def place_order(self, contract, side, qty, price, is_exit_order=False,
                           strategy_name=None, product_override=None):
        self.calls.append((contract, side, qty, price, is_exit_order, strategy_name, product_override))
        return None


class _FakeReconcileEngine:
    def __init__(self, positions):
        self.broker = _FakeReconcileBroker(positions)
        self.order_manager = _FakeReconcileOrderManager()
        self._active_spreads = {}
        self._active_condors = {}
        self._single_leg_journals = {}
        self._notifications = []
        self._kite = None
        self._redis = None

    async def _notify(self, msg):
        self._notifications.append(msg)


@pytest.mark.asyncio
async def test_reconcile_orphan_close_passes_broker_product_as_override():
    orphan = {
        "symbol": "GHOST26AUG100CE", "quantity": 175,
        "avg_price": 42.5, "product": "MIS",
    }
    fake = _FakeReconcileEngine([orphan])

    await LiveTradingEngine._reconcile_broker_positions(fake)

    assert len(fake.order_manager.calls) == 1
    contract, side, qty, price, is_exit, strategy_name, product_override = fake.order_manager.calls[0]
    assert contract == "GHOST26AUG100CE"
    assert side == "SELL"
    assert qty == 175
    assert is_exit is True
    assert product_override == "MIS", (
        "orphan close must pass the broker's own product field, not guess from "
        "a strategy_name that's unavailable for an untracked position"
    )


@pytest.mark.asyncio
async def test_reconcile_skips_positions_already_tracked():
    tracked = {"symbol": "TITAN26AUG4975CE", "quantity": 175, "avg_price": 46.59, "product": "NRML"}
    fake = _FakeReconcileEngine([tracked])
    fake._single_leg_journals["TITAN26AUG4975CE"] = {"strategy_name": "momentum_v1"}

    await LiveTradingEngine._reconcile_broker_positions(fake)

    assert fake.order_manager.calls == [], "a position the engine already tracks must not be auto-closed"


# ── Credit spread / iron condor exits use the real fill, not the quote (2026-08-13) ──
#
# net_pnl and exit_price were computed from cur_short/cur_long (the pre-trade
# QUOTE fed into place_order()) instead of the real fill -- despite the real
# fill being computed immediately afterward, but only for the slippage
# diagnostic. Confirmed live: a real TITAN BULL_PUT_SPREAD closed with
# recorded pnl=Rs7,638.75 (quote-based) vs the real fill-based pnl of
# Rs7,390.25 -- a Rs248.50 overstatement from slippage the record never
# reflected. These tests reproduce that exact trade's numbers.

class _FakeSpreadOrder:
    def __init__(self, fill_price, status="OPEN"):
        self.fill_price = fill_price
        self.order_status = status


class _FakeSpreadOrderManager:
    def __init__(self, fills: dict):
        self._fills = fills  # contract -> fill_price
        self.calls = []

    async def place_order(self, contract, side, qty, price, is_spread_leg=False, is_exit_order=False):
        self.calls.append((contract, side, qty, price))
        return _FakeSpreadOrder(self._fills[contract])


class _FakeSpreadRiskManager:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


class _FakeSpreadExitEngine:
    """Reproduces the real 2026-08-13 TITAN BULL_PUT_SPREAD close: quoted
    exit prices of 36.35 (short buyback) / 11.00 (long sell), real fills of
    37.44 / 10.67 after slippage."""

    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self):
        self.order_manager = _FakeSpreadOrderManager({
            "TITAN26SEP4650PE": 37.44,   # short leg real fill (BUY to close)
            "TITAN26SEP4400PE": 10.67,   # long leg real fill (SELL to close)
        })
        self.risk_manager = _FakeSpreadRiskManager()
        self._active_spreads = {
            "TITAN": {
                "short_contract": "TITAN26SEP4650PE", "long_contract": "TITAN26SEP4400PE",
                "short_premium": 82.0, "long_premium": 13.0, "net_credit": 69.0,
                "short_strike": 4650, "long_strike": 4400, "option_type": "PE",
                "spread_type": "BULL_PUT_SPREAD", "lot_size": 175,
                "entry_vix": 0.0, "gtt_id": None, "strategy_name": "credit_spread_v1",
            },
        }
        self._active_condors = {}
        self._exited_today = set()
        self._profit_closed_today = set()
        self._close_on_first_cycle = set()
        self._closed_journals = []
        self._notifications = []
        self._ltp_poller = None
        self._kite = None
        self._redis = None

    async def _get_market_data(self, symbol):
        # current_price safely above short_strike (4650) so the put-breach
        # check doesn't fire before the DTE-tiered profit check does.
        return {"close": 4900.0, "atr14": 20.0}

    async def _get_cached_vix(self):
        return None

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, market_data, total_slippage_pts, **_kw):
        self._closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state(self):
        pass

    async def _notify(self, msg):
        self._notifications.append(msg)

    async def _cancel_gtt(self, gtt_id, contract=""):
        pass

    async def _safe_get_positions(self):
        # Both legs still genuinely open at the broker -- 2026-08-20 fix's
        # "already flat, skip re-submitting" shortcut in _close_leg() must
        # NOT trigger here, so the order actually gets placed and its real
        # fill (not the quote) is what the test verifies.
        self._broker_position_state_known = True
        return [
            {"symbol": "TITAN26SEP4650PE", "quantity": -175, "avg_price": 82.0},
            {"symbol": "TITAN26SEP4400PE", "quantity": 175,  "avg_price": 13.0},
        ]


@pytest.mark.asyncio
async def test_spread_exit_pnl_uses_real_fill_not_quote():
    from datetime import timedelta
    from src.core.utils import now_ist

    fake = _FakeSpreadExitEngine()

    with pytest.MonkeyPatch.context() as mp:
        # Fixed DTE=11 (matches the real trade's "DTE-tiered profit (DTE=11)"
        # exit and its 45% tier) and a fixed quote (36.35 / 11.00, matching
        # the real pre-trade quote logged for this exact close).
        mp.setattr(
            "src.live_trading.live_trading_engine.get_near_month_expiry",
            lambda: now_ist().replace(tzinfo=None) + timedelta(days=11),
        )

        async def _fake_get_option_quote(contract, kite, redis):
            return {"TITAN26SEP4650PE": 36.35, "TITAN26SEP4400PE": 11.00}[contract]
        mp.setattr("src.market_data.option_chain.get_option_quote", _fake_get_option_quote)

        await LiveTradingEngine._check_spread_exits(fake, active_strategies={})

    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]

    # Real fill-based numbers, NOT the quote-based ones (short 36.35/long
    # 11.00 would give exit_price=25.35, pnl=7638.75 -- the bug).
    assert exit_price == 26.77
    assert abs(pnl - 7390.25) < 0.01
    assert "DTE-tiered profit" in exit_reason

    # TITAN must be fully closed out and routed to the profit bucket.
    assert "TITAN" not in fake._active_spreads
    assert "TITAN" in fake._profit_closed_today


class _FakeCondorOrder:
    def __init__(self, fill_price, status="OPEN"):
        self.fill_price = fill_price
        self.order_status = status


class _FakeCondorOrderManager:
    def __init__(self, fills: dict):
        self._fills = fills
        self.calls = []

    async def place_order(self, contract, side, qty, price, is_spread_leg=False, is_exit_order=False):
        self.calls.append((contract, side, qty, price))
        return _FakeCondorOrder(self._fills[contract])


class _FakeCondorExitEngine:
    """Same fill-vs-quote fix, on the 4-leg iron condor exit path."""

    _real_fill = staticmethod(LiveTradingEngine._real_fill)

    def __init__(self):
        # Quote-side prices (what the exit DECISION and old buggy pnl used):
        #   put short 4.5, put long 2.0, call short 15.0, call long 6.0
        # Real fills (what pnl/exit_price must now use instead):
        #   put short 4.8, put long 1.9, call short 15.2, call long 5.9
        self.order_manager = _FakeCondorOrderManager({
            "SBIN26SEP4400PE": 4.8,
            "SBIN26SEP4300PE": 1.9,
            "SBIN26SEP4900CE": 15.2,
            "SBIN26SEP5000CE": 5.9,
        })
        self.risk_manager = _FakeSpreadRiskManager()
        self._active_spreads = {}
        self._active_condors = {
            "SBIN": {
                "put_short_contract": "SBIN26SEP4400PE", "put_long_contract": "SBIN26SEP4300PE",
                "call_short_contract": "SBIN26SEP4900CE", "call_long_contract": "SBIN26SEP5000CE",
                "put_short_premium": 20.0, "put_long_premium": 8.0,
                "call_short_premium": 18.0, "call_long_premium": 7.0,
                "put_short_strike": 4400, "put_long_strike": 4300,
                "call_short_strike": 4900, "call_long_strike": 5000,
                "net_credit": 23.0, "lot_size": 175,
                "entry_vix": 0.0, "strategy_name": "iron_condor_v1",
            },
        }
        self._exited_today = set()
        self._profit_closed_today = set()
        self._close_on_first_cycle = set()
        self._closed_journals = []
        self._notifications = []
        self._ltp_poller = None
        self._kite = None
        self._redis = None

    async def _get_market_data(self, symbol):
        # Between both short strikes (4400 put / 4900 call) so neither
        # breach check fires before the profit-target check does.
        return {"close": 4650.0, "atr14": 20.0}

    async def _get_cached_vix(self):
        return None

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, market_data, total_slippage_pts, **_kw):
        self._closed_journals.append((journal_id, exit_price, pnl, exit_reason))

    async def _persist_state(self):
        pass

    async def _notify(self, msg):
        self._notifications.append(msg)

    async def _cancel_gtt(self, gtt_id, contract=""):
        pass

    async def _safe_get_positions(self):
        # All 4 legs still genuinely open at the broker -- see the matching
        # comment on _FakeSpreadExitEngine._safe_get_positions().
        self._broker_position_state_known = True
        return [
            {"symbol": "SBIN26SEP4400PE", "quantity": -175, "avg_price": 20.0},
            {"symbol": "SBIN26SEP4300PE", "quantity": 175,  "avg_price": 8.0},
            {"symbol": "SBIN26SEP4900CE", "quantity": -175, "avg_price": 18.0},
            {"symbol": "SBIN26SEP5000CE", "quantity": 175,  "avg_price": 7.0},
        ]


@pytest.mark.asyncio
async def test_condor_exit_pnl_uses_real_fill_not_quote():
    from datetime import timedelta
    from src.core.utils import now_ist

    fake = _FakeCondorExitEngine()

    with pytest.MonkeyPatch.context() as mp:
        # DTE=25 -> default profit_pct stays 0.25 (no DTE-tiering involved).
        mp.setattr(
            "src.live_trading.live_trading_engine.get_near_month_expiry",
            lambda: now_ist().replace(tzinfo=None) + timedelta(days=25),
        )

        _quotes = {
            "SBIN26SEP4400PE": 4.5, "SBIN26SEP4300PE": 2.0,
            "SBIN26SEP4900CE": 15.0, "SBIN26SEP5000CE": 6.0,
        }
        async def _fake_get_option_quote(contract, kite, redis):
            return _quotes[contract]
        mp.setattr("src.market_data.option_chain.get_option_quote", _fake_get_option_quote)

        await LiveTradingEngine._check_condor_exits(fake, active_strategies={})

    assert len(fake._closed_journals) == 1
    journal_id, exit_price, pnl, exit_reason = fake._closed_journals[0]

    # Fill-based: (20-4.8)+(18-15.2)-(8-1.9)-(7-5.9) = 10.8 * 175 = 1890.0
    # exit_price = 4.8+15.2-1.9-5.9 = 12.2
    # Quote-based (the bug) would instead give exit_price=11.5, pnl=2012.5.
    assert exit_price == 12.2
    assert abs(pnl - 1890.0) < 0.01

    assert "SBIN" not in fake._active_condors
    assert "SBIN" in fake._profit_closed_today


# ── Entry-side counterpart of the same fill-vs-quote bug (2026-08-13) ────────
#
# The exit-side fix above (test_spread_exit_pnl_uses_real_fill_not_quote /
# test_condor_exit_pnl_uses_real_fill_not_quote) only closed half the gap.
# short_p/long_p and psc/plc/csc/clc's prices in _process_credit_spread /
# _process_iron_condor are the pre-trade QUOTES fed into place_order() --
# the returned orders already carry the real fill_price (place_order()
# reconciles it synchronously, "fixed 2026-08-07"), but entry code never
# read it before storing short_premium/long_premium (which drive every
# later SL/profit-target check and the exit pnl calc) or the journal's
# entry_price. Confirmed live: this is a real, currently-open-position bug,
# not just historical -- BAJFINANCE (id=157) and TATASTEEL (id=161) both
# have their live thresholds anchored to quote-based entry premiums.
# net_credit deliberately stays quote-based (see the fix's comment) since
# it must match _capital_at_risk's basis for add/release symmetry.

def test_credit_spread_entry_uses_real_fill_not_quote():
    src = inspect.getsource(LiveTradingEngine._process_credit_spread)
    assert "short_fill = self._real_fill(short_order, short_p)" in src
    assert "long_fill  = self._real_fill(long_order,  long_p)" in src
    assert "entry_price=round(short_fill - long_fill, 2)" in src
    assert '"short_premium":  short_fill,      "long_premium":   long_fill,' in src
    # GTT backstop trigger must also be anchored to the real fill.
    assert "_place_gtt_backstop(short_contract, lot_size, short_fill)" in src


# ── Cross-strategy contract-collision guard (2026-08-13) ─────────────────────
#
# Confirmed live: ema_crossover_v1 independently bought and sold
# BAJFINANCE26SEP1190CE while credit_spread_v1 was already holding that
# exact contract open as a spread's long leg (id=157). Invisible in paper
# mode (each strategy's fills are simulated independently), but Zerodha
# nets ONE position per contract per account -- two strategies trading the
# same underlying's options independently would corrupt real position/
# margin accounting once live. Guarded both directions: spread/condor entry
# skips if the underlying already has an open single-leg position, and
# single-leg entry skips if the underlying already has an active spread/condor.
#
# Consolidated 2026-08-13 into two small shared methods (were 3 independent
# inline copies at each entry site -- a future 4th entry path had nothing
# forcing it to remember the check).

# ── _real_fill() -- shared fill-vs-quote helper (2026-08-13) ────────────────
#
# Extracted from ~13 inlined copies of `float(getattr(order, "fill_price",
# None) or fallback)` scattered across every entry/exit path -- this exact
# bug class (quote instead of real fill) had already been found and fixed
# piecemeal at 6+ call sites earlier the same day.

def test_real_fill_prefers_order_fill_price():
    order = SimpleNamespace(fill_price=37.44)
    assert LiveTradingEngine._real_fill(order, fallback=36.35) == 37.44


def test_real_fill_falls_back_when_no_fill_price():
    order = SimpleNamespace(fill_price=None)
    assert LiveTradingEngine._real_fill(order, fallback=36.35) == 36.35


def test_real_fill_falls_back_when_order_is_none():
    assert LiveTradingEngine._real_fill(None, fallback=36.35) == 36.35


def test_real_fill_casts_decimal_to_float():
    order = SimpleNamespace(fill_price=Decimal("4.90"))
    result = LiveTradingEngine._real_fill(order, fallback=5.05)
    assert result == 4.90
    assert isinstance(result, float)


def test_has_active_multi_leg_structure():
    fake = SimpleNamespace(_active_spreads={"TITAN": {}}, _active_condors={"SBIN": {}})
    assert LiveTradingEngine._has_active_multi_leg_structure(fake, "TITAN") is True
    assert LiveTradingEngine._has_active_multi_leg_structure(fake, "SBIN") is True
    assert LiveTradingEngine._has_active_multi_leg_structure(fake, "INFY") is False


def test_has_open_single_leg_position():
    fake = SimpleNamespace(_single_leg_journals={"TITAN26SEP4650PE": {"underlying": "TITAN"}})
    assert LiveTradingEngine._has_open_single_leg_position(fake, "TITAN") is True
    assert LiveTradingEngine._has_open_single_leg_position(fake, "SBIN") is False


class _FakeCollisionEngine:
    # Bind the real (small, pure) helper methods rather than reimplementing
    # them -- the function descriptor protocol binds `self` correctly when
    # called as fake._has_active_multi_leg_structure(symbol).
    _has_active_multi_leg_structure = LiveTradingEngine._has_active_multi_leg_structure
    _has_open_single_leg_position   = LiveTradingEngine._has_open_single_leg_position
    _audit_gate                     = LiveTradingEngine._audit_gate

    def __init__(self, active_spreads=None, active_condors=None, single_leg_journals=None):
        self._active_spreads = active_spreads or {}
        self._active_condors = active_condors or {}
        self._single_leg_journals = single_leg_journals or {}
        self._exited_today = set()
        self._max_daily_orders = 0
        self._last_signal_date = {}
        self._signal_gate_stats = {}
        self.order_manager = None  # must never be touched if the guard fires first


@pytest.mark.asyncio
async def test_credit_spread_entry_blocked_by_open_single_leg_on_same_underlying():
    fake = _FakeCollisionEngine(
        single_leg_journals={"BAJFINANCE26SEP1190CE": {"underlying": "BAJFINANCE", "strategy_name": "ema_crossover_v1"}},
    )
    strategy = SimpleNamespace(name="credit_spread_v1", min_dte=7)

    await LiveTradingEngine._process_credit_spread(
        fake, strategy, "BAJFINANCE", "BEAR_CALL_SPREAD", {"close": 1200.0}, vix=15.0,
    )

    assert fake._active_spreads == {}  # never got past the guard to place any order


@pytest.mark.asyncio
async def test_iron_condor_entry_blocked_by_open_single_leg_on_same_underlying():
    fake = _FakeCollisionEngine(
        single_leg_journals={"SBIN26SEP600CE": {"underlying": "SBIN", "strategy_name": "momentum_v1"}},
    )
    strategy = SimpleNamespace(name="iron_condor_v1", min_dte=7)

    await LiveTradingEngine._process_iron_condor(fake, strategy, "SBIN", {"close": 580.0}, vix=15.0)

    assert fake._active_condors == {}


@pytest.mark.asyncio
async def test_single_leg_entry_blocked_by_active_spread_on_same_underlying():
    fake = _FakeCollisionEngine(active_spreads={"BAJFINANCE": {"short_contract": "BAJFINANCE26SEP1160CE"}})
    fake._get_market_data = AsyncMock(return_value={"close": 1200.0, "ltp_source": "live_tick"})
    fake._notify = AsyncMock()
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "BAJFINANCE", vix=15.0, regime="TRENDING")

    assert fake.order_manager is None  # guard fired before any order could be placed


# ── Paused strategies must keep observing (2026-09-03, external review) ────
#
# generate_signal() used to sit behind `if not strategy.is_active: return`,
# meaning a paused strategy (regime switch, StrategyMonitor circuit-breaker,
# or a manual pause) never called generate_signal() at all -- freezing
# EMACrossoverStrategy's/MomentumStrategy's cross-cycle state
# (prev_fast_ema/prev_slow_ema, pending-confirmation bar counts,
# pullback/breakout tracking) for the entire pause window. A genuine
# crossover/pullback event completing AND reversing during that window was
# permanently, silently missed. Fix: generate_signal() is now always called
# (observation continues); a resulting signal is only acted on if the
# strategy is currently active (execution stays regime/circuit-breaker gated).

def test_generate_signal_called_before_is_active_check():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    gen_idx = src.index("strategy.generate_signal(market_data)")
    active_idx = src.index("if not strategy.is_active:")
    assert gen_idx < active_idx, (
        "generate_signal() must run (state tracking) before the is_active "
        "gate, not after -- a paused strategy must keep observing"
    )


@pytest.mark.asyncio
async def test_paused_strategy_still_calls_generate_signal_but_places_no_order():
    fake = _FakeCollisionEngine()
    fake._get_market_data = AsyncMock(return_value={"close": 1200.0, "ltp_source": "live_tick"})
    fake._notify = AsyncMock()
    calls = []

    def _tracking_generate_signal(market_data):
        calls.append(market_data)
        return SignalType.BUY

    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=False,
        generate_signal=_tracking_generate_signal,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "BAJFINANCE", vix=15.0, regime="TRENDING")

    assert len(calls) == 1, "generate_signal() must still be called while paused (state tracking)"
    assert fake.order_manager is None  # but no order-placement path was ever reached


@pytest.mark.asyncio
async def test_active_strategy_generate_signal_can_still_lead_to_an_order_attempt():
    """Guard against over-fixing -- an ACTIVE strategy's BUY signal must
    still reach the downstream entry path (verified here via the same
    single-leg collision guard already exercised above, which requires
    generate_signal() + is_active + the entry pipeline all to have run)."""
    fake = _FakeCollisionEngine(active_spreads={"BAJFINANCE": {"short_contract": "BAJFINANCE26SEP1160CE"}})
    fake._get_market_data = AsyncMock(return_value={"close": 1200.0, "ltp_source": "live_tick"})
    fake._notify = AsyncMock()
    strategy = SimpleNamespace(
        name="ema_crossover_v1", is_active=True,
        generate_signal=lambda market_data: SignalType.BUY,
    )

    await LiveTradingEngine._process_signal(fake, strategy, "BAJFINANCE", vix=15.0, regime="TRENDING")

    assert fake.order_manager is None  # collision guard fired, but only AFTER the pipeline ran


def test_iron_condor_entry_uses_real_fill_not_quote():
    src = inspect.getsource(LiveTradingEngine._process_iron_condor)
    assert 'placed.append((contract, side, price, is_leg, order))' in src
    assert "_fills = {c: self._real_fill(o, p) for c, _s, p, _l, o in placed}" in src
    assert "entry_price=round(put_short_fill + call_short_fill - put_long_fill - call_long_fill, 2)" in src
    assert '"put_short_premium":   put_short_fill,  "put_long_premium":  put_long_fill,' in src
    assert '"call_short_premium":  call_short_fill, "call_long_premium": call_long_fill,' in src
    assert "_place_gtt_backstop(psc, lot_size, put_short_fill)" in src
    assert "_place_gtt_backstop(csc, lot_size, call_short_fill)" in src


# ── EOD report shows real capital, not just PnL (2026-08-13) ────────────────
#
# The daily email/Telegram report had no capital figures at all -- just
# PnL/order counts. initial_capital must reflect the compounding
# expiry-to-expiry period (RiskManager.set_capital(), not a static .env
# constant), and in live mode capital_left/capital_in_use must prefer
# Zerodha's real margins() over the internal deployed-capital estimate.

def test_daily_report_includes_capital_lines_sourced_from_risk_manager_and_zerodha():
    src = inspect.getsource(LiveTradingEngine.send_daily_report)
    assert "initial_capital = self.risk_manager.initial_capital" in src
    assert "capital_in_use  = sum(self.risk_manager.get_deployed_by_strategy().values())" in src
    assert "capital_left    = initial_capital - capital_in_use" in src
    assert "if self.mode == TradingMode.LIVE and self._kite:" in src
    assert "from src.live_trading.zerodha_sync import get_zerodha_capital" in src
    assert '"Initial Capital: ₹{initial_capital:,.2f}"' in src
    assert '"Capital in Use:  ₹{capital_in_use:,.2f}"' in src
    assert '"Capital Left:    ₹{capital_left:,.2f}"' in src
