"""
Streamlit Dashboard — localhost UI
Run: streamlit run src/dashboard/app.py
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from config.settings import DB_PATH, WATCH_SYMBOLS
from src.models.schema import init_db


def _get_db_symbols() -> list:
    """Pull actual symbols present in DB rather than relying on config."""
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT symbol FROM option_chain_snapshots ORDER BY symbol")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows if rows else WATCH_SYMBOLS
    except Exception:
        return WATCH_SYMBOLS

st.set_page_config(
    page_title="NSEBOT Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def _ensure_db_ready() -> bool:
    init_db()
    return True


_ensure_db_ready()

# ── DB helpers ────────────────────────────────────────────────────────────

@st.cache_resource
def _conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, _conn(), params=params)


# ── Sidebar ───────────────────────────────────────────────────────────────

st.sidebar.title("⚙️ NSEBOT")
symbol   = st.sidebar.selectbox("Symbol", _get_db_symbols())
lookback = st.sidebar.slider("History (hours)", 1, 48, 6)
refresh  = st.sidebar.button("🔄 Refresh")

if refresh:
    st.cache_data.clear()

st.title(f"📊 NSEBOT — {symbol} Option Chain Monitor")

# ── Latest snapshot metadata ──────────────────────────────────────────────

meta = _query(
    "SELECT MAX(fetched_at) AS last_fetch, COUNT(DISTINCT fetched_at) AS snapshots "
    "FROM option_chain_snapshots WHERE symbol=?",
    (symbol,),
)
col1, col2 = st.columns(2)
col1.metric("Last Fetch (UTC)", meta["last_fetch"].iloc[0] or "—")
col2.metric("Total Snapshots", int(meta["snapshots"].iloc[0] or 0))

st.divider()

# ── Underlying price trend ────────────────────────────────────────────────

cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback)).isoformat()
price_df = _query(
    "SELECT fetched_at, price FROM underlying_price "
    "WHERE symbol=? AND fetched_at>=? ORDER BY fetched_at",
    (symbol, cutoff),
)

if not price_df.empty:
    price_df["fetched_at"] = pd.to_datetime(price_df["fetched_at"])
    fig_price = px.line(price_df, x="fetched_at", y="price",
                        title=f"{symbol} Spot Price Trend",
                        labels={"fetched_at": "Time (UTC)", "price": "Spot Price"})
    fig_price.update_layout(height=300)
    st.plotly_chart(fig_price, use_container_width=True)

st.divider()

# ── OI Heatmap (latest snapshot) ─────────────────────────────────────────

st.subheader("📦 Open Interest Heatmap — Latest Snapshot")

latest_snap = _query(
    """
    SELECT strike, option_type, oi, ltp, iv, oi_change
    FROM option_chain_snapshots
    WHERE symbol=? AND fetched_at=(
        SELECT MAX(fetched_at) FROM option_chain_snapshots WHERE symbol=?
    )
    ORDER BY strike
    """,
    (symbol, symbol),
)

if not latest_snap.empty:
    ce_df = latest_snap[latest_snap["option_type"] == "CE"].set_index("strike")
    pe_df = latest_snap[latest_snap["option_type"] == "PE"].set_index("strike")

    oi_pivot = pd.DataFrame({
        "CE OI":  ce_df["oi"],
        "PE OI":  pe_df["oi"],
        "CE OI Δ": ce_df["oi_change"],
        "PE OI Δ": pe_df["oi_change"],
    }).fillna(0).astype(int)

    # Bar chart
    fig_oi = go.Figure()
    fig_oi.add_bar(x=oi_pivot.index, y=oi_pivot["CE OI"], name="CE OI",
                   marker_color="rgba(239,83,80,0.7)")
    fig_oi.add_bar(x=oi_pivot.index, y=oi_pivot["PE OI"], name="PE OI",
                   marker_color="rgba(38,166,154,0.7)")
    fig_oi.update_layout(
        barmode="group", height=400,
        xaxis_title="Strike", yaxis_title="Open Interest",
        title="CE vs PE Open Interest by Strike",
    )
    st.plotly_chart(fig_oi, use_container_width=True)

    # OI Change heatmap
    st.subheader("📐 OI Change (vs prev snapshot)")
    fig_delta = go.Figure()
    fig_delta.add_bar(x=oi_pivot.index, y=oi_pivot["CE OI Δ"], name="CE OI Δ",
                      marker_color="rgba(239,83,80,0.5)")
    fig_delta.add_bar(x=oi_pivot.index, y=oi_pivot["PE OI Δ"], name="PE OI Δ",
                      marker_color="rgba(38,166,154,0.5)")
    fig_delta.update_layout(barmode="group", height=300,
                            xaxis_title="Strike", yaxis_title="OI Change")
    st.plotly_chart(fig_delta, use_container_width=True)

st.divider()

# ── PCR Trend ─────────────────────────────────────────────────────────────

st.subheader("🔄 PCR Trend")

pcr_raw = _query(
    """
    SELECT fetched_at,
           SUM(CASE WHEN option_type='PE' THEN oi ELSE 0 END) * 1.0 /
           NULLIF(SUM(CASE WHEN option_type='CE' THEN oi ELSE 0 END),0) AS pcr
    FROM option_chain_snapshots
    WHERE symbol=? AND fetched_at>=?
    GROUP BY fetched_at
    ORDER BY fetched_at
    """,
    (symbol, cutoff),
)

if not pcr_raw.empty:
    pcr_raw["fetched_at"] = pd.to_datetime(pcr_raw["fetched_at"])
    fig_pcr = px.line(pcr_raw, x="fetched_at", y="pcr",
                      title="PCR over Time",
                      labels={"fetched_at": "Time (UTC)", "pcr": "PCR"})
    fig_pcr.add_hline(y=1.5, line_dash="dot", line_color="red",   annotation_text="Bearish Extreme")
    fig_pcr.add_hline(y=0.5, line_dash="dot", line_color="green", annotation_text="Bullish Extreme")
    fig_pcr.update_layout(height=300)
    st.plotly_chart(fig_pcr, use_container_width=True)

st.divider()

# ── Alert History ─────────────────────────────────────────────────────────

st.subheader("🔔 Alert History")

alerts_df = _query(
    "SELECT fired_at, alert_type, strike, option_type, expiry, telegram_sent, detail_json "
    "FROM anomaly_alerts WHERE symbol=? ORDER BY fired_at DESC LIMIT 100",
    (symbol,),
)

if alerts_df.empty:
    st.info("No alerts yet for this symbol.")
else:
    def parse_detail(js: str) -> str:
        try:
            d = json.loads(js)
            return " | ".join(f"{k}={v}" for k, v in list(d.items())[:4])
        except Exception:
            return js

    alerts_df["detail"] = alerts_df["detail_json"].apply(parse_detail)
    alerts_df["📨"] = alerts_df["telegram_sent"].map({1: "✅", 0: "⏳"})
    st.dataframe(
        alerts_df[["fired_at", "alert_type", "strike", "option_type", "expiry", "📨", "detail"]],
        use_container_width=True,
        hide_index=True,
    )

"""
Deprecated: NSEBOT no longer uses Streamlit for its dashboard.
Use the FastAPI + plain HTML dashboard served by `dashboard_server.py`.

Run:
  python dashboard_server.py

Open:
  http://localhost:8080/
"""
