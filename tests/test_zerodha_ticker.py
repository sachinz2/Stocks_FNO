"""
ZerodhaTicker.seconds_since_last_tick() -- the core logic behind the
tick-staleness watchdog in src/api/main.py's _kite_self_heal job.
"""
import datetime as dt
import json
import pytz
import pytest

from src.market_data.zerodha_ticker import ZerodhaTicker

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def frozen_now(monkeypatch):
    # zerodha_ticker.py does `from src.core.utils import now_ist` INSIDE each
    # method (not at module level), so there's no module-level attribute to
    # patch there -- patch the source in src.core.utils instead, which the
    # local import re-resolves at call time.
    state = {"t": IST.localize(dt.datetime(2026, 7, 31, 10, 0, 0))}

    def _now_ist():
        return state["t"]

    monkeypatch.setattr("src.core.utils.now_ist", _now_ist)
    return state


def _bare_ticker():
    """Skip __init__ (no real redis/kite connection needed for this logic)."""
    t = ZerodhaTicker.__new__(ZerodhaTicker)
    t._connected_at = None
    t._last_tick_at = None
    t._connect_attempted_at = None
    t._nifty_token = None
    return t


def test_never_connected_returns_none():
    t = _bare_ticker()
    assert t.seconds_since_last_tick() is None


# ── _connect_attempted_at fallback (2026-09-03 live incident) ──────────────
# A connection attempt that never confirms (no on_connect, no on_error --
# the exact 2026-07-31 pattern) used to leave BOTH _last_tick_at and
# _connected_at at None forever, since both are only ever set INSIDE
# callbacks that never fire in this failure mode -- seconds_since_last_tick()
# returned None the whole time, so the watchdog's staleness check was never
# true. Confirmed live 2026-09-03: this exact scenario recurred for 3.5+
# hours with the watchdog never firing.

def test_connect_attempted_but_never_confirmed_still_reports_staleness(frozen_now):
    t = _bare_ticker()
    t._connect_attempted_at = frozen_now["t"]  # start() called, connect() never confirmed
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 5, 0))  # +300s, no on_connect ever
    stale_for = t.seconds_since_last_tick()
    assert stale_for is not None, (
        "must report a real duration even when on_connect never fires -- "
        "this is exactly the failure mode the watchdog exists to catch"
    )
    assert abs(stale_for - 300) < 0.01


def test_connect_attempted_then_confirmed_prefers_the_later_connected_at(frozen_now):
    """Guard against over-fixing -- once a real connect DOES confirm, that
    (later, more accurate) timestamp must win over the attempt time."""
    t = _bare_ticker()
    t._connect_attempted_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 1, 0))  # +60s, connect confirms
    t._connected_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 1, 45))  # +45s since connect
    stale_for = t.seconds_since_last_tick()
    assert abs(stale_for - 45) < 0.01


def test_start_sets_connect_attempted_at_before_connecting(monkeypatch, frozen_now):
    t = _bare_ticker()
    t._instrument_tokens = {"RELIANCE": 100}

    # Prevent the real background thread from actually running KiteTicker.
    monkeypatch.setattr(t, "_run_ticker", lambda: None)
    import threading
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    assert t._connect_attempted_at is None
    t.start()
    assert t._connect_attempted_at == frozen_now["t"]


def test_connected_no_tick_yet_measures_from_connect_time(frozen_now):
    t = _bare_ticker()
    t._connected_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 2, 0))  # +120s
    assert abs(t.seconds_since_last_tick() - 120) < 0.01


def test_tick_arrival_switches_reference_to_last_tick(frozen_now):
    t = _bare_ticker()
    t._connected_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 2, 0))
    t._last_tick_at = frozen_now["t"]
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 2, 30))  # +30s since tick
    assert abs(t.seconds_since_last_tick() - 30) < 0.01


def test_reproduces_the_2026_07_31_dead_reactor_incident(frozen_now):
    # connected long ago, no tick ever, 2+ hours pass -> watchdog threshold trips
    t = _bare_ticker()
    t._connected_at = IST.localize(dt.datetime(2026, 7, 31, 8, 30, 22))
    frozen_now["t"] = IST.localize(dt.datetime(2026, 7, 31, 10, 23, 33))  # ~1h53m later
    stale_for = t.seconds_since_last_tick()
    TICK_STALE_THRESHOLD_SECONDS = 180
    assert stale_for > TICK_STALE_THRESHOLD_SECONDS


def test_watchdog_exits_process_on_staleness_not_inplace_reconnect():
    """
    Fixed 2026-08-07: the watchdog in api/main.py's _kite_self_heal used to
    call ticker.stop(); ticker.start() on staleness -- an in-place reconnect
    that repeated the exact same silent-failure pattern for 27+ minutes on
    2026-08-07 (KiteTicker's Twisted reactor can only run once per process;
    stop()/start() leaves a dead reactor that never processes another
    on_connect/on_error). It must call os._exit(1) instead, so
    docker-compose's `restart: unless-stopped` brings up a genuinely fresh
    process. os._exit() itself can't be exercised in a test process (it
    would kill the test runner), so this checks the source directly.
    """
    from pathlib import Path

    main_py = Path(__file__).resolve().parent.parent / "src" / "api" / "main.py"
    main_src = main_py.read_text(encoding="utf-8")

    start = main_src.index("async def _kite_self_heal")
    end = main_src.index("scheduler.add_job(\n        _kite_self_heal")
    body = main_src[start:end]

    assert "os._exit(1)" in body, "watchdog must force a full process exit on staleness"
    assert "ticker.stop()\n                    ticker.start()" not in body, (
        "old in-place reconnect (proven insufficient) must not be present"
    )


# ── set_instrument_tokens() (2026-08-20, code-review round 2) ───────────────
#
# The weekly active-universe recompute job used to mutate
# _instrument_tokens/_token_symbol directly from outside this class --
# correct bookkeeping, but subscribe()/set_mode() are only ever called from
# _on_connect(), so an already-open WebSocket connection never actually
# learned about the change. Confirmed independently by 5 of 6 code-review
# finder angles as the review's most severe finding.

class _FakeUnderlyingTicker:
    MODE_QUOTE = "quote"

    def __init__(self):
        self.subscribed = []
        self.unsubscribed = []
        self.mode_calls = []

    def subscribe(self, tokens):
        self.subscribed.append(list(tokens))

    def unsubscribe(self, tokens):
        self.unsubscribed.append(list(tokens))

    def set_mode(self, mode, tokens):
        self.mode_calls.append((mode, list(tokens)))


def test_set_instrument_tokens_updates_bookkeeping_dicts():
    t = _bare_ticker()
    t._ticker = None
    t._instrument_tokens = {}
    t._token_symbol = {}

    t.set_instrument_tokens({"RELIANCE": 100, "TCS": 200})

    assert t._instrument_tokens == {"RELIANCE": 100, "TCS": 200}
    assert t._token_symbol == {100: "RELIANCE", 200: "TCS"}


def test_set_instrument_tokens_is_a_noop_beyond_bookkeeping_when_not_started():
    # self._ticker is None before .start() -- nothing to push to yet;
    # start()'s own _on_connect() will subscribe everything fresh.
    t = _bare_ticker()
    t._ticker = None
    t._instrument_tokens = {}
    t._token_symbol = {}

    t.set_instrument_tokens({"RELIANCE": 100})  # must not raise


def test_set_instrument_tokens_pushes_new_tokens_to_the_live_connection():
    t = _bare_ticker()
    fake_ticker = _FakeUnderlyingTicker()
    t._ticker = fake_ticker
    t._instrument_tokens = {"RELIANCE": 100}
    t._token_symbol = {100: "RELIANCE"}

    t.set_instrument_tokens({"RELIANCE": 100, "TCS": 200})

    assert fake_ticker.subscribed == [[200]]
    assert fake_ticker.mode_calls == [("quote", [200])]
    assert fake_ticker.unsubscribed == []


def test_set_instrument_tokens_unsubscribes_removed_tokens_from_the_live_connection():
    t = _bare_ticker()
    fake_ticker = _FakeUnderlyingTicker()
    t._ticker = fake_ticker
    t._instrument_tokens = {"RELIANCE": 100, "THIN": 999}
    t._token_symbol = {100: "RELIANCE", 999: "THIN"}

    t.set_instrument_tokens({"RELIANCE": 100})  # THIN dropped

    assert fake_ticker.unsubscribed == [[999]]
    assert fake_ticker.subscribed == []


def test_set_instrument_tokens_no_op_on_the_wire_when_token_set_unchanged():
    t = _bare_ticker()
    fake_ticker = _FakeUnderlyingTicker()
    t._ticker = fake_ticker
    t._instrument_tokens = {"RELIANCE": 100}
    t._token_symbol = {100: "RELIANCE"}

    t.set_instrument_tokens({"RELIANCE": 100})

    assert fake_ticker.subscribed == []
    assert fake_ticker.unsubscribed == []


def test_set_instrument_tokens_survives_a_broken_live_connection():
    t = _bare_ticker()

    class _BrokenTicker(_FakeUnderlyingTicker):
        def subscribe(self, tokens):
            raise ConnectionError("websocket send failed")

    t._ticker = _BrokenTicker()
    t._instrument_tokens = {}
    t._token_symbol = {}

    t.set_instrument_tokens({"RELIANCE": 100})  # must not raise

    assert t._instrument_tokens == {"RELIANCE": 100}  # bookkeeping still updated


# ── _on_ticks() stamps a WebSocket freshness marker (2026-09-02 fix) ───────
# Live incident: ZerodhaLTPPoller's own read-modify-write of the same Redis
# key could silently revert cur_bar_volume/_last_cum_volume to a stale
# snapshot, corrupting RVOL and starving momentum_v1 of real breakout
# confirmations for 8 days. Fix: ZerodhaTicker stamps _ws_last_tick_at on
# every write; the REST poller skips its own write when that stamp is
# recent (see test_zerodha_ltp_poller.py for the poller-side tests).

class _FakeSyncRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


def test_on_ticks_stamps_ws_last_tick_at(frozen_now, monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    t = _bare_ticker()
    t._redis = _FakeSyncRedis()
    t._token_symbol = {100: "RELIANCE"}
    frozen_now["t"] = IST.localize(dt.datetime(2026, 9, 2, 12, 0, 0))

    t._on_ticks(None, [{"instrument_token": 100, "last_price": 2500.0, "volume_traded": 500000}])

    stored = json.loads(t._redis.store["tick:RELIANCE"])
    assert stored["close"] == 2500.0
    assert stored["_ws_last_tick_at"] == frozen_now["t"].isoformat()


def test_on_ticks_updates_stamp_on_every_batch(frozen_now, monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    t = _bare_ticker()
    t._redis = _FakeSyncRedis()
    t._token_symbol = {100: "RELIANCE"}

    frozen_now["t"] = IST.localize(dt.datetime(2026, 9, 2, 12, 0, 0))
    t._on_ticks(None, [{"instrument_token": 100, "last_price": 2500.0, "volume_traded": 500000}])

    frozen_now["t"] = IST.localize(dt.datetime(2026, 9, 2, 12, 0, 5))
    t._on_ticks(None, [{"instrument_token": 100, "last_price": 2501.0, "volume_traded": 500100}])

    stored = json.loads(t._redis.store["tick:RELIANCE"])
    assert stored["_ws_last_tick_at"] == frozen_now["t"].isoformat()


# ── NIFTY 50 index subscription (2026-09-15, external review global-regime) ─
#
# Tracked entirely separately from self._instrument_tokens/self._token_symbol
# -- that dict is wholesale-replaced by set_instrument_tokens() every weekly
# active-universe recompute, diffed only against its OWN previous state, so
# an entry added there would silently vanish on the next recompute.

class _FakeKiteTicker:
    MODE_QUOTE = "quote"

    def __init__(self):
        self.subscribed = []
        self.modes = []

    def subscribe(self, tokens):
        self.subscribed.extend(tokens)

    def set_mode(self, mode, tokens):
        self.modes.append((mode, list(tokens)))

    def unsubscribe(self, tokens):
        for t in tokens:
            if t in self.subscribed:
                self.subscribed.remove(t)


def test_on_ticks_routes_nifty_token_to_its_own_key_not_the_symbol_loop(frozen_now, monkeypatch):
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    t = _bare_ticker()
    t._redis = _FakeSyncRedis()
    t._token_symbol = {100: "RELIANCE"}  # NIFTY token (256265) NOT in here
    t._nifty_token = 256265
    frozen_now["t"] = IST.localize(dt.datetime(2026, 9, 15, 12, 0, 0))

    t._on_ticks(None, [
        {"instrument_token": 100, "last_price": 2500.0, "volume_traded": 500000},
        {"instrument_token": 256265, "last_price": 24800.5},  # no volume_traded -- indices don't carry it
    ])

    assert "tick:RELIANCE" in t._redis.store  # existing symbol pipeline unaffected
    nifty = json.loads(t._redis.store["market:nifty_tick"])
    assert nifty["close"] == 24800.5
    assert nifty["symbol"] == "NIFTY_50_INDEX"


def test_nifty_tick_accumulates_real_bars_not_a_single_blob(frozen_now, monkeypatch):
    # The exact defect already proven in this codebase for per-stock data
    # (see ltp_poller.py's module docstring) -- a single "today" blob bar is
    # too weak to move EMA20/50. NIFTY must accumulate real per-5-min bars
    # via the same update_intraday_bar() machinery, not one static snapshot.
    monkeypatch.setattr("src.core.utils.is_market_open", lambda: True)
    t = _bare_ticker()
    t._redis = _FakeSyncRedis()
    t._token_symbol = {}
    t._nifty_token = 256265

    frozen_now["t"] = IST.localize(dt.datetime(2026, 9, 15, 9, 15, 0))
    t._on_ticks(None, [{"instrument_token": 256265, "last_price": 24800.0}])

    frozen_now["t"] = IST.localize(dt.datetime(2026, 9, 15, 9, 21, 0))  # next 5-min bucket
    t._on_ticks(None, [{"instrument_token": 256265, "last_price": 24750.0}])

    nifty = json.loads(t._redis.store["market:nifty_tick"])
    assert len(nifty.get("bars_today", [])) == 1  # first bar finalized on rollover
    assert nifty["close"] == 24750.0


def test_on_connect_resubscribes_nifty_token_alongside_the_regular_universe():
    t = _bare_ticker()
    t._instrument_tokens = {"RELIANCE": 100}
    t._nifty_token = 256265
    fake_ws = _FakeKiteTicker()
    t._ticker = fake_ws

    t._on_connect(None, None)

    assert 100 in fake_ws.subscribed
    assert 256265 in fake_ws.subscribed


def test_on_connect_does_not_error_when_no_nifty_token_set():
    t = _bare_ticker()
    t._instrument_tokens = {"RELIANCE": 100}
    fake_ws = _FakeKiteTicker()
    t._ticker = fake_ws

    t._on_connect(None, None)  # must not raise

    assert fake_ws.subscribed == [100]


def test_set_nifty_token_pushes_to_an_already_connected_websocket():
    t = _bare_ticker()
    fake_ws = _FakeKiteTicker()
    t._ticker = fake_ws

    t.set_nifty_token(256265)

    assert t._nifty_token == 256265
    assert 256265 in fake_ws.subscribed


def test_set_nifty_token_is_a_noop_before_start():
    t = _bare_ticker()
    t._ticker = None

    t.set_nifty_token(256265)  # must not raise

    assert t._nifty_token == 256265


def test_set_nifty_token_ignores_none():
    t = _bare_ticker()
    fake_ws = _FakeKiteTicker()
    t._ticker = fake_ws

    t.set_nifty_token(None)

    assert t._nifty_token is None
    assert fake_ws.subscribed == []


def test_set_instrument_tokens_diff_never_touches_the_separately_tracked_nifty_token():
    # The exact bug this separation avoids: set_instrument_tokens() computes
    # added/removed purely from self._instrument_tokens' own before/after --
    # the NIFTY token must never appear in that dict, so a weekly
    # active-universe recompute can never accidentally unsubscribe it.
    t = _bare_ticker()
    t._instrument_tokens = {"RELIANCE": 100}
    t._token_symbol = {100: "RELIANCE"}
    t._nifty_token = 256265
    fake_ws = _FakeKiteTicker()
    t._ticker = fake_ws

    t.set_instrument_tokens({"RELIANCE": 100, "TCS": 200})  # universe grows

    assert 256265 not in fake_ws.subscribed  # never touched by this call at all
    assert t._nifty_token == 256265  # still tracked, unaffected
