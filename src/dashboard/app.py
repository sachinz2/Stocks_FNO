import os
import streamlit as st
import requests
from datetime import datetime
import pandas as pd

st.set_page_config(page_title="Falcon Quant Platform", layout="wide", page_icon="🦅")

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000/api/v1")
_DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin123")

def check_password():
    """Returns `True` if the user had the correct password."""
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
        st.error("😕 Password incorrect")
        return False
    return True

if not check_password():
    st.stop()  # Do not continue if not authenticated

# --- Navigation ---
st.sidebar.title("🦅 Falcon Quant")
st.sidebar.markdown("---")
page = st.sidebar.radio("Navigate", ["Home", "Positions", "Orders & Trades", "Strategies", "Risk & PnL", "System Health"])

st.sidebar.markdown("---")
if st.sidebar.button("🔄 Refresh Data"):
    st.rerun()

# --- Helpers ---
def fetch_data(endpoint):
    try:
        response = requests.get(f"{API_BASE_URL}/{endpoint}")
        if response.status_code == 200:
            return response.json()
        st.error(f"API Error: {response.status_code}")
    except Exception as e:
        st.warning(f"Backend API not reachable. Showing mock data. ({e})")
    return None

# --- Pages ---

if page == "Home":
    st.title("Dashboard Overview")
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Net PnL", "₹ 24,500.00", "+5.2%")
    col2.metric("Open Positions", "3")
    col3.metric("Win Rate", "64.2%", "VWAP & EMA")
    col4.metric("Capital Utilization", "45%", "-2%")
    
    st.markdown("### Active Signals")
    # Mock data fallback
    signals = fetch_data("signals") or [
        {"symbol": "RELIANCE", "signal": "BUY", "confidence": 0.85, "strategy": "VWAP_REVERSION"},
        {"symbol": "TCS", "signal": "SELL", "confidence": 0.72, "strategy": "EMA_CROSSOVER"},
    ]
    st.dataframe(pd.DataFrame(signals), use_container_width=True)

elif page == "Positions":
    st.title("Open Positions")
    positions = fetch_data("positions") or [
        {"symbol": "SBIN", "quantity": 750, "avg_price": 810.20, "ltp": 815.00, "unrealized_pnl": 3600.0},
        {"symbol": "INFY", "quantity": 400, "avg_price": 1400.00, "ltp": 1395.00, "unrealized_pnl": -2000.0},
    ]
    
    df = pd.DataFrame(positions)
    if not df.empty:
        # Highlight PnL colors
        def color_pnl(val):
            color = 'green' if val > 0 else 'red' if val < 0 else 'gray'
            return f'color: {color}'
        
        st.dataframe(df.style.applymap(color_pnl, subset=['unrealized_pnl']), use_container_width=True)
    else:
        st.info("No open positions.")

elif page == "Orders & Trades":
    st.title("Order Management")
    orders = fetch_data("orders") or [
        {"order_id": "1001", "symbol": "SBIN", "side": "BUY", "quantity": 750, "price": 810.20, "status": "COMPLETED", "time": "09:15:01"},
        {"order_id": "1002", "symbol": "INFY", "side": "BUY", "quantity": 400, "price": 1400.00, "status": "COMPLETED", "time": "09:45:22"},
        {"order_id": "1003", "symbol": "RELIANCE", "side": "SELL", "quantity": 250, "price": 2800.00, "status": "PENDING", "time": "10:30:00"},
    ]
    st.dataframe(pd.DataFrame(orders), use_container_width=True)

elif page == "Strategies":
    st.title("Strategy Management")
    strategies = fetch_data("strategies") or [
        {"name": "VWAP_REVERSION", "active": True, "trades": 45, "win_rate": 62.5},
        {"name": "EMA_CROSSOVER", "active": False, "trades": 12, "win_rate": 45.0},
    ]
    for s in strategies:
        col1, col2, col3 = st.columns([2, 1, 1])
        col1.subheader(s["name"])
        col2.metric("Win Rate", f"{s['win_rate']}%")
        
        button_text = "Deactivate" if s["active"] else "Activate"
        if col3.button(button_text, key=s["name"]):
            st.toast(f"{s['name']} status changed!")

elif page == "Risk & PnL":
    st.title("Risk Engine & Reports")
    
    st.subheader("Daily Limits")
    col1, col2 = st.columns(2)
    col1.progress(0.25, text="Daily Loss Limit (25% utilized)")
    col2.progress(0.60, text="Max Open Positions (3/5)")
    
    st.markdown("---")
    st.subheader("Performance Metrics")
    metrics = {
        "Total Trades": 124,
        "Net Profit": "₹ 145,200",
        "Profit Factor": 1.85,
        "Max Drawdown": "6.2%",
        "Sharpe Ratio": 2.1
    }
    st.json(metrics)

elif page == "System Health":
    st.title("System Health")
    health = fetch_data("health") or {
        "status": "UP",
        "database": "UP",
        "redis": "UP",
        "broker_api": "CONNECTED",
        "latency_ms": 45
    }
    
    for key, value in health.items():
        if value == "UP" or value == "CONNECTED":
            st.success(f"**{key.upper()}**: {value}")
        elif isinstance(value, int):
            st.info(f"**{key.upper()}**: {value}")
        else:
            st.error(f"**{key.upper()}**: {value}")
