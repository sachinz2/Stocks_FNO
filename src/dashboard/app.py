import os
import time
import json
import streamlit as st
import requests
import pandas as pd
from datetime import datetime, date

st.set_page_config(page_title="Falcon Quant Platform", layout="wide", page_icon="🦅")

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000/api/v1")
_DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")
INITIAL_CAPITAL = float(os.environ.get("INITIAL_CAPITAL", "300000"))
MAX_DAILY_LOSS_PCT = 0.05   # 5% — matches RiskManager
MAX_OPEN_POSITIONS = 25     # matches RiskManager


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
    ["Home", "Positions", "Orders & Trades", "Strategies", "Risk & PnL", "System Health"],
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


def fmt_inr(val: float) -> str:
    return f"₹{val:,.2f}"


def pnl_color(val: float) -> str:
    if val > 0:
        return "green"
    if val < 0:
        return "red"
    return "gray"


# ── Pages ─────────────────────────────────────────────────────────────────────

if page == "Home":
    st.title("Dashboard Overview")

    positions = fetch("positions") or []
    orders = fetch("orders") or []
    health = fetch("health") or {}

    net_pnl = sum(p.get("unrealized_pnl", 0) + p.get("realized_pnl", 0) for p in positions)
    open_positions = len([p for p in positions if p.get("quantity", 0) != 0])
    capital_deployed = sum(abs(p.get("quantity", 0)) * p.get("avg_price", 0) for p in positions)
    capital_pct = (capital_deployed / INITIAL_CAPITAL) * 100 if capital_deployed else 0

    today_str = date.today().isoformat()
    orders_today = [o for o in orders if (o.get("created_at") or "").startswith(today_str)]
    open_orders = [o for o in orders if o.get("status") in ("PENDING", "OPEN")]

    col1, col2, col3, col4 = st.columns(4)
    pnl_delta = f"{(net_pnl / INITIAL_CAPITAL) * 100:.2f}%" if INITIAL_CAPITAL else "0%"
    col1.metric("Net PnL", fmt_inr(net_pnl), pnl_delta)
    col2.metric("Open Positions", str(open_positions), f"Max {MAX_OPEN_POSITIONS}")
    col3.metric("Orders Today", str(len(orders_today)), f"{len(open_orders)} open")
    col4.metric("Capital Deployed", fmt_inr(capital_deployed), f"{capital_pct:.1f}%")

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
        # Detect spread/condor legs by reading Redis state via API (best-effort grouping)
        spread_symbols: set = set()
        condor_symbols: set = set()

        # Try to identify multi-leg groups from position contracts
        # A contract is a spread/condor leg if there is another contract with the same underlying
        # and opposite qty sign (short + long pair)
        by_underlying: dict = {}
        for p in positions:
            sym = p.get("symbol", "")
            qty = p.get("quantity", 0)
            # Extract underlying: strip trailing digits and option type
            underlying = sym.rstrip("CEPE").rstrip("0123456789").rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")[:10]
            by_underlying.setdefault(underlying, []).append(p)

        # Identify grouped multi-leg structures
        multi_leg_groups: list = []
        standalone: list = []
        for underlying, legs in by_underlying.items():
            has_short = any(l.get("quantity", 0) < 0 for l in legs)
            has_long = any(l.get("quantity", 0) > 0 for l in legs)
            if has_short and has_long and len(legs) >= 2:
                multi_leg_groups.append((underlying, legs))
            else:
                standalone.extend(legs)

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
            show = [c for c in ["symbol", "quantity", "avg_price", "market_price", "unrealized_pnl", "realized_pnl", "total_pnl", "capital"] if c in df.columns]
            df = df[show]
            df.columns = [c.replace("_", " ").title() for c in show]

            def highlight_pnl(row):
                styles = [""] * len(row)
                for i, col in enumerate(row.index):
                    if "Pnl" in col:
                        styles[i] = f"color: {'green' if row[col] > 0 else 'red' if row[col] < 0 else 'gray'}"
                return styles

            st.dataframe(df.style.apply(highlight_pnl, axis=1), use_container_width=True, hide_index=True)

        st.markdown("---")
        total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
        total_realized = sum(p.get("realized_pnl", 0) for p in positions)
        c1, c2, c3 = st.columns(3)
        c1.metric("Unrealized PnL", fmt_inr(total_unrealized))
        c2.metric("Realized PnL", fmt_inr(total_realized))
        c3.metric("Total PnL", fmt_inr(total_unrealized + total_realized))


elif page == "Orders & Trades":
    st.title("Order Management")

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

    strategies = fetch("strategies") or []

    if not strategies:
        st.info("No strategies registered.")
    else:
        for s in strategies:
            col1, col2 = st.columns([3, 1])
            name = s.get("name", "Unknown")
            active = s.get("active", False)
            col1.markdown(f"**{name}** — {'Active' if active else 'Inactive'}")
            col2.write("🟢 Running" if active else "⚫ Stopped")
        st.markdown("---")
        st.caption("Activate / deactivate strategies via the FastAPI docs at `/docs`.")


elif page == "Risk & PnL":
    st.title("Risk Engine & PnL Report")

    positions = fetch("positions") or []
    orders    = fetch("orders") or []

    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
    total_realized   = sum(p.get("realized_pnl", 0) for p in positions)
    net_pnl = total_unrealized + total_realized
    capital = INITIAL_CAPITAL
    max_loss_limit = capital * MAX_DAILY_LOSS_PCT

    st.subheader("PnL Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net PnL", fmt_inr(net_pnl), f"{(net_pnl / capital) * 100:.2f}%")
    c2.metric("Unrealized", fmt_inr(total_unrealized))
    c3.metric("Realized",   fmt_inr(total_realized))
    c4.metric("Capital",    fmt_inr(capital))

    st.markdown("---")
    st.subheader("Risk Limits")

    open_pos = len([p for p in positions if p.get("quantity", 0) != 0])
    capital_deployed = sum(abs(p.get("quantity", 0)) * p.get("avg_price", 0) for p in positions)

    col1, col2 = st.columns(2)
    pos_ratio = min(1.0, open_pos / MAX_OPEN_POSITIONS) if MAX_OPEN_POSITIONS > 0 else 0.0
    col1.progress(pos_ratio, text=f"Open Positions: {open_pos} / {MAX_OPEN_POSITIONS}")

    loss_used = abs(min(net_pnl, 0))
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


elif page == "System Health":
    st.title("System Health")

    health = fetch("health") or {}

    if not health:
        st.error("Cannot reach the API.")
    else:
        col1, col2 = st.columns(2)
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
            elif value in ("yfinance", "yfinance_fallback"):
                st.warning(f"**{label}**: {value} (15-min delay — Zerodha ticker offline?)")
            elif isinstance(value, str) and value.startswith("DOWN"):
                st.error(f"**{label}**: {value}")
            elif isinstance(value, (int, float)):
                st.info(f"**{label}**: {value}")
            else:
                st.info(f"**{label}**: {value}")

    st.markdown("---")
    st.caption(f"Last checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# ── Auto-refresh ───────────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(60)
    st.rerun()
