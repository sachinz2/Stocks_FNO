import os
import time
import json
import streamlit as st
import requests
import pandas as pd
from datetime import datetime, date
from urllib.parse import quote_plus

from src.core.constants import FNO_SYMBOLS

st.set_page_config(page_title="Falcon Quant Platform", layout="wide", page_icon="🦅")

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000/api/v1")
_DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "300000"))
# Fixed 2026-08-21 (deep review): was a hardcoded 0.05 literal, disconnected
# from settings.MAX_DAILY_LOSS_PCT (.env) which RiskManager actually reads --
# tightening the real limit via .env silently left this dashboard figure
# showing the stale default, understating how close the account actually is
# to the real kill-switch threshold. Same env var, same pattern as
# INITIAL_CAPITAL above.
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", "0.05"))
MAX_OPEN_POSITIONS = 25     # matches RiskManager (intentionally not .env-driven -- see risk_manager.py's own comment on why MAX_OPEN_POSITIONS isn't wired to this)

# Shared secret required by every mutating API call (admin_router,
# strategies/activate|deactivate, orders POST/DELETE, signals/generate) --
# same env var the API side reads via require_admin_token() in
# src/api/services/auth.py, so a single ADMIN_API_TOKEN in .env configures
# both. Empty here just means every mutating call gets a 403 from the API
# (fail closed), same as an unset ADMIN_API_TOKEN disables the API side.
_ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
_ADMIN_AUTH_HEADERS = {"X-Admin-Token": _ADMIN_API_TOKEN}

# Read-only /logs/recent token — same var the API's logs_router reads.
_LOGS_API_TOKEN = os.environ.get("LOGS_API_TOKEN", "")


def check_password():
    def password_entered():
        if st.session_state["password"] == _DASHBOARD_PASSWORD:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.text_input("Password", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.text_input("Password", type="password", on_change=password_entered, key="password")
        st.error("Password incorrect")
        return False
    return True


if not check_password():
    st.stop()

# --- Navigation ---
st.sidebar.title("Falcon Quant")
st.sidebar.markdown("---")
page = st.sidebar.radio(
    "Navigate",
    ["Home", "Positions", "Orders & Trades", "Strategies", "Risk & PnL", "Analytics", "System Health", "Admin"],
)
st.sidebar.markdown("---")

# Auto-refresh toggle
auto_refresh = st.sidebar.toggle("Auto-refresh (60 s)", value=False)
if st.sidebar.button("Refresh Now"):
    st.rerun()


# --- API helpers ---
def fetch(endpoint: str):
    try:
        r = requests.get(f"{API_BASE_URL}/{endpoint}", timeout=5)
        if r.status_code == 200:
            return r.json()
        st.warning(f"API returned {r.status_code} for /{endpoint}")
    except Exception as e:
        st.warning(f"API not reachable ({endpoint}): {e}")
    return None


def post(endpoint: str, timeout: int = 10):
    try:
        r = requests.post(f"{API_BASE_URL}/{endpoint}", headers=_ADMIN_AUTH_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        st.error(f"API returned {r.status_code}: {r.text}")
    except Exception as e:
        st.error(f"API not reachable ({endpoint}): {e}")
    return None


def fmt_inr(val: float) -> str:
    return f"₹{val:,.2f}"


def pnl_color(val: float) -> str:
    if val > 0:
        return "green"
    if val < 0:
        return "red"
    return "gray"


def _estimate_capital_deployed(positions: list) -> float:
    """
    Fixed 2026-08-21 (deep review): summing abs(quantity) * avg_price
    across EVERY leg double-counts credit spreads/iron condors -- it adds
    both the short leg's premium RECEIVED and the long leg's premium PAID
    as if both were capital blocked, when the real risk-based figure is
    (wing width - net credit) * lot_size, overstating utilization for every
    multi-leg structure. This positions list doesn't carry strike prices
    (only symbol/quantity/avg_price/group_id/structure_type), so the exact
    width-based figure the live engine itself uses isn't computable here
    without re-parsing strikes out of contract symbols -- a real gap, not
    fully closed by this fix. What IS fixed: grouped legs (group_id set,
    i.e. spread/condor) are now netted by their SIGNED quantity (short legs
    carry a negative quantity, long legs positive -- see
    positions_router.py's _positions_from_engine_uncached) instead of
    summed as unsigned notional, so a paired credit spread/condor no longer
    shows roughly double its real net premium exposure. Standalone
    (group_id=None) single-leg positions are unaffected -- abs(qty)*avg_price
    is already the real premium paid for those.
    """
    ungrouped_total = sum(
        abs(p.get("quantity", 0)) * p.get("avg_price", 0)
        for p in positions if not p.get("group_id")
    )
    grouped: dict = {}
    for p in positions:
        gid = p.get("group_id")
        if gid:
            grouped.setdefault(gid, 0.0)
            grouped[gid] += p.get("quantity", 0) * p.get("avg_price", 0)
    grouped_total = sum(abs(v) for v in grouped.values())
    return ungrouped_total + grouped_total


# ── Pages ─────────────────────────────────────────────────────────────────────

if page == "Home":
    st.title("Dashboard Overview")

    positions  = fetch("positions") or []
    orders     = fetch("orders") or []
    health     = fetch("health") or {}
    pnl_data   = fetch("analytics/pnl-summary") or {}

    net_pnl = pnl_data.get("total_pnl", 0)
    open_positions = len([p for p in positions if p.get("quantity", 0) != 0])
    capital_deployed = _estimate_capital_deployed(positions)
    capital_pct = (capital_deployed / INITIAL_CAPITAL) * 100 if capital_deployed else 0

    from src.core.utils import now_ist
    today_str = now_ist().date().isoformat()
    orders_today = [o for o in orders if (o.get("created_at") or "").startswith(today_str)]
    open_orders = [o for o in orders if o.get("status") in ("PENDING", "OPEN")]

    # Live virtual cash balance (PaperBroker.balance) — decreases on every BUY,
    # increases on every SELL, and gates whether a new order can be placed.
    # None in live (Zerodha) mode, where real broker funds/margin apply instead.
    available_cash = (health or {}).get("available_cash")

    col1, col2, col3, col4, col5 = st.columns(5)
    pnl_delta = f"{(net_pnl / INITIAL_CAPITAL) * 100:.2f}%" if INITIAL_CAPITAL else "0%"
    col1.metric("Net PnL", fmt_inr(net_pnl), pnl_delta)
    col2.metric("Open Positions", str(open_positions), f"Max {MAX_OPEN_POSITIONS}")
    col3.metric("Orders Today", str(len(orders_today)), f"{len(open_orders)} open")
    col4.metric("Capital Deployed", fmt_inr(capital_deployed), f"{capital_pct:.1f}%")
    if available_cash is not None:
        cash_pct = (available_cash / INITIAL_CAPITAL) * 100 if INITIAL_CAPITAL else 0
        col5.metric("Available Cash", fmt_inr(available_cash), f"{cash_pct:.1f}% of start")
    else:
        col5.metric("Available Cash", "—", "live mode")

    st.markdown("---")

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Recent Orders")
        if orders:
            recent = sorted(orders, key=lambda x: x.get("created_at") or "", reverse=True)[:10]
            df = pd.DataFrame(recent)
            show = [c for c in ["id", "symbol", "side", "quantity", "price", "status", "created_at"] if c in df.columns]
            df = df[show]
            df.columns = [c.replace("_", " ").title() for c in show]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No orders yet.")

    with col_right:
        st.subheader("Open Positions")
        if positions:
            df = pd.DataFrame(positions)
            show = [c for c in ["symbol", "quantity", "avg_price", "unrealized_pnl"] if c in df.columns]
            df = df[show]
            df.columns = ["Symbol", "Qty", "Avg Price", "Unrealized PnL"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No open positions.")

    st.markdown("---")
    api_status = health.get("status", "UNKNOWN")
    db_status = health.get("database", "UNKNOWN")
    redis_status = health.get("redis", "UNKNOWN")
    ltp_source = health.get("ltp_source", "—")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("API", api_status)
    s2.metric("Database", db_status)
    s3.metric("Redis", redis_status)
    s4.metric("LTP Source", ltp_source)


elif page == "Positions":
    st.title("Open Positions")

    positions = fetch("positions") or []

    if not positions:
        st.info("No open positions.")
    else:
        # Group by the backend's own group_id (added 2026-08-13) -- the engine
        # already knows exactly which legs belong to which _active_spreads/
        # _active_condors structure, so use that directly instead of guessing
        # from same-underlying + has-short-and-long-leg. That heuristic could
        # misattribute an unrelated standalone single-leg position
        # (ema_crossover_v1/momentum_v1) into a credit_spread_v1/
        # iron_condor_v1 structure's group whenever both happened to be open
        # on the same underlying at once -- entirely plausible since both
        # trade from the same 41-symbol F&O universe, and would silently
        # distort "Net structure PnL" by folding in an unrelated position's
        # PnL. group_id is None for single-leg / fallback-path positions,
        # which always go to standalone, never merged into a group.
        _UND_BY_LEN = sorted(FNO_SYMBOLS, key=len, reverse=True)

        def _extract_underlying(contract: str) -> str:
            for u in _UND_BY_LEN:
                if contract.startswith(u):
                    return u
            return contract[:10]

        by_group: dict = {}
        standalone: list = []
        for p in positions:
            gid = p.get("group_id")
            if gid:
                by_group.setdefault(gid, []).append(p)
            else:
                standalone.append(p)

        multi_leg_groups = [
            (_extract_underlying(legs[0].get("symbol", "")), legs)
            for legs in by_group.values()
        ]

        if multi_leg_groups:
            st.subheader("Multi-Leg Structures (Spreads / Condors)")
            for underlying, legs in multi_leg_groups:
                label = f"**{underlying}** — {len(legs)} legs"
                with st.expander(label, expanded=True):
                    rows = []
                    for p in legs:
                        qty = p.get("quantity", 0)
                        avg_p = float(p.get("avg_price") or 0)
                        mkt_p = float(p.get("market_price") or avg_p)
                        unreal = float(p.get("unrealized_pnl") or (mkt_p - avg_p) * qty)
                        rows.append({
                            "Contract": p.get("symbol", ""),
                            "Side": "SHORT" if qty < 0 else "LONG",
                            "Qty": qty,
                            "Entry": fmt_inr(avg_p),
                            "Market": fmt_inr(mkt_p),
                            "Unrealized PnL": fmt_inr(unreal),
                        })
                    sub_df = pd.DataFrame(rows)

                    def _hl(row):
                        styles = [""] * len(row)
                        for i, col in enumerate(row.index):
                            if "PnL" in col:
                                val = float(row[col].replace("₹", "").replace(",", ""))
                                styles[i] = f"color: {'green' if val > 0 else 'red' if val < 0 else 'gray'}"
                        return styles

                    st.dataframe(sub_df.style.apply(_hl, axis=1), use_container_width=True, hide_index=True)
                    net = sum(float(r["Unrealized PnL"].replace("₹", "").replace(",", "")) for r in rows)
                    st.caption(f"Net structure PnL: {fmt_inr(net)}")

        if standalone:
            st.subheader("Single-Leg Positions")
            df = pd.DataFrame(standalone)
            df["total_pnl"] = df["unrealized_pnl"].fillna(0) + df["realized_pnl"].fillna(0)
            df["capital"] = (df["quantity"].abs() * df["avg_price"]).round(2)

            # market_price == 0 means "no live quote available yet" (e.g. Zerodha
            # not currently quoting this specific contract) -- NOT "worth zero".
            # Blank it out (and the pnl fields that depend on it) to NaN so it
            # renders as "—" below instead of a misleading hard 0.00.
            no_quote = df["market_price"].fillna(0) == 0
            df.loc[no_quote, ["market_price", "unrealized_pnl", "total_pnl"]] = float("nan")

            show = [c for c in ["symbol", "quantity", "avg_price", "market_price", "unrealized_pnl", "realized_pnl", "total_pnl", "capital"] if c in df.columns]
            numeric_df = df[show].copy()
            numeric_df.columns = [c.replace("_", " ").title() for c in show]

            def highlight_pnl(row):
                styles = [""] * len(row)
                for i, col in enumerate(row.index):
                    if "Pnl" in col:
                        val = numeric_df.loc[row.name, col]
                        if pd.notna(val):
                            styles[i] = f"color: {'green' if val > 0 else 'red' if val < 0 else 'gray'}"
                return styles

            # Format to display strings ourselves rather than relying on
            # Styler's na_rep -- Streamlit's dataframe widget doesn't reliably
            # honor it for NaN cells (renders literal "None" instead). Same
            # raw-float display issue as Orders & Trades / Analytics tables
            # otherwise (e.g. "46.590000" without an explicit format).
            display_df = numeric_df.copy()
            for c in ["Avg Price", "Market Price", "Unrealized Pnl", "Realized Pnl", "Total Pnl", "Capital"]:
                if c in display_df.columns:
                    display_df[c] = display_df[c].map(lambda v: f"{v:,.2f}" if pd.notna(v) else "—")

            st.dataframe(
                display_df.style.apply(highlight_pnl, axis=1),
                use_container_width=True, hide_index=True,
            )

        st.markdown("---")
        total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
        total_realized = sum(p.get("realized_pnl", 0) for p in positions)
        c1, c2, c3 = st.columns(3)
        c1.metric("Unrealized PnL", fmt_inr(total_unrealized))
        c2.metric("Realized PnL", fmt_inr(total_realized))
        c3.metric("Total PnL", fmt_inr(total_unrealized + total_realized))


elif page == "Orders & Trades":
    st.title("Order Management")

    # ── Closed Trades (P&L) ──────────────────────────────────────────────────
    # Added 2026-08-06 — this page previously only showed raw individual orders
    # (one row per BUY/SELL leg, no P&L), which is the entry/exit pair a
    # "trade" actually is. Per-trade P&L already existed via analytics/trades
    # (used on the Analytics page) but wasn't surfaced here, on the one page
    # named for trades — added it here too using the same data.
    st.subheader("Closed Trades (P&L)")
    closed_trades = fetch("analytics/trades") or []
    if not closed_trades:
        st.info("No closed trades yet.")
    else:
        t_df = pd.DataFrame(closed_trades)
        t_show = [c for c in [
            "exit_time", "strategy", "underlying", "structure_type",
            "entry_price", "exit_price", "quantity", "pnl", "hold_days", "exit_reason",
        ] if c in t_df.columns]
        t_df = t_df.sort_values("exit_time", ascending=False)[t_show]
        t_df.columns = [c.replace("_", " ").title() for c in t_show]

        # Coerce to numeric first (None -> NaN) so a column that's ever
        # entirely null doesn't come through as object dtype, which
        # .style.format() can't apply a numeric spec to (TypeError on None).
        for _c in ["Entry Price", "Exit Price", "Quantity", "Pnl", "Hold Days"]:
            if _c in t_df.columns:
                t_df[_c] = pd.to_numeric(t_df[_c], errors="coerce")

        def _highlight_pnl(row):
            styles = [""] * len(row)
            for i, col in enumerate(row.index):
                if "Pnl" in col:
                    styles[i] = f"color: {'green' if row[col] > 0 else 'red' if row[col] < 0 else 'gray'}"
            return styles

        # Values come straight from the DB as raw floats (Numeric(18,4)
        # columns) -- without an explicit format, Styler renders full float
        # precision (e.g. "8.250000") instead of clean currency figures.
        _num_fmt = {c: "{:,.2f}" for c in ["Entry Price", "Exit Price", "Pnl"] if c in t_df.columns}
        if "Quantity" in t_df.columns:
            _num_fmt["Quantity"] = "{:,.0f}"
        if "Hold Days" in t_df.columns:
            _num_fmt["Hold Days"] = "{:,.0f}"
        st.dataframe(
            t_df.style.format(_num_fmt, na_rep="—").apply(_highlight_pnl, axis=1),
            use_container_width=True, hide_index=True,
        )

        total_pnl = sum(t.get("pnl") or 0 for t in closed_trades)
        wins = sum(1 for t in closed_trades if (t.get("pnl") or 0) > 0)
        tc1, tc2, tc3 = st.columns(3)
        tc1.metric("Closed Trades", str(len(closed_trades)))
        tc2.metric("Win Rate", f"{wins / len(closed_trades) * 100:.1f}%")
        tc3.metric("Total PnL", fmt_inr(total_pnl))

    st.markdown("---")

    # ── All Orders (raw) ─────────────────────────────────────────────────────
    st.subheader("All Orders (raw)")
    orders = fetch("orders") or []

    if not orders:
        st.info("No orders in the database yet.")
    else:
        df = pd.DataFrame(orders)

        col_f1, col_f2 = st.columns(2)
        symbols = ["All"] + sorted(df["symbol"].unique().tolist())
        statuses = ["All"] + sorted(df["status"].unique().tolist()) if "status" in df.columns else ["All"]
        sym_filter = col_f1.selectbox("Symbol", symbols)
        st_filter = col_f2.selectbox("Status", statuses)

        if sym_filter != "All":
            df = df[df["symbol"] == sym_filter]
        if st_filter != "All" and "status" in df.columns:
            df = df[df["status"] == st_filter]

        df = df.sort_values("created_at", ascending=False)
        show = [c for c in ["id", "symbol", "side", "quantity", "price", "status", "created_at"] if c in df.columns]
        df = df[show]
        df.columns = [c.replace("_", " ").title() for c in show]
        st.dataframe(df, use_container_width=True, hide_index=True)

        st.markdown("---")
        all_orders = fetch("orders") or []
        buys  = sum(1 for o in all_orders if o.get("side") == "BUY")
        sells = sum(1 for o in all_orders if o.get("side") == "SELL")
        open_c = sum(1 for o in all_orders if o.get("status") in ("PENDING", "OPEN"))
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Orders", str(len(all_orders)))
        c2.metric("BUY / SELL", f"{buys} / {sells}")
        c3.metric("Open / Pending", str(open_c))


elif page == "Strategies":
    st.title("Strategy Management")

    strategies   = fetch("strategies") or []
    health_data  = fetch("analytics/strategy-health") or {}
    perf_data    = fetch("analytics/strategy-performance") or {}
    regime_data  = fetch("analytics/market-regime") or {}
    # Added 2026-08-21 -- the "signal audit" gate-by-gate funnel (external
    # review of ema_crossover_v1, its own top recommendation: "how many
    # signals reached each gate, and how many passed" instead of guessing).
    # In-memory on the engine, resets on restart -- see get_signal_audit_report().
    audit_data   = fetch("analytics/signal-audit") or {}

    current_regime = regime_data.get("regime", "—")
    regime_ts      = regime_data.get("timestamp", "")[:16].replace("T", " ")
    st.caption(f"Current Regime: **{current_regime}** (as of {regime_ts} IST)")
    st.markdown("---")

    if not strategies:
        st.info("No strategies registered. The engine may not be running.")
    else:
        for s in strategies:
            sid    = s.get("id") or s.get("name", "unknown")
            is_active     = s.get("is_active", False)
            paused_reason = s.get("paused_reason") or ""
            perf   = perf_data.get(sid, {})
            health = health_data.get(sid, {})

            with st.container():
                c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
                status_icon = "🟢 Running" if is_active else "🔴 Paused"
                c1.markdown(f"**{sid}**")
                c2.write(status_icon)
                c3.metric("Trades", perf.get("trade_count", 0))
                c4.metric("Win Rate", f"{perf.get('win_rate', 0)*100:.0f}%" if perf else "—")
                c5.metric("Total PnL", fmt_inr(perf.get("total_pnl", 0)) if perf else "—")

                if not is_active and paused_reason:
                    st.caption(f"Paused: {paused_reason}")

                # Auto-kill health — how close this strategy is to being paused for
                # statistical deterioration, not just whether it currently is.
                _rpf, _rdd = health.get("rolling_pf"), health.get("rolling_drawdown")
                _pff, _ddt = health.get("pf_floor"),   health.get("dd_threshold")
                if _rpf is not None or _rdd is not None:
                    _bits = []
                    if _rpf is not None and _pff is not None:
                        _bits.append(f"Rolling PF {_rpf:.2f} (auto-pause below {_pff:.2f})")
                    if _rdd is not None and _ddt is not None:
                        _bits.append(f"Rolling drawdown {fmt_inr(_rdd)} (auto-pause at {fmt_inr(_ddt)})")
                    st.caption(f"Health ({health.get('trades_in_window', 0)} recent trades): " + " · ".join(_bits))

                act_col, deact_col, _ = st.columns([1, 1, 5])
                if act_col.button("Activate", key=f"act_{sid}", disabled=is_active):
                    r = requests.post(
                        f"{API_BASE_URL}/strategies/activate",
                        json={"strategy_id": sid},
                        headers=_ADMIN_AUTH_HEADERS,
                    )
                    st.success("Activated." if r.ok else r.text)
                    st.rerun()
                if deact_col.button("Pause", key=f"deact_{sid}", disabled=not is_active):
                    r = requests.post(
                        f"{API_BASE_URL}/strategies/deactivate",
                        json={"strategy_id": sid},
                        headers=_ADMIN_AUTH_HEADERS,
                    )
                    st.success("Paused." if r.ok else r.text)
                    st.rerun()

                # Signal audit funnel: how many candidate signals reached
                # each entry gate, in the order they're actually checked in
                # LiveTradingEngine._process_signal. Only single-leg
                # strategies (ema_crossover_v1/momentum_v1) go through this
                # gate sequence -- credit_spread_v1/iron_condor_v1 have their
                # own separate, much longer precondition chains not yet
                # instrumented this way.
                gates = audit_data.get(sid)
                if gates:
                    with st.expander(f"Signal audit — {sid}"):
                        _gate_order = [
                            "signal_generated", "dte_passed", "rvol_passed", "adx_passed",
                            "rs_passed", "mtf_passed", "lot_passed", "contract_resolved",
                            "trade_placed",
                        ]
                        _rows = [
                            {"Gate": g.replace("_", " ").title(), "Reached": gates[g]}
                            for g in _gate_order if g in gates
                        ]
                        if _rows:
                            st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
                            st.caption(
                                "In-memory since last restart — a big drop between two "
                                "consecutive gates is the specific filter rejecting most "
                                "candidates right now."
                            )

            st.markdown("---")


elif page == "Risk & PnL":
    st.title("Risk Engine & PnL Report")

    ks_status = fetch("admin/kill-switch") or {}
    if ks_status.get("active"):
        st.error(
            f"🔴 Kill switch ACTIVE — no new entries can be placed. "
            f"Reason: {ks_status.get('reason') or 'unknown'}. Reset from the Admin tab."
        )
        st.markdown("---")

    positions  = fetch("positions") or []
    orders     = fetch("orders") or []
    pnl_data   = fetch("analytics/pnl-summary") or {}
    capital_periods = (fetch("analytics/capital-periods") or {}).get("periods", [])
    active_period = next((p for p in capital_periods if not p.get("closed")), None)

    today_pnl       = pnl_data.get("today_pnl", 0)
    today_realized  = pnl_data.get("today_realized", 0)
    today_unrealized= pnl_data.get("today_unrealized", 0)
    total_pnl       = pnl_data.get("total_pnl", 0)
    total_realized  = pnl_data.get("total_realized", 0)
    total_unrealized= pnl_data.get("total_unrealized", 0)
    # Compounding capital (current expiry-to-expiry period), falls back to
    # the static .env value before the first period has been bootstrapped.
    capital = active_period.get("capital_now") if active_period else INITIAL_CAPITAL
    max_loss_limit = capital * MAX_DAILY_LOSS_PCT

    # ── Today's PnL ─────────────────────────────────────────────────────────
    st.subheader("Today's PnL")
    c1, c2, c3 = st.columns(3)
    today_pct = f"{(today_pnl / capital) * 100:.2f}%" if capital else "0%"
    c1.metric("Today's Net PnL",   fmt_inr(today_pnl),       today_pct)
    c2.metric("Today Realized",    fmt_inr(today_realized),
              f"{pnl_data.get('closed_trades_today', 0)} closed trades")
    c3.metric("Unrealized (Open)", fmt_inr(today_unrealized),
              f"{pnl_data.get('open_positions', 0)} open positions")

    st.markdown("---")

    # ── All-time PnL ─────────────────────────────────────────────────────────
    st.subheader("All-Time PnL")
    c1, c2, c3, c4 = st.columns(4)
    total_pct = f"{(total_pnl / capital) * 100:.2f}%" if capital else "0%"
    c1.metric("Total Net PnL",  fmt_inr(total_pnl),       total_pct)
    c2.metric("Total Realized", fmt_inr(total_realized),
              f"{pnl_data.get('closed_trades_total', 0)} closed trades")
    c3.metric("Unrealized",     fmt_inr(total_unrealized))
    c4.metric("Capital",        fmt_inr(capital),
              "current period, compounding" if active_period else "static .env value")

    st.markdown("---")

    # ── Monthly P&L (expiry-to-expiry) ─────────────────────────────────────
    st.subheader("Monthly P&L (Expiry-to-Expiry)")
    st.caption(
        "Each “month” is one NSE F&O expiry cycle, not a calendar month. "
        "A closed period's ending capital becomes the next period's starting "
        "capital — profits/losses compound."
    )
    if capital_periods:
        rows = []
        for p in reversed(capital_periods):
            row = {
                "Period":            f"{p['period_start']} → {p['period_end']}",
                "Starting Capital":  fmt_inr(p["starting_capital"]),
                "Status":            "Open" if not p["closed"] else "Closed",
            }
            if p["closed"]:
                row["Realized PnL"]  = fmt_inr(p["realized_pnl"])
                row["Ending Capital"] = fmt_inr(p["ending_capital"])
            else:
                row["Realized PnL"]  = fmt_inr(p.get("realized_pnl_so_far", 0)) + " (so far)"
                row["Ending Capital"] = fmt_inr(p.get("capital_now", p["starting_capital"])) + " (running)"
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No capital periods recorded yet.")

    st.markdown("---")
    st.subheader("Risk Limits")

    open_pos = len([p for p in positions if p.get("quantity", 0) != 0])
    capital_deployed = _estimate_capital_deployed(positions)
    capital_left = capital - capital_deployed

    c1, c2, c3 = st.columns(3)
    c1.metric("Capital in Use", fmt_inr(capital_deployed))
    c2.metric("Capital Left",   fmt_inr(capital_left))
    c3.metric("Utilization",    f"{(capital_deployed / capital * 100):.1f}%" if capital else "0%")

    col1, col2 = st.columns(2)
    pos_ratio = min(1.0, open_pos / MAX_OPEN_POSITIONS) if MAX_OPEN_POSITIONS > 0 else 0.0
    col1.progress(pos_ratio, text=f"Open Positions: {open_pos} / {MAX_OPEN_POSITIONS}")

    loss_used = abs(min(today_pnl, 0))
    loss_ratio = min(1.0, loss_used / max_loss_limit) if max_loss_limit > 0 else 0.0
    col2.progress(
        loss_ratio,
        text=f"Daily Loss: {fmt_inr(loss_used)} / {fmt_inr(max_loss_limit)} ({MAX_DAILY_LOSS_PCT*100:.0f}% limit)"
    )

    st.markdown("---")
    st.subheader("Order Activity")
    total_orders = len(orders)
    open_orders  = sum(1 for o in orders if o.get("status") in ("PENDING", "OPEN"))
    completed    = sum(1 for o in orders if o.get("status") == "COMPLETED")
    failed       = sum(1 for o in orders if o.get("status") in ("FAILED", "REJECTED_BY_RISK", "REJECTED"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Orders", total_orders)
    c2.metric("Open",         open_orders)
    c3.metric("Completed",    completed)
    c4.metric("Failed / Rejected", failed)

    if positions:
        st.markdown("---")
        st.subheader("Position Detail")
        rows = []
        for p in positions:
            unreal = p.get("unrealized_pnl", 0)
            real   = p.get("realized_pnl", 0)
            rows.append({
                "Symbol":       p.get("symbol", ""),
                "Qty":          p.get("quantity", 0),
                "Avg Price":    fmt_inr(p.get("avg_price", 0)),
                "Market Price": fmt_inr(p.get("market_price", 0)),
                "Unrealized":   fmt_inr(unreal),
                "Realized":     fmt_inr(real),
                "Total PnL":    fmt_inr(unreal + real),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


elif page == "Analytics":
    st.title("Trade Analytics")

    summary = fetch("analytics/summary") or {}
    trades  = fetch("analytics/trades") or []
    by_sym  = fetch("analytics/by-symbol") or []

    if not trades:
        st.info("No closed trades in the journal yet. Analytics populate once positions are closed.")
    else:
        # ── Overall banner ──────────────────────────────────────────────────
        overall = summary.get("__overall__", {})
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Closed Trades", overall.get("trade_count", 0))
        c2.metric("Win Rate",   f"{overall.get('win_rate', 0)*100:.1f}%")
        c3.metric("Total PnL",  fmt_inr(overall.get("total_pnl", 0)))
        c4.metric("Avg PnL / Trade", fmt_inr(overall.get("avg_pnl", 0)))

        st.markdown("---")

        # ── Per-strategy breakdown ──────────────────────────────────────────
        st.subheader("Strategy Performance")
        strat_rows = []
        for strat, stats in summary.items():
            if strat == "__overall__":
                continue
            strat_rows.append({
                "Strategy":      strat,
                "Trades":        stats.get("trade_count", 0),
                "Win Rate":      f"{stats.get('win_rate', 0)*100:.1f}%",
                "Avg PnL":       fmt_inr(stats.get("avg_pnl", 0)),
                "Total PnL":     fmt_inr(stats.get("total_pnl", 0)),
                "Best Trade":    fmt_inr(stats.get("max_win",  0)),
                "Worst Trade":   fmt_inr(stats.get("max_loss", 0)),
                "Avg Hold Days": stats.get("avg_hold_days", 0),
            })
        if strat_rows:
            st.dataframe(pd.DataFrame(strat_rows), use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── By symbol ──────────────────────────────────────────────────────
        col_l, col_r = st.columns(2)
        # by_sym is pre-sorted descending by total_pnl. Split by the SIGN of total_pnl
        # rather than taking top-10 / bottom-10 of the whole list — with fewer than 10
        # distinct symbols (the common case early on), a naive top/bottom split shows
        # the exact same symbols in both boxes, just reversed (e.g. a single losing
        # symbol among 3 total would appear as both a "best" and a "worst" symbol).
        winners = [s for s in by_sym if s["total_pnl"] > 0]
        losers  = list(reversed([s for s in by_sym if s["total_pnl"] < 0]))

        def _render_symbol_table(rows):
            df = pd.DataFrame(rows)
            df["total_pnl"] = df["total_pnl"].apply(fmt_inr)
            df["avg_pnl"]   = df["avg_pnl"].apply(fmt_inr)
            df["win_rate"]  = df["win_rate"].apply(lambda v: f"{v*100:.1f}%")
            df.columns      = ["Symbol", "Trades", "Total PnL", "Avg PnL", "Win Rate"]
            st.dataframe(df, use_container_width=True, hide_index=True)

        with col_l:
            st.subheader("Best Symbols")
            if winners:
                _render_symbol_table(winners[:10])
            else:
                st.info("No profitable symbols yet.")

        with col_r:
            st.subheader("Worst Symbols")
            if losers:
                _render_symbol_table(losers[:10])
            else:
                st.info("No losing symbols yet.")

        st.markdown("---")

        # ── Trade log ──────────────────────────────────────────────────────
        st.subheader("Closed Trade Log")
        # Added 2026-08-21 -- the entry-context/MFE-MAE/daily-ATR/credit-max-
        # loss/wing-failure columns (three external-review rounds, same day)
        # exist in trade_journal and are now returned by /analytics/trades,
        # but cramming all ~12 of them into the default table would bury the
        # core P&L columns most sessions actually need. Off by default.
        show_detail = st.checkbox(
            "Show entry-context & MFE/MAE detail",
            help="Per-trade attribution data added 2026-08-21: ADX/RVOL/delta/DTE at "
                 "entry, running MFE/MAE, daily ATR%, credit/max-loss%, and (iron "
                 "condor) which wing failed.",
        )
        df_t = pd.DataFrame(trades)
        base_cols = [
            "entry_time", "underlying", "strategy", "structure_type",
            "entry_price", "exit_price", "pnl", "hold_days",
            "iv_rank", "vix_at_entry", "exit_reason",
        ]
        detail_cols = [
            "adx_at_entry", "rvol_at_entry", "delta_at_entry", "dte_at_entry",
            "daily_atr_pct", "credit_to_max_loss_pct",
            "underlying_mfe_pct", "underlying_mae_pct",
            "option_mfe_pct", "option_mae_pct", "wing_failed",
        ]
        show_cols = [c for c in base_cols + (detail_cols if show_detail else []) if c in df_t.columns]
        df_t = df_t[show_cols]
        df_t.columns = [c.replace("_", " ").title() for c in show_cols]

        # Coerce to numeric first (None -> NaN) so columns that are entirely
        # null (e.g. iv_rank before ~30 days of history has accumulated)
        # don't come through as object dtype, which .style.format() can't
        # apply a numeric spec to (TypeError on None).
        _numeric_candidates = [
            "Entry Price", "Exit Price", "Pnl", "Iv Rank", "Vix At Entry", "Hold Days",
            "Adx At Entry", "Rvol At Entry", "Delta At Entry", "Dte At Entry",
            "Daily Atr Pct", "Credit To Max Loss Pct",
            "Underlying Mfe Pct", "Underlying Mae Pct", "Option Mfe Pct", "Option Mae Pct",
        ]
        for _c in _numeric_candidates:
            if _c in df_t.columns:
                df_t[_c] = pd.to_numeric(df_t[_c], errors="coerce")

        def _highlight_pnl(row):
            styles = [""] * len(row)
            for i, col in enumerate(row.index):
                if "Pnl" in col:
                    styles[i] = f"color: {'green' if row[col] > 0 else 'red' if row[col] < 0 else 'gray'}"
            return styles

        # Same raw-float display issue as Orders & Trades' trade table --
        # DB Numeric(18,4)/float columns render as e.g. "8.250000" without
        # an explicit format.
        _num_fmt = {c: "{:,.2f}" for c in [
            "Entry Price", "Exit Price", "Pnl", "Iv Rank", "Vix At Entry",
            "Adx At Entry", "Rvol At Entry", "Delta At Entry",
            "Daily Atr Pct", "Credit To Max Loss Pct",
            "Underlying Mfe Pct", "Underlying Mae Pct", "Option Mfe Pct", "Option Mae Pct",
        ] if c in df_t.columns}
        for _c in ["Hold Days", "Dte At Entry"]:
            if _c in df_t.columns:
                _num_fmt[_c] = "{:,.0f}"
        st.dataframe(
            df_t.style.format(_num_fmt, na_rep="—").apply(_highlight_pnl, axis=1),
            use_container_width=True, hide_index=True,
        )

        # ── Iron condor wing-failure breakdown ───────────────────────────────
        # Added 2026-08-21 (external review of iron_condor_v1's own explicit
        # ask: "I want a wing failure analysis"). Which wing's exit condition
        # actually fired, across closed condors -- the root-cause
        # classification the review also asked for (breakout / IV expansion /
        # gap / drift / bad strike) is a human analysis output from this
        # count, not something derivable automatically yet.
        _condor_trades = [t for t in trades if t.get("structure_type") == "IRON_CONDOR" and t.get("wing_failed")]
        if _condor_trades:
            st.markdown("---")
            st.subheader("Iron Condor — Wing Failure Breakdown")
            _wf_counts = pd.Series([t["wing_failed"] for t in _condor_trades]).value_counts()
            wf1, wf2, wf3 = st.columns(3)
            wf1.metric("Put wing failed",  int(_wf_counts.get("PUT", 0)))
            wf2.metric("Call wing failed", int(_wf_counts.get("CALL", 0)))
            wf3.metric("Both wings failed", int(_wf_counts.get("BOTH", 0)))


elif page == "System Health":
    st.title("System Health")

    health = fetch("health") or {}

    if not health:
        st.error("Cannot reach the API.")
    else:
        checks = {
            "API":        health.get("status", "UNKNOWN"),
            "Database":   health.get("database", "UNKNOWN"),
            "Redis":      health.get("redis", "UNKNOWN"),
            "LTP Source": health.get("ltp_source", "unknown"),
        }
        for label, value in checks.items():
            if isinstance(value, str) and value.startswith("UP"):
                st.success(f"**{label}**: {value}")
            elif value in ("zerodha_realtime",):
                st.success(f"**{label}**: {value} (real-time WebSocket)")
            elif value == "zerodha_rest":
                st.success(f"**{label}**: {value} (REST poll, 5 s delay)")
            elif value == "zerodha_historical":
                st.info(f"**{label}**: {value} (historical OHLC, 60 s delay)")
            elif isinstance(value, str) and value.startswith("DOWN"):
                st.error(f"**{label}**: {value}")
            elif isinstance(value, (int, float)):
                st.info(f"**{label}**: {value}")
            else:
                st.info(f"**{label}**: {value}")

        # Market-data source. Zerodha confirmed (2026-07-27, in writing) that
        # historical_data() is not designed for the current trading session and
        # recommended generating candles from live ticks instead — so today's
        # bars always come from live ticks now (see update_intraday_bar() in
        # core/utils.py); historical_data() only supplies the prior-day baseline.
        # This just reports whether every symbol has live tick data flowing yet.
        ds = health.get("data_source") or {}
        all_live   = ds.get("all_symbols_live")
        n_missing  = ds.get("symbols_without_live_data")
        n_symbols  = ds.get("n_symbols")
        if all_live is None:
            st.info("**Market Data Source**: not yet reported (market closed, or LTPPoller hasn't run a cycle yet)")
        elif all_live:
            st.success(f"**Market Data Source**: live ticks flowing for all {n_symbols} tracked symbols")
        else:
            st.warning(
                f"**Market Data Source**: {n_missing}/{n_symbols} symbols have no live tick "
                "data yet — using historical_data() as a bootstrap fallback for them. "
                "Normal only in the first few seconds after market open; persisting longer "
                "than that is worth checking (WebSocket/REST poller connectivity)."
            )
            st.caption(
                "The API self-heals a stuck WebSocket automatically within a few minutes "
                "(see the tick-staleness watchdog). If this keeps persisting, you can force "
                "a restart from Admin → Restart API."
            )

    st.markdown("---")

    # ── Live Log Tail ─────────────────────────────────────────────────────────
    st.subheader("Live Log (last 20 lines)")

    log_col, btn_col = st.columns([5, 1])
    with btn_col:
        if st.button("Refresh", key="refresh_logs"):
            st.rerun()

    log_data = fetch(f"logs/recent?n=20&token={quote_plus(_LOGS_API_TOKEN)}") or {}
    log_lines = log_data.get("lines", [])
    log_note  = log_data.get("note", "")

    if log_note:
        st.info(log_note)
    elif not log_lines:
        st.info("No log entries yet.")
    else:
        _COLOR = {
            "CRITICAL": "#FF0000",
            "ERROR":    "#FF4B4B",
            "WARNING":  "#FFA500",
            "DEBUG":    "#888888",
            "INFO":     "#DDDDDD",
        }
        html_rows = []
        for entry in log_lines:
            raw   = entry.get("text", "")
            level = entry.get("level", "INFO")
            # Escape HTML special chars
            safe  = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            color = _COLOR.get(level, "#DDDDDD")
            html_rows.append(f'<span style="color:{color}">{safe}</span>')

        log_html = (
            '<pre style="'
            "background:#0e1117;padding:12px;border-radius:6px;"
            "overflow-x:auto;font-size:11px;line-height:1.6;"
            'white-space:pre-wrap;word-break:break-all">'
            + "<br>".join(html_rows)
            + "</pre>"
        )
        st.markdown(log_html, unsafe_allow_html=True)

    from src.core.utils import now_ist
    st.caption(
        f"Fetched at {now_ist().strftime('%H:%M:%S')} IST  ·  "
        "ERROR = red  ·  WARNING = orange  ·  DEBUG = gray"
    )


elif page == "Admin":
    st.title("Admin Controls")
    st.warning("These controls affect live trading state. Use with care.")

    # ── Email Alerts ─────────────────────────────────────────────────────────
    st.subheader("Email Alerts")
    alert_status = fetch("admin/email-alerts") or {}
    is_paused    = alert_status.get("paused", False)
    configured   = alert_status.get("configured", False)

    if not configured:
        st.error("Email is not configured — check EMAIL_SENDER / EMAIL_APP_PASSWORD / EMAIL_RECIPIENT in .env")
    else:
        if is_paused:
            st.error("Email alerts are currently PAUSED")
            if st.button("Resume Email Alerts", type="primary"):
                result = post("admin/email-alerts/resume")
                if result:
                    st.success("Email alerts resumed.")
                    st.rerun()
        else:
            st.success("Email alerts are ACTIVE")
            if st.button("Pause Email Alerts"):
                result = post("admin/email-alerts/pause")
                if result:
                    st.warning("Email alerts paused.")
                    st.rerun()

    st.markdown("---")

    # ── Kill Switch ──────────────────────────────────────────────────────────
    st.subheader("Kill Switch")
    ks_status  = fetch("admin/kill-switch") or {}
    ks_active  = ks_status.get("active", False)

    if ks_active:
        st.error(
            f"Kill switch is ACTIVE — no new entries can be placed. "
            f"Reason: {ks_status.get('reason') or 'unknown'} "
            f"(tripped at {ks_status.get('activated_at') or 'unknown time'} UTC)"
        )
        st.caption("Existing positions can still be closed — only new entries are blocked.")
        st.markdown(
            "Confirm the underlying issue is actually resolved before resetting — "
            "this does not re-check anything, it only unblocks new entries."
        )
        if st.button("Reset Kill Switch", type="primary"):
            result = post("admin/kill-switch/reset")
            if result:
                st.success("Kill switch reset. New entries can resume.")
                st.rerun()
    else:
        st.success("Kill switch is inactive — trading is not blocked.")

    st.markdown("---")

    # ── Restart API ──────────────────────────────────────────────────────────
    st.subheader("Restart API")
    st.markdown("""
Gracefully restarts the API process — equivalent to `docker compose restart api`
on the server, without needing SSH access. Useful when market data looks stuck
(e.g. System Health shows symbols with no live tick data for an extended period)
and the automatic tick-staleness watchdog hasn't recovered it on its own.

All engine state (open positions, spreads, condors, today's order count) is
Redis-persisted and restored automatically — this is the same restart procedure
used routinely to deploy code changes. Takes about 10-15 seconds.
""")

    if "confirm_restart_api" not in st.session_state:
        st.session_state["confirm_restart_api"] = False

    if not st.session_state["confirm_restart_api"]:
        if st.button("Restart API", type="secondary"):
            st.session_state["confirm_restart_api"] = True
            st.rerun()
    else:
        st.warning("Restart the API now? Live market data and signal processing will pause for ~10-15 seconds.")
        col1, col2 = st.columns(2)
        if col1.button("YES — Restart Now", type="primary"):
            result = post("admin/restart-api")
            st.session_state["confirm_restart_api"] = False
            if result:
                st.success(result.get("message", "Restarting..."))
                st.info("Refresh this page in about 15 seconds.")
            st.rerun()
        if col2.button("Cancel", key="cancel_restart_api"):
            st.session_state["confirm_restart_api"] = False
            st.rerun()

    st.markdown("---")

    # ── Reset All Data ───────────────────────────────────────────────────────
    st.subheader("Reset Platform Data")
    st.markdown("""
**What this deletes:**
- All orders, positions, trades, trade journal entries
- All audit logs, signals, walk-forward results
- Engine in-memory state (active spreads, condors, peak premiums)
- PaperBroker virtual balance reset to ₹3,00,000

**What is kept:**
- Stocks, instruments, historical OHLC data
- Zerodha access token and lot sizes
- Strategy configuration
""")

    if "confirm_reset" not in st.session_state:
        st.session_state["confirm_reset"] = False

    if not st.session_state["confirm_reset"]:
        if st.button("Reset All Trading Data", type="secondary"):
            st.session_state["confirm_reset"] = True
            st.rerun()
    else:
        st.error("Are you sure? This will permanently delete ALL trading history and cannot be undone.")
        col1, col2 = st.columns(2)
        if col1.button("YES — Delete Everything", type="primary"):
            result = post("admin/reset", timeout=30)
            if result:
                st.session_state["confirm_reset"] = False
                st.success(f"Reset complete. {result.get('message', '')}")
                st.balloons()
                st.rerun()
        if col2.button("Cancel"):
            st.session_state["confirm_reset"] = False
            st.rerun()


# ── Auto-refresh ───────────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(60)
    st.rerun()
