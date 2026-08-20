#!/usr/bin/env python3
"""
Post-deploy invariant checker for Falcon Trader.

Unlike health-check.py (which only checks that API/DB/Redis are UP), this
script re-checks the SPECIFIC bugs found and fixed during the full-system
audit — so a future edit can't silently reintroduce one of them without a
deploy failing loudly instead of being discovered days later from logs.

Two kinds of checks:
  STATIC  — scans the checked-out source on this server for the fixed code
            pattern (or the absence of the broken one). Runs instantly,
            no running services required.
  RUNTIME — queries the live API/Redis of the just-started stack. Some are
            market-hours-aware and SKIP (not FAIL) outside 09:15-15:30 IST,
            since the underlying data legitimately doesn't exist yet.

Usage:
    python3 scripts/verify_invariants.py                  # run on the server
    python3 scripts/verify_invariants.py --repo /home/falcon/trading
    python3 scripts/verify_invariants.py --api http://localhost:8000/api/v1

Exit code 0 = all checks PASS or SKIP. Exit code 1 = at least one FAIL.
Intended to run as the final step of deploy.sh.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Tuple

# Force UTF-8 stdout regardless of the host's default locale (e.g. Windows
# consoles default to cp1252, which can't encode the status icons below).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

try:
    import requests
except ImportError:
    requests = None

IST = timezone(timedelta(hours=5, minutes=30))

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
if sys.stdout.isatty():
    _ICON = {PASS: "\033[92m✓\033[0m", FAIL: "\033[91m✗\033[0m", WARN: "\033[93m!\033[0m", SKIP: "\033[90m-\033[0m"}
else:
    _ICON = {PASS: "PASS", FAIL: "FAIL", WARN: "WARN", SKIP: "SKIP"}

Result = Tuple[str, str, str]  # (status, name, detail)


def _read(repo: Path, rel_path: str) -> str:
    return (repo / rel_path).read_text(encoding="utf-8")


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return t >= datetime.strptime("09:15", "%H:%M").time() and t <= datetime.strptime("15:30", "%H:%M").time()


# ── STATIC checks — scan the deployed source tree ───────────────────────────

def check_exit_classification_by_pnl(repo: Path) -> Result:
    name = "Exit profit/adverse classified by net_pnl, not exit_reason keywords"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "_adverse_kw" in src:
        return FAIL, name, "Found reintroduced keyword-based classification (_adverse_kw) — a DTE timeout loss will be miscounted as a profit again."
    hits = len(re.findall(r"if net_pnl < 0:\s*\n\s*(adverse_closes|to_close_adverse_c)\.append", src))
    if hits < 2:
        return FAIL, name, f"Expected net_pnl-based classification in both _check_spread_exits and _check_condor_exits, found {hits}/2."
    return PASS, name, f"net_pnl-based classification present in both exit paths ({hits}/2)."


def check_capital_allocation_keys(repo: Path) -> Result:
    name = "STRATEGY_CAPITAL_ALLOCATION keys match runtime instance ids"
    src = _read(repo, "src/core/constants.py")
    m = re.search(r"STRATEGY_CAPITAL_ALLOCATION\s*=\s*\{([^}]*)\}", src, re.S)
    if not m:
        return FAIL, name, "STRATEGY_CAPITAL_ALLOCATION dict not found."
    body = m.group(1)
    expected = ["ema_crossover_v1", "credit_spread_v1", "iron_condor_v1"]
    missing = [k for k in expected if f'"{k}"' not in body]
    if missing:
        return FAIL, name, f"Missing/renamed keys: {missing}. If these don't match the instance_id StrategyRegistry.load_strategy() uses, the budget check silently never fires."
    return PASS, name, "All three lowercase _v1 instance-id keys present."


def check_ema_state_is_per_symbol(repo: Path) -> Result:
    name = "EMA Crossover state is keyed per-symbol, not shared across its pool"
    src = _read(repo, "src/strategies/ema_crossover.py")
    if re.search(r"self\.prev_fast_ema:\s*Optional\[float\]\s*=\s*None", src):
        return FAIL, name, "Found flat scalar prev_fast_ema again — state will leak between the 5 pool symbols within a cycle."
    if not re.search(r"self\.prev_fast_ema:\s*Dict\[str,\s*float\]\s*=\s*\{\}", src):
        return WARN, name, "Could not confirm the per-symbol Dict[str, float] declaration — check manually."
    if "data.get(\"symbol\"" not in src:
        return FAIL, name, "generate_signal() no longer reads symbol from the tick data — per-symbol keying can't work without it."
    return PASS, name, "prev_fast_ema/prev_slow_ema/_pending_* are per-symbol dicts, generate_signal reads symbol."


def check_eod_notification_guard(repo: Path) -> Result:
    name = "EOD/expiry square-off notification only fires once per day"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "_eod_notified_today" not in src:
        return FAIL, name, "_eod_notified_today flag not found — is_square_off_time() is true for the whole 15:20-15:30 window and _square_off_all() runs every cycle in it, so without this the notification spams every minute."
    guards = len(re.findall(r"and not self\._eod_notified_today", src))
    if guards < 2:
        return FAIL, name, f"Expected the guard on both the expiry-day and normal-day notify calls, found {guards}/2."
    if "self._eod_notified_today = False" not in src:
        return FAIL, name, "Flag is never reset — would stay stuck 'already notified' forever after the first trading day."
    return PASS, name, "Guard present on both notify paths and reset at market open."


def check_dte_rollover_exists(repo: Path) -> Result:
    name = "Fresh credit-spread/iron-condor entries roll to next expiry instead of going dark"
    utils_src = _read(repo, "src/core/utils.py")
    if "def get_entry_expiry" not in utils_src:
        return FAIL, name, "get_entry_expiry() missing from core/utils.py."
    engine_src = _read(repo, "src/live_trading/live_trading_engine.py")
    hits = len(re.findall(r"expiry\s*=\s*get_entry_expiry\(_ENTRY_MIN_DTE\)", engine_src))
    if hits < 2:
        return FAIL, name, f"Expected the rollover call in both _process_credit_spread and _process_iron_condor, found {hits}/2."
    return PASS, name, f"get_entry_expiry() defined and wired into both entry paths ({hits}/2)."


def check_regime_uses_real_market_data(repo: Path) -> Result:
    name = "Regime detector reads live market-wide stats, not the dead NIFTY50 tick"
    src = _read(repo, "src/market_data/regime_detector.py")
    if "tick:NIFTY50" in src or "REDIS_NIFTY_TICK" in src:
        return FAIL, name, "Still references the NIFTY50 tick key that nothing ever writes — regime will silently default to flat/quiet again."
    if "REDIS_TREND_STATS_KEY" not in src or "market:trend_stats" not in src:
        return FAIL, name, "market:trend_stats key not referenced — regime detector has no live data source."
    poller_src = _read(repo, "src/market_data/ltp_poller.py")
    if "market:trend_stats" not in poller_src or "FIVE_MIN_ATR_DAILY_SCALE" not in poller_src:
        return FAIL, name, "LTPPoller no longer publishes market:trend_stats with the daily-ATR scale applied."
    return PASS, name, "regime_detector reads market:trend_stats; LTPPoller publishes it with daily-ATR scaling."


def check_kill_switch_has_reset_path(repo: Path) -> Result:
    name = "Kill switch has a reset path outside of restarting the whole server"
    rm_src = _read(repo, "src/risk/risk_manager.py")
    if "def deactivate_kill_switch" not in rm_src:
        return FAIL, name, "RiskManager.deactivate_kill_switch() missing."
    admin_src = _read(repo, "src/api/routers/admin_router.py")
    if "kill-switch/reset" not in admin_src and "kill_switch/reset" not in admin_src:
        return FAIL, name, "No admin API endpoint to reset the kill switch."
    dash_src = _read(repo, "src/dashboard/app.py")
    if "kill-switch" not in dash_src:
        return WARN, name, "Backend reset endpoint exists but no dashboard control found — only reachable via raw API calls."
    return PASS, name, "Backend reset endpoint + dashboard control both present."


def check_capital_released_on_ema_exit(repo: Path) -> Result:
    name = "EMA Crossover exits release their deployed capital"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    hits = len(re.findall(r"release_deployed_capital\(\s*\n?\s*(info|jrnl|_jrnl_info)\.get\(\"strategy_name\"", src))
    if hits < 3:
        return FAIL, name, f"Expected release_deployed_capital() at all 3 single-leg exit points (normal exit, reversal exit, EOD/expiry square-off), found {hits}/3 — budget would only ever grow and eventually block entries."
    return PASS, name, f"release_deployed_capital() present at {hits}/3 single-leg exit points."


def check_expiry_day_journal_logged(repo: Path) -> Result:
    name = "Expiry-day force-close logs trade_journal exit for spreads/condors"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "_exit_prices" not in src:
        return FAIL, name, "_exit_prices capture missing from _square_off_all — expiry-day closes for spreads/condors would never get an exit_time/pnl in trade_journal."
    hits = len(re.findall(r'exit_reason=f"Expiry day force-close', src))
    if hits < 2:
        return FAIL, name, f"Expected the expiry-day journal-close call for both spreads and condors, found {hits}/2."
    return PASS, name, "Expiry-day journal close logged for both structure types."


def check_spread_condor_exit_uses_real_fill(repo: Path) -> Result:
    name = "Credit-spread/condor EXITS price off the real fill, not the pre-trade quote"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if 'net_pnl = (\n                (spread["short_premium"] - cur_short)' in src:
        return FAIL, name, "Found the old quote-based net_pnl calc reintroduced in _check_spread_exits."
    # Fixed 2026-08-13: the fill-vs-quote pattern was consolidated from ~13
    # inlined `float(getattr(order, "fill_price", None) or fallback)` copies
    # into one shared self._real_fill(order, fallback) helper -- check for
    # that instead of the old inline pattern.
    if "def _real_fill(order, fallback: float) -> float:" not in src:
        return FAIL, name, "_real_fill() shared helper missing."
    real_fill_hits = len(re.findall(r"self\._real_fill\(\w+,\s*cur_\w+\)", src))
    if real_fill_hits < 6:  # 2 spread legs (short/long) + 4 condor legs
        return FAIL, name, f"Expected 6 self._real_fill(...) exit-price reads total (2 spread + 4 condor legs), found {real_fill_hits}."
    return PASS, name, f"Exit pricing reads real fill_price at {real_fill_hits} leg-exit sites (spread + condor) via the shared helper."


def check_spread_condor_entry_uses_real_fill(repo: Path) -> Result:
    name = "Credit-spread/condor ENTRIES record the real fill, not the pre-trade quote"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "short_fill = self._real_fill(short_order, short_p)" not in src:
        return FAIL, name, "_process_credit_spread no longer reads short_order.fill_price -- short_premium/entry_price would silently revert to the pre-trade quote."
    if "_fills = {c: self._real_fill(o, p) for c, _s, p, _l, o in placed}" not in src:
        return FAIL, name, "_process_iron_condor no longer builds its per-leg fill map from the placed orders' fill_price."
    if '"short_premium":  short_fill,' not in src:
        return FAIL, name, "_active_spreads no longer stores the real fill as short_premium -- live SL/profit-target checks would use the stale quote again."
    return PASS, name, "Both credit_spread and iron_condor entries store real fill_price-based premiums."


def check_cross_strategy_collision_guard(repo: Path) -> Result:
    name = "Single-leg and spread/condor strategies can't both trade the same underlying at once"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "Cross-strategy contract-collision guard" not in src:
        return FAIL, name, "Guard comment/marker missing -- likely removed."
    # Fixed 2026-08-13: consolidated from 3 independent inline copies into
    # two shared helper methods -- check for those instead of the old
    # inline patterns, and confirm both entry directions actually call them.
    if "def _has_active_multi_leg_structure(self, symbol: str) -> bool:" not in src:
        return FAIL, name, "_has_active_multi_leg_structure() helper missing."
    if "def _has_open_single_leg_position(self, symbol: str) -> bool:" not in src:
        return FAIL, name, "_has_open_single_leg_position() helper missing."
    if src.count("if self._has_open_single_leg_position(symbol):") < 2:  # credit_spread + iron_condor entry
        return FAIL, name, "Spread/condor entry no longer checks for an existing open single-leg position on the same underlying (both _process_credit_spread and _process_iron_condor need this)."
    if "if self._has_active_multi_leg_structure(symbol):" not in src:
        return FAIL, name, "Single-leg entry (_process_signal) no longer checks for an existing active spread/condor on the same underlying."
    # Fixed 2026-08-14: the guard above only covered the engine's own
    # automated entry paths -- a manual order placed via orders_router.py
    # called OrderManager.place_order() directly, bypassing it entirely.
    orders_src = _read(repo, "src/api/routers/orders_router.py")
    if "_has_active_multi_leg_structure(underlying)" not in orders_src:
        return FAIL, name, "orders_router.py's place_order() no longer checks _has_active_multi_leg_structure() before placing a manual order -- manual orders can again collide with an active spread/condor on the same underlying."
    if "_get_underlying_from_contract(order_request.symbol)" not in orders_src:
        return FAIL, name, "orders_router.py's place_order() no longer resolves the underlying from the manual order's contract symbol before the collision check."
    return PASS, name, "Guarded both directions via shared helpers: spread/condor vs single-leg, plus manual orders through orders_router.py."


def check_risk_limits_wired_from_settings(repo: Path) -> Result:
    name = "Exposure/daily-loss caps come from settings (.env), not a disconnected hardcoded literal"
    rm_src = _read(repo, "src/risk/risk_manager.py")
    if "max_exposure_per_trade_pct: float = 0.30" not in rm_src:
        return FAIL, name, "RiskManager.__init__ no longer takes max_exposure_per_trade_pct as a constructor arg -- may have reverted to a hardcoded literal, silently disconnecting settings.MAX_EXPOSURE_PCT again."
    if "max_daily_loss_pct: float = 0.05" not in rm_src:
        return FAIL, name, "RiskManager.__init__ no longer takes max_daily_loss_pct as a constructor arg."
    main_src = _read(repo, "src/api/main.py")
    if "max_exposure_per_trade_pct=settings.MAX_EXPOSURE_PCT" not in main_src:
        return FAIL, name, "main.py no longer passes settings.MAX_EXPOSURE_PCT through to RiskManager."
    if "max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT" not in main_src:
        return FAIL, name, "main.py no longer passes settings.MAX_DAILY_LOSS_PCT through to RiskManager."
    # Fixed 2026-08-13: orders_router.py used to build its OWN separate
    # RiskManager (never wired to capital-period compounding, tracking
    # independent exposure/daily-loss state from the live engine's) --
    # now it must reuse the live engine's order_manager/risk_manager
    # instead of constructing anything of its own.
    orders_src = _read(repo, "src/api/routers/orders_router.py")
    if "RiskManager(" in orders_src or "PaperBroker(" in orders_src:
        return FAIL, name, "orders_router.py constructs its own RiskManager/PaperBroker again -- manual orders would stop sharing the live engine's real risk state."
    if "engine.order_manager" not in orders_src:
        return FAIL, name, "orders_router.py no longer reuses request.app.state.trading_engine.order_manager."
    return PASS, name, "main.py wires exposure/daily-loss caps from settings; orders_router.py reuses the live engine's RiskManager instead of its own."


def check_capital_period_compounding_drives_live_limits(repo: Path) -> Result:
    name = "Expiry-to-expiry capital compounding updates RiskManager's LIVE limits, not just reporting"
    rm_src = _read(repo, "src/risk/risk_manager.py")
    if "def set_capital" not in rm_src:
        return FAIL, name, "RiskManager.set_capital() missing."
    cp_src = _read(repo, "src/portfolio/capital_periods.py")
    if "risk_manager.set_capital(float(active.starting_capital))" not in cp_src:
        return FAIL, name, "rollover_if_needed() no longer calls risk_manager.set_capital() on rollover -- capital periods would go back to reporting-only."
    if "kwargs={\"risk_manager\": engine.risk_manager}" not in _read(repo, "src/core/scheduler.py"):
        return FAIL, name, "Daily rollover job no longer passes the live engine's risk_manager through -- set_capital() would never actually be called."
    return PASS, name, "set_capital() exists, called on rollover, wired to the live risk_manager via the scheduled job."


def check_zerodha_sync_gated_to_live_mode(repo: Path) -> Result:
    name = "Daily Zerodha<->DB sync only runs in TradingMode.LIVE, not whenever a kite client exists"
    src = _read(repo, "src/api/main.py")
    if "async def _run_daily_zerodha_sync" not in src:
        return FAIL, name, "_run_daily_zerodha_sync job missing from main.py."
    idx = src.index("async def _run_daily_zerodha_sync")
    job_body = src[idx:idx + 400]
    if "if mode != TradingMode.LIVE:" not in job_body:
        return FAIL, name, "Job no longer gates on TradingMode.LIVE -- a real, authenticated kite client is attached even in paper mode (for market data), so without this gate the job would pull the real Zerodha account's orders into the paper-trading DB every morning."
    return PASS, name, "Job explicitly gated to TradingMode.LIVE."


def check_lot_size_and_contract_resolution_fail_closed(repo: Path) -> Result:
    name = "Missing lot-size/contract metadata blocks the trade, doesn't fall back to a computed guess"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "async def _get_lot_size(self, symbol: str) -> Optional[int]:" not in src:
        return FAIL, name, "_get_lot_size() no longer returns Optional[int] -- may have reverted to always returning an int (silently falling back to the static FNO_LOT_SIZES table on a cache miss)."
    lot_idx = src.index("async def _get_lot_size")
    lot_body = src[lot_idx:lot_idx + 1200]
    if "return get_lot_size(symbol)" in lot_body:
        return FAIL, name, "_get_lot_size() still falls back to the static table -- that table's own comment documents 36/39 symbols once found wrong there, masked only by this same cache."
    if "-> Optional[Tuple[float, str]]:" not in src or "async def _resolve_contract(" not in src:
        return FAIL, name, "_resolve_contract() no longer returns Optional[...] -- may have reverted to always returning a (possibly computed-guess) contract."
    resolve_idx = src.index("async def _resolve_contract(")
    resolve_body = src[resolve_idx:resolve_idx + 1800]
    if "return candidate_strike, build_option_symbol(symbol, candidate_strike, option_type, expiry)" in resolve_body:
        return FAIL, name, "_resolve_contract() still falls back to the computed build_option_symbol() guess on a cache miss."
    if "if not lot_size:" not in src:
        return FAIL, name, "No entry-path caller checks _get_lot_size()'s result for None before using it."
    if src.count("if resolved is None:") + src.count("if _r is None:") < 3:
        return FAIL, name, "Not all 3 entry paths (single-leg, credit-spread, iron-condor) check _resolve_contract()'s result for None."
    # Fixed 2026-08-14: the checks above only count how many guard patterns
    # EXIST, not whether every _resolve_contract() call site actually has
    # one -- that let _get_live_sigma()'s two call sites (used by both
    # credit-spread and iron-condor entries for delta-based strike
    # selection) go unguarded for a full review cycle, silently degrading
    # to ATR sigma instead of the loud warning every other site gets.
    # Explicitly walk every call site this time.
    unguarded = []
    idx = 0
    while True:
        idx = src.find("await self._resolve_contract(", idx)
        if idx == -1:
            break
        # A guard is anywhere in the ~200 chars before this call (the "if
        # ... is None" check for the PRIOR leg) or ~250 after (this leg's
        # own check, or the loop-based iron-condor pattern's "if _r is
        # None" a few lines below the call inside its for-loop).
        window = src[max(0, idx - 250):idx + 350]
        if "is None" not in window:
            unguarded.append(idx)
        idx += 1
    if unguarded:
        return FAIL, name, f"Found {len(unguarded)} _resolve_contract() call site(s) with no None-check nearby (offsets {unguarded}) -- a cache miss there would either crash or silently degrade instead of failing closed/loud."
    return PASS, name, "_get_lot_size()/_resolve_contract() fail closed on missing metadata; every call site checked and handles None."


def check_stale_option_price_resolved(repo: Path) -> Result:
    name = "Option prices trust last_price only if traded today, else fall back to bid/ask"
    oc_src = _read(repo, "src/market_data/option_chain.py")
    if "def resolve_reliable_option_price" not in oc_src:
        return FAIL, name, "resolve_reliable_option_price() missing from option_chain.py."
    if "last_trade_time.date() == now_ist().date()" not in oc_src and "last_trade_time.date()" not in oc_src:
        return FAIL, name, "resolve_reliable_option_price() no longer checks last_trade_time against today -- a stale last_price (e.g. weeks old) could be trusted again."
    poller_src = _read(repo, "src/market_data/zerodha_ltp_poller.py")
    if "resolve_reliable_option_price" not in poller_src:
        return FAIL, name, "ZerodhaLTPPoller no longer routes option prices through resolve_reliable_option_price()."
    return PASS, name, "resolve_reliable_option_price() present and wired into the LTP poller."


def check_auth_self_heal_actively_retries(repo: Path) -> Result:
    name = "Kite auth self-heal actively retries login, not just waits for the 08:30 job"
    utils_src = _read(repo, "src/core/utils.py")
    if "def is_auth_retry_window" not in utils_src or "def should_retry_auth" not in utils_src:
        return FAIL, name, "is_auth_retry_window()/should_retry_auth() missing from core/utils.py."
    main_src = _read(repo, "src/api/main.py")
    if "run_daily_auth" not in main_src or "_kite_self_heal" not in main_src:
        return FAIL, name, "_kite_self_heal no longer references run_daily_auth -- a missed 08:30 job would leave the system with zero live data until the next day again."
    return PASS, name, "Self-heal has an active-retry path, not just a wait for the next scheduled auth."


def check_margin_and_broker_position_failures_fail_closed(repo: Path) -> Result:
    name = "Margin API failure and broker-position-fetch failure block new entries, don't fail open"
    src = _read(repo, "src/live_trading/live_trading_engine.py")
    if "Blocking entry (fail-closed)" not in src:
        return FAIL, name, "_check_available_margin()'s live-mode API-error path no longer fails closed -- a kite.margins() error could once again silently allow an entry with unverified margin."
    if "return False" not in src[src.index("Blocking entry (fail-closed)"):src.index("Blocking entry (fail-closed)") + 100]:
        return FAIL, name, "_check_available_margin()'s fail-closed log line is no longer immediately followed by `return False`."
    if "_broker_position_state_known" not in src:
        return FAIL, name, "_broker_position_state_known flag missing -- _safe_get_positions() no longer distinguishes a broker API failure from a confirmed-zero position list."
    if "if not self._broker_position_state_known:" not in src:
        return FAIL, name, "run_signal_cycle() no longer checks _broker_position_state_known before the entry loop -- a broker fetch failure would silently be treated as zero positions again."
    return PASS, name, "Margin API errors and broker-position-fetch failures both block new entries instead of assuming a safe default."


def check_single_leg_dte_window_covers_post_roll_dte(repo: Path) -> Result:
    name = "ema_crossover_v1/momentum_v1 max_dte covers the DTE right after a monthly roll"
    ema_src = _read(repo, "src/strategies/ema_crossover.py")
    mom_src = _read(repo, "src/strategies/momentum.py")
    # Fixed 2026-08-20: max_dte=25 left a structural monthly dead zone --
    # get_near_month_expiry() only rolls at DTE<7, so a freshly-rolled
    # contract can be up to DTE=41, well above the old max_dte=25. Confirmed
    # live: zero single-leg orders 2026-07-25..08-05 and 2026-08-17..08-20.
    for label, src in (("ema_crossover.py", ema_src), ("momentum.py", mom_src)):
        idx = src.find('self.max_dte: int = self.parameters.get("max_dte",')
        if idx == -1:
            return FAIL, name, f"{label} no longer sets max_dte via self.parameters.get(...) -- can't verify its default."
        window = src[idx:idx + 60]
        m = re.search(r'"max_dte",\s*(\d+)\)', window)
        if not m or int(m.group(1)) < 41:
            return FAIL, name, f"{label}'s max_dte default is below 41 -- reintroduces the monthly post-roll dead zone that blocked all single-leg entries 2026-08-17..08-20."
    return PASS, name, "Both single-leg strategies' max_dte covers the worst-case post-roll DTE (41)."


STATIC_CHECKS: List[Callable[[Path], Result]] = [
    check_exit_classification_by_pnl,
    check_capital_allocation_keys,
    check_ema_state_is_per_symbol,
    check_eod_notification_guard,
    check_dte_rollover_exists,
    check_regime_uses_real_market_data,
    check_kill_switch_has_reset_path,
    check_capital_released_on_ema_exit,
    check_expiry_day_journal_logged,
    check_spread_condor_exit_uses_real_fill,
    check_spread_condor_entry_uses_real_fill,
    check_cross_strategy_collision_guard,
    check_risk_limits_wired_from_settings,
    check_capital_period_compounding_drives_live_limits,
    check_zerodha_sync_gated_to_live_mode,
    check_lot_size_and_contract_resolution_fail_closed,
    check_stale_option_price_resolved,
    check_auth_self_heal_actively_retries,
    check_margin_and_broker_position_failures_fail_closed,
    check_single_leg_dte_window_covers_post_roll_dte,
]


# ── RUNTIME checks — query the live, just-started stack ─────────────────────

def _get(api_base: str, path: str, timeout: float = 5.0):
    r = requests.get(f"{api_base}/{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def check_api_stack_up(api_base: str) -> Result:
    name = "API / DB / Redis are up"
    try:
        data = _get(api_base, "health")
    except Exception as e:
        return FAIL, name, f"Health endpoint unreachable: {e}"
    bad = [k for k in ("status", "database", "redis") if not str(data.get(k, "")).startswith(("UP", "ok", "OK"))]
    if bad:
        return FAIL, name, f"Not healthy: {data}"
    return PASS, name, f"status={data.get('status')} database={data.get('database')} redis={data.get('redis')}"


def check_three_strategies_registered(api_base: str) -> Result:
    name = "All 3 strategies registered (ema_crossover_v1, credit_spread_v1, iron_condor_v1)"
    try:
        data = _get(api_base, "strategies")
    except Exception as e:
        return FAIL, name, f"/strategies unreachable: {e}"
    ids = {s.get("id") for s in data}
    expected = {"ema_crossover_v1", "credit_spread_v1", "iron_condor_v1"}
    missing = expected - ids
    if missing:
        return FAIL, name, f"Missing: {missing}. Engine may not have finished starting, or a strategy failed to load."
    return PASS, name, f"Registered: {sorted(ids)}"


def check_kill_switch_endpoint(api_base: str) -> Result:
    name = "Kill-switch status endpoint reachable and well-formed"
    try:
        data = _get(api_base, "admin/kill-switch")
    except Exception as e:
        return FAIL, name, f"/admin/kill-switch unreachable: {e}"
    if "active" not in data:
        return FAIL, name, f"Response missing 'active' field: {data}"
    if data["active"]:
        return WARN, name, f"Kill switch is currently ACTIVE — reason: {data.get('reason')}. Not a deploy failure, but new entries are blocked until reset."
    return PASS, name, "Reachable, inactive."


def check_regime_data_is_live(api_base: str) -> Result:
    name = "Regime classification is using live market data, not frozen defaults"
    if not _is_market_hours():
        return SKIP, name, "Outside market hours (09:15-15:30 IST Mon-Fri) — no live tick data expected."
    try:
        data = _get(api_base, "analytics/market-regime")
    except Exception as e:
        return FAIL, name, f"/analytics/market-regime unreachable: {e}"
    atr = data.get("market_atr_pct")
    ema_spread = data.get("market_ema_spread")
    if atr is None:
        return WARN, name, f"No regime data yet this run: {data}"
    # The old bug's exact frozen fallback values were 1.0 / 0.15 — flag an exact
    # match as suspicious (real data essentially never lands on these precisely).
    if atr == 1.0 and ema_spread == 0.15:
        return WARN, name, "ATR%=1.00 EMA_spread%=0.15 exactly — matches the old hardcoded fallback. Possibly coincidence, but worth a manual look if it persists past the next cycle."
    return PASS, name, f"regime={data.get('regime')} atr%={atr} ema_spread%={ema_spread}"


def check_no_duplicate_eod_notifications(repo: Path) -> Result:
    """
    Flags rapid-fire repeats (the actual bug pattern: same notification re-sent
    every cycle for the whole 15:20-15:30 window) that happened SINCE THE
    CURRENT PROCESS STARTED — not anywhere in the log file's whole history.
    The log persists across restarts, so a fix deployed mid-window leaves
    genuine pre-fix spam sitting earlier in the same file; scoping to "since
    the last engine start" is what makes this a check of the code currently
    running, rather than a permanent trip-wire on that historical incident.
    """
    name = "No rapid-fire duplicate EOD notifications since this deploy"
    log_path = repo / "logs" / "falcon.log"
    if not log_path.exists():
        return SKIP, name, "falcon.log not found at expected path."
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    last_start: datetime = None
    eod_timestamps: List[datetime] = []
    try:
        with log_path.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = ts_re.match(line)
                if not m:
                    continue
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                if "Trading engine STARTED" in line:
                    last_start = ts
                    eod_timestamps = []  # only care about sends after the latest start
                elif "Email sent: EOD POSITION UPDATE" in line:
                    eod_timestamps.append(ts)
    except Exception as e:
        return WARN, name, f"Could not read log: {e}"
    if last_start is None:
        return WARN, name, "Could not find a 'Trading engine STARTED' line to scope the check to."
    for a, b in zip(eod_timestamps, eod_timestamps[1:]):
        if (b - a) < timedelta(minutes=15):
            return FAIL, name, f"Two sends only {int((b - a).total_seconds())}s apart since the last start ({a} -> {b}) — the once-per-day guard may not be working."
    return PASS, name, f"{len(eod_timestamps)} EOD notification(s) since the engine last started ({last_start}), none within 15 min of each other."


RUNTIME_CHECKS: List[Callable[[str], Result]] = [
    check_api_stack_up,
    check_three_strategies_registered,
    check_kill_switch_endpoint,
    check_regime_data_is_live,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="Path to the checked-out repo (default: current directory)")
    parser.add_argument("--api", default="http://localhost:8000/api/v1", help="API base URL")
    parser.add_argument("--skip-runtime", action="store_true", help="Only run static source checks")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    results: List[Result] = []

    print(f"Falcon Trader — invariant check\nRepo: {repo}\nTime: {datetime.now(IST).isoformat()}\n")

    print("── Static (source) checks ──────────────────────────────")
    for check in STATIC_CHECKS:
        try:
            status, name, detail = check(repo)
        except FileNotFoundError as e:
            status, name, detail = FAIL, check.__name__, f"File not found: {e}"
        except Exception as e:
            status, name, detail = FAIL, check.__name__, f"Check crashed: {e}"
        results.append((status, name, detail))
        print(f"  {_ICON[status]} [{status}] {name}\n        {detail}")

    status, name, detail = check_no_duplicate_eod_notifications(repo)
    results.append((status, name, detail))
    print(f"  {_ICON[status]} [{status}] {name}\n        {detail}")

    if not args.skip_runtime:
        print("\n── Runtime (live stack) checks ──────────────────────────")
        if requests is None:
            print("  ! 'requests' not installed — skipping runtime checks. pip install requests")
        else:
            for check in RUNTIME_CHECKS:
                try:
                    status, name, detail = check(args.api)
                except Exception as e:
                    status, name, detail = FAIL, check.__name__, f"Check crashed: {e}"
                results.append((status, name, detail))
                print(f"  {_ICON[status]} [{status}] {name}\n        {detail}")

    n_pass = sum(1 for s, _, _ in results if s == PASS)
    n_warn = sum(1 for s, _, _ in results if s == WARN)
    n_skip = sum(1 for s, _, _ in results if s == SKIP)
    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{n_pass} passed, {n_warn} warnings, {n_skip} skipped, {n_fail} failed.")

    if n_fail:
        print("\nFAILED — a previously-fixed bug pattern may have been reintroduced, or the deploy didn't start cleanly. See details above.")
        return 1
    if n_warn:
        print("\nPassed with warnings — review above, no action required unless something looks wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
