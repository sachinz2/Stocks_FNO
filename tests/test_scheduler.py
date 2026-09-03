"""
schedule_trading_jobs()'s LTP-poll / signal-cycle ordering (2026-09-03,
external review).

LTPPoller.poll() (builds this cycle's OHLC/ATR/ADX/RVOL) and
run_signal_cycle() (reads that same data to decide entries) used to be two
independent 60s IntervalTrigger jobs with no ordering guarantee -- whichever
job happened to be added to the scheduler a few milliseconds earlier got a
head start unrelated to which one should actually run first. Both are now
anchored to the same reference moment, with the signal cycle offset behind
the LTP poll by LTP_POLL_TO_SIGNAL_CYCLE_OFFSET_SECONDS.
"""
from types import SimpleNamespace

from src.core import scheduler as scheduler_module
from src.core.scheduler import (
    LTP_POLL_TO_SIGNAL_CYCLE_OFFSET_SECONDS,
    get_scheduler,
    schedule_trading_jobs,
)


def _reset_scheduler():
    """Each test gets a fresh scheduler instance -- the module keeps a
    global singleton, and leaving a previous test's jobs registered would
    make start_date comparisons meaningless."""
    scheduler_module._scheduler = None


def _fake_engine(with_poller=True):
    return SimpleNamespace(
        run_signal_cycle=lambda: None,
        on_market_open=lambda: None,
        on_market_close=lambda: None,
        send_daily_report=lambda: None,
        _check_gap_opens=lambda: None,
        _run_exit_checks_only=lambda: None,
        sync_orders=lambda: None,
        risk_manager=object(),
        _symbol_poller=SimpleNamespace(poll=lambda: None) if with_poller else None,
    )


def test_ltp_poll_and_signal_cycle_share_the_same_epoch_with_a_positive_offset():
    _reset_scheduler()
    engine = _fake_engine()
    schedule_trading_jobs(engine)
    scheduler = get_scheduler()

    ltp_job    = scheduler.get_job("ltp_poll")
    signal_job = scheduler.get_job("signal_generation")

    assert ltp_job is not None
    assert signal_job is not None

    ltp_start    = ltp_job.trigger.start_date
    signal_start = signal_job.trigger.start_date
    offset = (signal_start - ltp_start).total_seconds()

    assert abs(offset - LTP_POLL_TO_SIGNAL_CYCLE_OFFSET_SECONDS) < 0.01, (
        "signal cycle must start exactly LTP_POLL_TO_SIGNAL_CYCLE_OFFSET_SECONDS "
        "after the LTP poll, not an accident of registration order"
    )


def test_signal_cycle_still_registers_even_without_a_symbol_poller_attached():
    """Guard against over-fixing -- a missing poller (e.g. attach order
    changes upstream) must not prevent signal generation from being
    scheduled at all, just skip the (now-impossible) ltp_poll registration."""
    _reset_scheduler()
    engine = _fake_engine(with_poller=False)
    schedule_trading_jobs(engine)
    scheduler = get_scheduler()

    assert scheduler.get_job("ltp_poll") is None
    assert scheduler.get_job("signal_generation") is not None


def test_ltp_poll_interval_is_60_seconds():
    _reset_scheduler()
    engine = _fake_engine()
    schedule_trading_jobs(engine)
    scheduler = get_scheduler()

    ltp_job = scheduler.get_job("ltp_poll")
    assert ltp_job.trigger.interval.total_seconds() == 60


def test_signal_cycle_interval_is_60_seconds():
    _reset_scheduler()
    engine = _fake_engine()
    schedule_trading_jobs(engine)
    scheduler = get_scheduler()

    signal_job = scheduler.get_job("signal_generation")
    assert signal_job.trigger.interval.total_seconds() == 60
