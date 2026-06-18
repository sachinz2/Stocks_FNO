import os
import streamlit as st
import requests
import pandas as pd
from datetime import datetime, date

st.set_page_config(page_title="Falcon Quant Platform", layout="wide", page_icon="🦅")

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000/api/v1")
_DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")


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
page = st.sidebar.radio("Navigate", ["Home", "Positions", "Orders & Trades", "Strategies", "Risk & PnL", "System Health"])
st.sidebar.markdown("---")
if st.sidebar.button("Refresh Data"):
    st.rerun()


# --- API helpers ---
def fetch(endpoint: str):
    """GET from the internal API. Returns parsed JSON or None on error."""
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


# ── Pages ────────────────────────────────────────────────────────────────────

if page == "Home":
    st.title("Dashboard Overview")

    positions = fetch("positions") or []
    orders = fetch("orders") or []
    health = fetch("health") or {}

    # Compute real metrics
    net_pnl = sum(p.get("unrealized_pnl", 0) + p.get("realized_pnl", 0) for p in positions)
    open_positions = len(positions)
    capital_deployed = sum(
        abs(p.get("quantity", 0)) * p.get("avg_price", 0) for p in positions
    )
    capital_pct = (capital_deployed / 300000) * 100 if capital_deployed else 0

    today_str = date.today().isoformat()
    orders_today = [o for o in orders if (o.get("created_at") or "").startswith(today_str)]
    open_orders = [o for o in orders if o.get("status") in ("PENDING", "OPEN")]

    col1, col2, col3, col4 = st.columns(4)
    pnl_delta = f"{(net_pnl / 300000) * 100:.2f}%" if net_pnl else "0%"
    col1.metric("Net PnL", fmt_inr(net_pnl), pnl_delta)
    col2.metric("Open Positions", str(open_positions), f"Max 5")
    col3.metric("Orders Today", str(len(orders_today)), f"{len(open_orders)} open")
    col4.metric("Capital Deployed", fmt_inr(capital_deployed), f"{capital_pct:.1f}%")

    st.markdown("---")

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Recent Orders")
        if orders:
            recent = sorted(orders, key=lambda x: x.get("created_at") or "", reverse=True)[:10]
            df = pd.DataFrame(recent)[["id", "symbol", "side", "quantity", "price", "status", "created_at"]]
            df.columns = ["ID", "Symbol", "Side", "Qty", "Price", "Status", "Time"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No orders yet.")

    with col_right:
        st.subheader("Open Positions")
        if positions:
            df = pd.DataFrame(positions)[["symbol", "quantity", "avg_price", "unrealized_pnl"]]
            df.columns = ["Symbol", "Qty", "Avg Price", "Unrealized PnL"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No open positions.")

    # System status strip
    st.markdown("---")
    api_status = health.get("status", "UNKNOWN")
    db_status = health.get("database", "UNKNOWN")
    redis_status = health.get("redis", "UNKNOWN")
    s1, s2, s3 = st.columns(3)
    s1.metric("API", api_status)
    s2.metric("Database", db_status)
    s3.metric("Redis", redis_status)


elif page == "Positions":
    st.title("Open Positions")

    positions = fetch("positions") or []

    if not positions:
        st.info("No open positions.")
    else:
        df = pd.DataFrame(positions)
        df["total_pnl"] = df["unrealized_pnl"] + df["realized_pnl"]
        df["capital"] = (df["quantity"].abs() * df["avg_price"]).round(2)

        display_cols = ["symbol", "quantity", "avg_price", "market_price", "unrealized_pnl", "realized_pnl", "total_pnl", "capital"]
        existing = [c for c in display_cols if c in df.columns]
        df = df[existing]
        df.columns = [c.replace("_", " ").title() for c in existing]

        def highlight_pnl(row):
            styles = [""] * len(row)
            for i, col in enumerate(row.index):
                if "Pnl" in col:
                    styles[i] = f"color: {'green' if row[col] > 0 else 'red' if row[col] < 0 else 'gray'}"
            return styles

        st.dataframe(df.style.apply(highlight_pnl, axis=1), use_container_width=True, hide_index=True)

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

        # Filters
        col_f1, col_f2 = st.columns(2)
        symbols = ["All"] + sorted(df["symbol"].unique().tolist())
        statuses = ["All"] + sorted(df["status"].unique().tolist())
        sym_filter = col_f1.selectbox("Symbol", symbols)
        st_filter = col_f2.selectbox("Status", statuses)

        if sym_filter != "All":
            df = df[df["symbol"] == sym_filter]
        if st_filter != "All":
            df = df[df["status"] == st_filter]

        df = df.sort_values("created_at", ascending=False)
        display_cols = ["id", "symbol", "side", "quantity", "price", "status", "created_at"]
        existing = [c for c in display_cols if c in df.columns]
        df = df[existing]
        df.columns = [c.replace("_", " ").title() for c in existing]
        st.dataframe(df, use_container_width=True, hide_index=True)

        # Summary
        st.markdown("---")
        all_orders = fetch("orders") or []
        buys = sum(1 for o in all_orders if o.get("side") == "BUY")
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
            status_badge = "🟢 Running" if active else "⚫ Stopped"
            col2.write(status_badge)
        st.markdown("---")
        st.caption("Strategy activate/deactivate requires API auth. Use the FastAPI docs at `/docs`.")


elif page == "Risk & PnL":
    st.title("Risk Engine & PnL Report")

    positions = fetch("positions") or []
    orders = fetch("orders") or []

    # Real PnL
    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
    total_realized = sum(p.get("realized_pnl", 0) for p in positions)
    net_pnl = total_unrealized + total_realized
    capital = 300000.0

    st.subheader("PnL Summary")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net PnL", fmt_inr(net_pnl), f"{(net_pnl / capital) * 100:.2f}%")
    c2.metric("Unrealized", fmt_inr(total_unrealized))
    c3.metric("Realized", fmt_inr(total_realized))
    c4.metric("Capital", fmt_inr(capital))

    st.markdown("---")
    st.subheader("Risk Limits")

    open_pos = len(positions)
    max_pos = 5
    capital_deployed = sum(abs(p.get("quantity", 0)) * p.get("avg_price", 0) for p in positions)
    max_loss_limit = capital * 0.02  # 2% daily loss limit

    col1, col2 = st.columns(2)
    col1.progress(open_pos / max_pos, text=f"Open Positions: {open_pos} / {max_pos}")
    loss_pct = max(0.0, min(1.0, abs(min(net_pnl, 0)) / max_loss_limit)) if max_loss_limit > 0 else 0.0
    col2.progress(loss_pct, text=f"Daily Loss Limit: {fmt_inr(abs(min(net_pnl, 0)))} / {fmt_inr(max_loss_limit)}")

    st.markdown("---")
    st.subheader("Order Activity")
    total_orders = len(orders)
    open_orders = sum(1 for o in orders if o.get("status") in ("PENDING", "OPEN"))
    completed = sum(1 for o in orders if o.get("status") == "COMPLETED")
    failed = sum(1 for o in orders if o.get("status") in ("FAILED", "REJECTED_BY_RISK", "REJECTED"))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Orders", total_orders)
    c2.metric("Open", open_orders)
    c3.metric("Completed", completed)
    c4.metric("Failed / Rejected", failed)

    if positions:
        st.markdown("---")
        st.subheader("Position Detail")
        rows = []
        for p in positions:
            unreal = p.get("unrealized_pnl", 0)
            real = p.get("realized_pnl", 0)
            rows.append({
                "Symbol": p["symbol"],
                "Qty": p["quantity"],
                "Avg Price": fmt_inr(p.get("avg_price", 0)),
                "Market Price": fmt_inr(p.get("market_price", 0)),
                "Unrealized PnL": fmt_inr(unreal),
                "Realized PnL": fmt_inr(real),
                "Total PnL": fmt_inr(unreal + real),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


elif page == "System Health":
    st.title("System Health")

    health = fetch("health") or {}

    if not health:
        st.error("Cannot reach the API.")
    else:
        for key, value in health.items():
            label = key.upper().replace("_", " ")
            if value in ("UP", "CONNECTED"):
                st.success(f"**{label}**: {value}")
            elif isinstance(value, (int, float)):
                st.info(f"**{label}**: {value}")
            else:
                st.error(f"**{label}**: {value}")

    st.markdown("---")
    st.caption(f"Last checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
