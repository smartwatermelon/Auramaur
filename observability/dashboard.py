"""Auramaur live Streamlit dashboard — auto-refreshes every 30 s."""

import json
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


# ── Exchange API queries (ground truth) ──────────────────────────────────────


_MIN_POSITION_TOKENS = 0.01


@st.cache_data(ttl=60)
def _fetch_kalshi_account() -> tuple[float, list[dict]] | None:
    """Query Kalshi Portfolio API. Returns (cash_balance, positions) or None."""
    cfg = _settings.kalshi
    api_key = cfg.api_key or _settings.kalshi_api_key
    priv_key_path = cfg.private_key_path or _settings.kalshi_private_key_path
    if not api_key or not priv_key_path:
        return None
    from pathlib import Path

    if not Path(priv_key_path).exists():
        st.warning(f"Kalshi: private key not found at {priv_key_path}")
        return None
    try:
        from kalshi_python import KalshiClient as _KalshiSDK
        from kalshi_python import Configuration, PortfolioApi

        host = (
            "https://demo-api.kalshi.co/trade-api/v2"
            if cfg.environment == "demo"
            else "https://api.elections.kalshi.com/trade-api/v2"
        )
        configuration = Configuration(host=host)
        client = _KalshiSDK(configuration=configuration)
        client.set_kalshi_auth(key_id=api_key, private_key_path=priv_key_path)
        portfolio_api = PortfolioApi(client)

        # Kalshi balance is in cents
        bal_resp = portfolio_api.get_balance()
        cash = float(bal_resp.balance) / 100

        pos_resp = portfolio_api.get_positions_without_preload_content()
        try:
            data = json.loads(pos_resp.data)
        except (json.JSONDecodeError, TypeError):
            st.warning("Kalshi: could not parse positions response")
            return None
        positions = []
        for p in data.get("market_positions", []):
            if "position_fp" not in p:
                continue
            # position_fp: positive = long YES, negative = short NO
            pos_fp = float(p.get("position_fp") or 0)
            if pos_fp == 0:
                continue
            contracts = abs(pos_fp)
            exposure = float(p.get("market_exposure_dollars") or 0)
            positions.append(
                {
                    "market": p.get("ticker", ""),
                    "side": "NO" if pos_fp < 0 else "YES",
                    "contracts": round(contracts, 2),
                    "exposure": round(exposure, 2),
                    "avg_price": (
                        round(exposure / contracts, 4) if contracts > 0 else 0
                    ),
                }
            )
        return (cash, positions)
    except Exception as exc:
        st.warning(f"Kalshi API: {type(exc).__name__}: {exc}")
        return None


@st.cache_data(ttl=60)
def _fetch_polymarket_account() -> tuple[float, list[dict]] | None:
    """Query Polymarket CLOB API. Returns (collateral, positions) or None.

    Position reconstruction mirrors auramaur/exchange/client.py _load_real_positions():
    get_trades() on an authenticated ClobClient returns only this account's trades.
    Each trade is classified as maker (matched via proxy address in maker_orders) or
    taker (trader_side == TAKER). Net position per asset_id = sum(BUY) - sum(SELL).
    """
    if not (
        _settings.polymarket_api_key
        and _settings.polymarket_api_secret
        and _settings.polymarket_passphrase
        and _settings.polymarket_proxy_address
        and _settings.polygon_private_key
    ):
        return None
    try:
        from py_clob_client_v2 import (
            ApiCreds,
            AssetType,
            BalanceAllowanceParams,
            ClobClient,
        )

        proxy = _settings.polymarket_proxy_address
        creds = ApiCreds(
            api_key=_settings.polymarket_api_key,
            api_secret=_settings.polymarket_api_secret,
            api_passphrase=_settings.polymarket_passphrase,
        )
        clob = ClobClient(
            "https://clob.polymarket.com",
            chain_id=137,
            key=_settings.polygon_private_key,
            creds=creds,
            signature_type=3,
            funder=proxy,
        )

        resp = clob.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=3)
        )
        if not isinstance(resp, dict) or "balance" not in resp:
            st.warning("Polymarket balance query returned unexpected format")
            return None
        # Polymarket collateral is in µUSDC (micro-units)
        try:
            collateral = float(resp["balance"]) / 1e6
        except (ValueError, TypeError):
            st.warning("Polymarket: could not parse balance value")
            return None

        trades = clob.get_trades()
        net: dict[str, dict] = {}
        proxy_lower = proxy.lower()

        for t in trades or []:
            if t.get("status") != "CONFIRMED":
                continue
            asset_id = side = None
            size = 0.0

            # Maker-side: proxy address appears in maker_orders
            for mo in t.get("maker_orders", []):
                if mo.get("maker_address", "").lower() == proxy_lower:
                    asset_id = mo.get("asset_id")
                    side = mo.get("side")
                    size = float(mo.get("matched_amount") or 0)
                    break

            # Taker-side: account took the other side of the trade
            if not asset_id and t.get("trader_side") == "TAKER":
                asset_id = t.get("asset_id")
                side = t.get("side")
                size = float(t.get("size") or 0)

            if asset_id and side in ("BUY", "SELL") and size > 0:
                if asset_id not in net:
                    net[asset_id] = {
                        "market": t.get("market", ""),
                        "outcome": t.get("outcome", ""),
                        "net": 0.0,
                        "last_price": 0.0,
                    }
                if side == "BUY":
                    net[asset_id]["net"] += size
                else:
                    net[asset_id]["net"] -= size
                net[asset_id]["last_price"] = float(t.get("price") or 0)

        positions = []
        for _aid, pos in net.items():
            if abs(pos["net"]) < _MIN_POSITION_TOKENS:
                continue
            positions.append(
                {
                    "market": pos["market"],
                    "outcome": pos["outcome"],
                    "tokens": round(pos["net"], 2),
                    "last_price": round(pos["last_price"], 4),
                    "est_value": round(pos["net"] * pos["last_price"], 2),
                }
            )
        return (collateral, positions)
    except Exception as exc:
        st.warning(f"Polymarket API: {type(exc).__name__}: {exc}")
        return None


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

# ── Account Overview (exchange APIs) ─────────────────────────────────────────

kalshi_result = _fetch_kalshi_account()
poly_result = _fetch_polymarket_account()

if kalshi_result is not None or poly_result is not None:
    st.subheader("Account Overview")

    kalshi_total = 0.0
    poly_total = 0.0
    kalshi_cash = kalshi_pos_value = 0.0
    poly_collateral = poly_pos_value = 0.0
    kalshi_positions: list[dict] = []
    poly_positions: list[dict] = []

    if kalshi_result is not None:
        kalshi_cash, kalshi_positions = kalshi_result
        kalshi_pos_value = sum(p["exposure"] for p in kalshi_positions)
        kalshi_total = kalshi_cash + kalshi_pos_value

    if poly_result is not None:
        poly_collateral, poly_positions = poly_result
        poly_pos_value = sum(p["est_value"] for p in poly_positions)
        poly_total = poly_collateral + poly_pos_value

    combined = kalshi_total + poly_total

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Portfolio", f"${combined:,.2f}")
    if kalshi_result is not None:
        c2.metric("Kalshi", f"${kalshi_total:,.2f}")
    else:
        c2.metric("Kalshi", "N/A", help="No Kalshi API credentials configured")
    if poly_result is not None:
        c3.metric("Polymarket", f"${poly_total:,.2f}")
    else:
        c3.metric("Polymarket", "N/A", help="No Polymarket API credentials configured")

    acct_left, acct_right = st.columns(2)

    with acct_left:
        if kalshi_result is not None and kalshi_positions:
            st.caption(
                f"Kalshi — Cash: ${kalshi_cash:,.2f} · "
                f"{len(kalshi_positions)} positions"
            )
            st.dataframe(
                pd.DataFrame(kalshi_positions),
                use_container_width=True,
                hide_index=True,
            )
        elif kalshi_result is not None:
            st.caption(f"Kalshi — Cash: ${kalshi_cash:,.2f} · No open positions")

    with acct_right:
        if poly_result is not None and poly_positions:
            st.caption(
                f"Polymarket — Collateral: ${poly_collateral:,.2f} · "
                f"{len(poly_positions)} positions"
            )
            st.dataframe(
                pd.DataFrame(poly_positions),
                use_container_width=True,
                hide_index=True,
            )
        elif poly_result is not None:
            st.caption(
                f"Polymarket — Collateral: ${poly_collateral:,.2f} · No open positions"
            )

    st.divider()

# ── Bot-tracked metrics ──────────────────────────────────────────────────────

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

# ── Bot-tracked positions ─────────────────────────────────────────────────────

left, right = st.columns([3, 2])

with left:
    st.subheader("Bot-Tracked Positions")
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
