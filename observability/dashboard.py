"""Auramaur live Streamlit dashboard — auto-refreshes every 30 s."""

import os
import sqlite3
import sys
import time
import urllib.parse
from contextlib import closing
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import Settings  # noqa: E402

_settings = Settings()
PAPER_INITIAL_BALANCE: float = _settings.execution.paper_initial_balance
_dashboard_mode = os.environ.get("AURAMAUR_DASHBOARD_MODE", "").lower()
_is_live = _dashboard_mode == "live" if _dashboard_mode else _settings.is_live
IS_PAPER: int = 0 if _is_live else 1
REFRESH_SECONDS = 30


def _discover_dbs() -> list[tuple[str, Path]]:
    data_dir = Path.home() / "Library" / "Application Support" / "auramaur"
    dbs = []
    for db_file in sorted(data_dir.glob("auramaur-*.db")):
        exchange = db_file.stem.replace("auramaur-", "")
        dbs.append((exchange, db_file))
    if not dbs:
        env_db = os.environ.get("AURAMAUR_DB")
        if env_db and Path(env_db).is_file():
            dbs.append(("legacy", Path(env_db)))
        else:
            legacy = Path(__file__).parent.parent / "auramaur.db"
            if legacy.is_file():
                dbs.append(("legacy", legacy))
    return dbs


@st.cache_data(ttl=60)
def _cached_discover_dbs() -> list[tuple[str, str]]:
    return [(name, str(path)) for name, path in _discover_dbs()]


def fetch_all(sql: str, params=(), *, tag_exchange: bool = False) -> pd.DataFrame:
    db_sources = _cached_discover_dbs()
    frames = []
    for exchange, db_path_str in db_sources:
        db_name = Path(db_path_str).name
        uri = f"file:{urllib.parse.quote(db_path_str)}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, check_same_thread=False)) as conn:
            try:
                df = pd.read_sql_query(sql, conn, params=params)
                if not df.empty:
                    if tag_exchange:
                        df.insert(0, "exchange", exchange)
                    frames.append(df)
            except sqlite3.DatabaseError as exc:
                st.warning(f"Failed to query {db_name}: {exc}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ── Layout ────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Auramaur",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)
mode_label = "LIVE" if _is_live else "PAPER"
st.title(f"📈 Auramaur — {mode_label} Dashboard")

if not _cached_discover_dbs():
    st.error("No databases found. Run the bot at least once first.")
    st.stop()

# ── Top-line metrics ──────────────────────────────────────────────────────────

pos_df = fetch_all(
    """
    SELECT
      COUNT(*) AS open_count,
      COALESCE(SUM((current_price - avg_price) * size), 0) AS unrealized_pnl,
      COALESCE(SUM(size * current_price), 0) AS market_value
    FROM portfolio
    WHERE is_paper = ? AND size > 0
    """,
    (IS_PAPER,),
)
open_count = int(pos_df["open_count"].sum()) if not pos_df.empty else 0
unrealized_pnl = float(pos_df["unrealized_pnl"].sum()) if not pos_df.empty else 0.0
market_value = float(pos_df["market_value"].sum()) if not pos_df.empty else 0.0

if _is_live:
    col1, col2, col3 = st.columns(3)
    col1.metric("Market Value", f"${market_value:,.2f}")
    col2.metric("Unrealized P&L", f"${unrealized_pnl:+,.2f}")
    col3.metric("Open Positions", str(open_count))
else:
    # Each exchange bot starts with its own paper balance
    initial_balance = len(_cached_discover_dbs()) * PAPER_INITIAL_BALANCE
    cash_df = fetch_all(
        """
        SELECT
          COALESCE(
            SUM(CASE WHEN side='BUY' THEN -size*price ELSE size*price END),
            0
          ) AS cash_flow,
          COUNT(*) AS trade_count
        FROM trades
        WHERE is_paper = 1 AND status = 'filled'
        """,
    )
    cash_flow = float(cash_df["cash_flow"].sum()) if not cash_df.empty else 0.0
    trade_count = int(cash_df["trade_count"].sum()) if not cash_df.empty else 0
    balance = initial_balance + cash_flow
    total_equity = balance + market_value
    equity_delta = total_equity - initial_balance

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Cash Balance", f"${balance:,.2f}")
    col2.metric("Market Value", f"${market_value:,.2f}")
    col3.metric("Total Equity", f"${total_equity:,.2f}", f"${equity_delta:+,.2f}")
    col4.metric("Unrealized P&L", f"${unrealized_pnl:+,.2f}")
    col5.metric("Open Positions", str(open_count))
    st.caption(f"{trade_count} filled paper trades recorded")

st.divider()

# ── Open positions ────────────────────────────────────────────────────────────

left, right = st.columns([3, 2])

with left:
    st.subheader("Open Positions")
    pos_table = fetch_all(
        """
        SELECT
          market_id,
          token,
          side,
          ROUND(size, 2)          AS tokens,
          ROUND(avg_price, 4)     AS avg_price,
          ROUND(current_price, 4) AS current_price,
          ROUND((current_price - avg_price) * size, 2) AS unr_pnl,
          ROUND(size * current_price, 2)               AS mkt_value,
          category,
          updated_at
        FROM portfolio
        WHERE is_paper = ? AND size > 0
        ORDER BY ABS((current_price - avg_price) * size) DESC
        """,
        (IS_PAPER,),
        tag_exchange=True,
    )
    if pos_table.empty:
        st.info("No open positions.")
    else:
        # Re-sort after concat: SQL orders within each DB, Python merges globally
        pos_table = pos_table.sort_values(
            "unr_pnl", key=lambda s: s.abs(), ascending=False
        )
        try:
            styled = pos_table.style.format(
                {
                    "avg_price": "{:.4f}",
                    "current_price": "{:.4f}",
                    "unr_pnl": "${:+.2f}",
                    "mkt_value": "${:.2f}",
                },
                na_rep="---",
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)
        except KeyError as exc:
            st.warning(f"Styling skipped — missing column: {exc}")
            st.dataframe(pos_table, use_container_width=True, hide_index=True)

# ── Recent signals ────────────────────────────────────────────────────────────

with right:
    st.subheader("Recent Signals (top edge)")
    sig_df = fetch_all(
        """
        SELECT
          datetime(timestamp, 'localtime') AS time,
          market_id,
          ROUND(claude_prob * 100, 1)  AS claude,
          ROUND(market_prob * 100, 1)  AS market,
          ROUND(edge, 1)               AS edge,
          claude_confidence            AS conf,
          action
        FROM signals
        ORDER BY ABS(edge) DESC
        LIMIT 20
        """,
        tag_exchange=True,
    )
    if sig_df.empty:
        st.info("No signals yet.")
    else:
        # Re-sort after concat: SQL orders within each DB, Python merges globally
        sig_df = sig_df.sort_values(
            "edge", key=lambda s: s.abs(), ascending=False
        ).head(20)
        st.dataframe(sig_df, use_container_width=True, hide_index=True)

st.divider()

# ── Cumulative cash-flow chart ────────────────────────────────────────────────

st.subheader("Cumulative Cash Flow (paper trades)")
if _is_live:
    st.info("Cash flow chart available in paper mode only.")
else:
    trades_df = fetch_all(
        """
        SELECT
          timestamp,
          market_id,
          side,
          ROUND(size, 2)  AS tokens,
          ROUND(price, 4) AS price
        FROM trades
        WHERE is_paper = 1 AND status = 'filled'
        ORDER BY timestamp
        """,
    )
    if trades_df.empty:
        st.info("No filled trades recorded yet.")
    else:
        trades_df["cash_delta"] = trades_df.apply(
            lambda r: (
                -r["tokens"] * r["price"]
                if r["side"] == "BUY"
                else r["tokens"] * r["price"]
            ),
            axis=1,
        )
        trades_df = trades_df.sort_values("timestamp")
        chart_initial = len(_cached_discover_dbs()) * PAPER_INITIAL_BALANCE
        trades_df["cumulative"] = chart_initial + trades_df["cash_delta"].cumsum()
        trades_df["timestamp"] = pd.to_datetime(trades_df["timestamp"])
        chart_df = trades_df.set_index("timestamp")[["cumulative"]].rename(
            columns={"cumulative": "Cash Balance ($)"}
        )
        st.line_chart(chart_df, use_container_width=True)

# ── Trade log ─────────────────────────────────────────────────────────────────

with st.expander("Trade Log (last 100)"):
    tlog = fetch_all(
        """
        SELECT
          datetime(timestamp, 'localtime') AS time,
          market_id,
          side,
          ROUND(size, 2)         AS tokens,
          ROUND(price, 4)        AS price,
          ROUND(size * price, 2) AS cost_usd,
          status,
          order_id
        FROM trades
        WHERE is_paper = ?
        ORDER BY timestamp DESC
        LIMIT 200
        """,
        (IS_PAPER,),
        tag_exchange=True,
    )
    if not tlog.empty:
        tlog = tlog.sort_values("time", ascending=False).head(100)
    st.dataframe(tlog, use_container_width=True, hide_index=True)

# ── Footer / auto-refresh countdown ──────────────────────────────────────────

db_names = ", ".join(name for name, _ in _cached_discover_dbs()) or "none"
footer = st.empty()
for remaining in range(REFRESH_SECONDS, 0, -1):
    footer.caption(f"Refreshing in {remaining}s · {mode_label} mode · DBs: {db_names}")
    time.sleep(1)
st.rerun()
