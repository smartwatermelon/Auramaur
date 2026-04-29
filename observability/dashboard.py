"""Auramaur live Streamlit dashboard — auto-refreshes every 30 s."""

import sqlite3
import sys
import time
import urllib.parse
from contextlib import closing
from pathlib import Path

import pandas as pd
import streamlit as st

# Make project importable from the observability/ subdirectory
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import Settings  # noqa: E402  (after sys.path tweak)

_settings = Settings()
PAPER_INITIAL_BALANCE: float = _settings.execution.paper_initial_balance
IS_PAPER: int = 0 if _settings.is_live else 1
_db_abs = (Path(__file__).parent.parent / "auramaur.db").resolve()
DB_URI = f"file:{urllib.parse.quote(str(_db_abs))}?mode=ro"
DB_PATH = _db_abs
REFRESH_SECONDS = 30


def connect() -> sqlite3.Connection:
    """Fresh read-only connection per rerun so each refresh sees latest WAL state."""
    conn = sqlite3.connect(DB_URI, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch(conn: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


# ── Layout ────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Auramaur",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)
mode_label = "LIVE" if _settings.is_live else "PAPER"
st.title(f"📈 Auramaur — {mode_label} Dashboard")

with closing(connect()) as conn:
    # ── Top-line metrics ──────────────────────────────────────────────────────────

    if _settings.is_live:
        st.info(
            "Live mode: cash balance must be queried from the exchange. "
            "Positions and signals below are from the local DB."
        )
    else:
        cash_df = fetch(
            conn,
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
        cash_flow = float(cash_df["cash_flow"].iloc[0])
        trade_count = int(cash_df["trade_count"].iloc[0])
        balance = PAPER_INITIAL_BALANCE + cash_flow

        pos_df = fetch(
            conn,
            """
            SELECT
              COUNT(*) AS open_count,
              COALESCE(SUM((current_price - avg_price) * size), 0) AS unrealized_pnl,
              COALESCE(SUM(size * current_price), 0) AS market_value
            FROM portfolio
            WHERE is_paper = 1 AND size > 0
            """,
        )
        open_count = int(pos_df["open_count"].iloc[0])
        unrealized_pnl = float(pos_df["unrealized_pnl"].iloc[0])
        market_value = float(pos_df["market_value"].iloc[0])
        total_equity = balance + market_value
        equity_delta = total_equity - PAPER_INITIAL_BALANCE

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
        pos_table = fetch(
            conn,
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
        )
        if pos_table.empty:
            st.info("No open positions.")
        else:
            try:
                styled = pos_table.style.format(
                    {
                        "avg_price": "{:.4f}",
                        "current_price": "{:.4f}",
                        "unr_pnl": "${:+.2f}",
                        "mkt_value": "${:.2f}",
                    },
                    na_rep="—",
                )
                st.dataframe(styled, use_container_width=True, hide_index=True)
            except KeyError as exc:
                st.warning(f"Styling skipped — missing column: {exc}")
                st.dataframe(pos_table, use_container_width=True, hide_index=True)

    # ── Recent signals ────────────────────────────────────────────────────────────
    # signals table has no is_paper column; signals are exchange-scoped, not mode-scoped.

    with right:
        st.subheader("Recent Signals (top edge)")
        sig_df = fetch(
            conn,
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
        )
        if sig_df.empty:
            st.info("No signals yet.")
        else:
            st.dataframe(sig_df, use_container_width=True, hide_index=True)

    st.divider()

    # ── Cumulative cash-flow chart ────────────────────────────────────────────────

    st.subheader("Cumulative Cash Flow (paper trades)")
    if _settings.is_live:
        st.info("Cash flow chart available in paper mode only.")
    else:
        trades_df = fetch(
            conn,
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
            trades_df["cumulative"] = (
                PAPER_INITIAL_BALANCE + trades_df["cash_delta"].cumsum()
            )
            trades_df["timestamp"] = pd.to_datetime(trades_df["timestamp"])
            chart_df = trades_df.set_index("timestamp")[["cumulative"]].rename(
                columns={"cumulative": "Cash Balance ($)"}
            )
            st.line_chart(chart_df, use_container_width=True)

    # ── Trade log ─────────────────────────────────────────────────────────────────

    with st.expander("Trade Log (last 100)"):
        tlog = fetch(
            conn,
            """
            SELECT
              datetime(timestamp, 'localtime') AS time,
              market_id,
              exchange,
              side,
              ROUND(size, 2)         AS tokens,
              ROUND(price, 4)        AS price,
              ROUND(size * price, 2) AS cost_usd,
              status,
              order_id
            FROM trades
            WHERE is_paper = ?
            ORDER BY timestamp DESC
            LIMIT 100
            """,
            (IS_PAPER,),
        )
        st.dataframe(tlog, use_container_width=True, hide_index=True)

# ── Footer / auto-refresh countdown ──────────────────────────────────────────

footer = st.empty()
for remaining in range(REFRESH_SECONDS, 0, -1):
    footer.caption(f"Refreshing in {remaining}s · {mode_label} mode · DB: {DB_PATH}")
    time.sleep(1)
st.rerun()
