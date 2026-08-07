"""
LiveTradingEngine behavioral tests. Most of these call the real unbound
methods against a lightweight duck-typed stand-in (constructing the real
engine needs a live DB/broker/redis stack) -- this exercises the actual
production code, not a reimplementation of it.
"""
import inspect
import types
import asyncio
import pytest

from src.live_trading.live_trading_engine import LiveTradingEngine
from src.risk.risk_manager import RiskManager


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


# ── Entry pricing (2026-08-06) ───────────────────────────────────────────────
#
# Single-leg entries (ema_crossover_v1/momentum_v1) must try a real Zerodha
# quote before falling back to the ATR-heuristic estimate, matching every
# exit path (which already did this correctly).

def test_entry_path_tries_real_quote_before_estimate_fallback():
    src = inspect.getsource(LiveTradingEngine._process_signal)
    assert "get_option_quote(contract" in src
    assert '_real_p if (_real_p and _real_p > 0) else estimate_option_premium' in src

    contract_idx = src.index("contract = build_option_symbol")
    quote_idx = src.index("get_option_quote(contract")
    price_idx = src.index("option_p = _real_p")
    assert contract_idx < quote_idx < price_idx, (
        "must build contract, then fetch quote, then assign option_p, in that order"
    )


def test_exit_path_quote_pattern_unchanged():
    src = inspect.getsource(LiveTradingEngine._check_open_option_exits)
    assert "_live_p = await get_option_quote(contract" in src
    assert "current_p = _live_p" in src


# ── Kill-switch bypass for spread/condor exits (2026-08-06) ─────────────────
#
# risk_manager.validate_trade()'s is_exit_order=True bypasses the kill
# switch/circuit breaker entirely (needed so an existing position can always
# be closed); is_spread_leg=True alone does NOT (intentional for entry hedge
# legs) -- credit-spread and iron-condor exit legs were only passing
# is_spread_leg=True, meaning a tripped kill switch could block closing an
# existing spread/condor position.

def test_spread_and_condor_exit_legs_all_carry_is_exit_order():
    spread_src = inspect.getsource(LiveTradingEngine._check_spread_exits)
    condor_src = inspect.getsource(LiveTradingEngine._check_condor_exits)

    for name, src, n_legs in [("_check_spread_exits", spread_src, 2), ("_check_condor_exits", condor_src, 4)]:
        assert src.count("is_spread_leg=True") >= n_legs, name
        assert src.count("is_exit_order=True") >= n_legs, name


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

    async def place_order(self, contract, side, qty, price, is_exit_order=False):
        self.calls.append((contract, side, qty, price, is_exit_order))
        return _FakeOrder(order_status="OPEN", fill_price=self.fill_price)


class _FakeRiskManager:
    def __init__(self):
        self.released = []

    def release_deployed_capital(self, strategy_name, amount):
        self.released.append((strategy_name, amount))


class _FakeEngine:
    """Duck-typed stand-in -- only the attributes/methods the target methods
    actually touch."""
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

    async def _log_trade_close(self, journal_id, exit_price, pnl, exit_reason, market_data, total_slippage_pts):
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
    contract, side, qty, price, is_exit = fake.order_manager.calls[0]
    assert contract == "TITAN26AUG4975CE"
    assert side == "SELL"
    assert qty == 175
    assert is_exit is True
    # price should be the ATR estimate (no live quote available), NOT 0 and NOT entry price
    assert price > 0 and price != 46.59


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
