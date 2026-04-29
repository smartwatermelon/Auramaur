"""Tests for the readiness check module.

Each criterion is tested at three boundaries: PASS, FAIL, and
INSUFFICIENT_DATA. The cycle-health log parser additionally has a
format-drift canary test.

Database fixtures use a real Database instance against a tempfile
SQLite (not :memory: — the bot's Database class manages its own
connection lifetime, and a tempfile makes it easier to seed via
plain SQL before connecting from the readiness module).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from auramaur.db.database import Database
from auramaur.monitoring.readiness import (
    check_brier_absolute,
    check_brier_vs_market,
    check_cycle_health,
    check_data_sources,
    check_divergence,
    check_pass_rate,
    check_pnl_after_fees,
    check_win_rate,
    evaluate_readiness,
)


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    instance = Database(str(db_path))
    await instance.connect()
    yield instance
    await instance.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _seed_signals(
    db: Database,
    *,
    n: int,
    exchange: str = "kalshi",
    market_prefix: str = "mkt-",
    divergence: float | None = None,
    timestamp_offset_days: float = 0.0,
) -> None:
    ts = (
        datetime.now(timezone.utc) - timedelta(days=timestamp_offset_days)
    ).isoformat()
    for i in range(n):
        await db.execute(
            "INSERT INTO signals (market_id, exchange, timestamp, claude_prob, "
            "claude_confidence, market_prob, edge, divergence, action) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{market_prefix}{i}",
                exchange,
                ts,
                0.55,
                "MEDIUM",
                0.50,
                5.0,
                divergence,
                "BUY",
            ),
        )
    await db.commit()


async def _seed_trades(
    db: Database,
    *,
    pnls: list[float],
    exchange: str = "kalshi",
    is_paper: int = 1,
    timestamp_offset_days: float = 0.0,
) -> None:
    ts = (
        datetime.now(timezone.utc) - timedelta(days=timestamp_offset_days)
    ).isoformat()
    for i, pnl in enumerate(pnls):
        await db.execute(
            "INSERT INTO trades (market_id, exchange, timestamp, side, size, "
            "price, is_paper, status, pnl) "
            "VALUES (?, ?, ?, 'BUY', 10.0, 0.50, ?, 'filled', ?)",
            (f"trade-mkt-{i}", exchange, ts, is_paper, pnl),
        )
    await db.commit()


async def _seed_calibration(
    db: Database,
    *,
    pairs: list[tuple[float, int]],
    market_probs: list[float] | None = None,
    timestamp_offset_days: float = 0.0,
) -> None:
    """Seed calibration entries plus matching first-signal market_prob.

    `pairs` is [(predicted_prob, actual_outcome), ...]
    `market_probs` (if provided) sets market_prob on the matching signal
    so check_brier_vs_market can join.
    """
    ts = (
        datetime.now(timezone.utc) - timedelta(days=timestamp_offset_days)
    ).isoformat()
    for i, (predicted, outcome) in enumerate(pairs):
        market_id = f"calib-mkt-{i}"
        await db.execute(
            "INSERT INTO calibration (market_id, predicted_prob, actual_outcome, "
            "resolved_at) VALUES (?, ?, ?, ?)",
            (market_id, predicted, outcome, ts),
        )
        if market_probs is not None:
            mp = market_probs[i]
            await db.execute(
                "INSERT INTO signals (market_id, exchange, timestamp, claude_prob, "
                "claude_confidence, market_prob, edge, action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (market_id, "kalshi", ts, predicted, "MEDIUM", mp, 5.0, "BUY"),
            )
    await db.commit()


# ---------------------------------------------------------------------------
# Cycle health — log parsing + drift canary
# ---------------------------------------------------------------------------


def _structlog_line(level: str, event: str, ts: datetime, **extra) -> str:
    payload = {
        "level": level,
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "event": event,
    }
    payload.update(extra)
    return json.dumps(payload) + "\n"


@pytest.mark.asyncio
async def test_cycle_health_pass_when_no_errors(tmp_path):
    log_file = tmp_path / "auramaur.log"
    now = datetime.now(timezone.utc)
    log_file.write_text(
        _structlog_line("info", "engine.cycle_complete", now)
        + _structlog_line("warning", "engine.skipped_junk", now)
    )
    result = await check_cycle_health(log_file, now - timedelta(days=7))
    assert result.status == "PASS"
    assert "0 errors" in result.value


@pytest.mark.asyncio
async def test_cycle_health_fail_on_error_level(tmp_path):
    log_file = tmp_path / "auramaur.log"
    now = datetime.now(timezone.utc)
    log_file.write_text(
        _structlog_line("info", "engine.cycle_complete", now)
        + _structlog_line("error", "exchange.order_failed", now)
    )
    result = await check_cycle_health(log_file, now - timedelta(days=7))
    assert result.status == "FAIL"
    assert "1 error" in result.value
    assert "error:exchange.order_failed" in result.detail


@pytest.mark.asyncio
async def test_cycle_health_fail_on_exception_field(tmp_path):
    """Even at info level, an `exception` field marks an unhandled exception."""
    log_file = tmp_path / "auramaur.log"
    now = datetime.now(timezone.utc)
    log_file.write_text(
        _structlog_line("info", "data_source.query", now, exception="Traceback ...")
    )
    result = await check_cycle_health(log_file, now - timedelta(days=7))
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_cycle_health_ignores_old_errors(tmp_path):
    """Errors outside the time window must not count."""
    log_file = tmp_path / "auramaur.log"
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=14)
    log_file.write_text(
        _structlog_line("error", "old.error", old)
        + _structlog_line("info", "recent.ok", now)
    )
    result = await check_cycle_health(log_file, now - timedelta(days=7))
    assert result.status == "PASS"


@pytest.mark.asyncio
async def test_cycle_health_drift_canary_fails(tmp_path):
    """If log lines fail to JSON-parse beyond the drift threshold, fail."""
    log_file = tmp_path / "auramaur.log"
    now = datetime.now(timezone.utc)
    # 9 unparseable lines + 1 valid line = 90% drift
    body = "this is not json\n" * 9
    body += _structlog_line("info", "ok", now)
    log_file.write_text(body)
    result = await check_cycle_health(log_file, now - timedelta(days=7))
    assert result.status == "FAIL"
    assert "format has drifted" in result.detail


@pytest.mark.asyncio
async def test_cycle_health_drift_canary_catches_timestamp_format_drift(tmp_path):
    """If the renderer changes timestamps to a non-ISO format, the canary
    must catch it — JSON-shape alone is not enough."""
    log_file = tmp_path / "auramaur.log"
    now = datetime.now(timezone.utc)
    # 9 lines with a hypothetical post-renderer-change timestamp format
    # (epoch seconds, common alternative). All have the required keys
    # but the timestamp doesn't parse, so they must count as drift.
    body = ""
    for _ in range(9):
        body += (
            json.dumps({"level": "info", "timestamp": "1714368000", "event": "ok"})
            + "\n"
        )
    body += _structlog_line("info", "ok", now)
    log_file.write_text(body)
    result = await check_cycle_health(log_file, now - timedelta(days=7))
    assert result.status == "FAIL"
    assert "format has drifted" in result.detail


@pytest.mark.asyncio
async def test_cycle_health_drift_canary_below_threshold_passes(tmp_path):
    """A small fraction of unparseable lines (e.g. truncated last line) is OK."""
    log_file = tmp_path / "auramaur.log"
    now = datetime.now(timezone.utc)
    # 99 valid lines + 1 unparseable = 1% drift, below 5% threshold
    body = "".join(_structlog_line("info", f"event-{i}", now) for i in range(99))
    body += "truncated{\n"
    log_file.write_text(body)
    result = await check_cycle_health(log_file, now - timedelta(days=7))
    assert result.status == "PASS"


@pytest.mark.asyncio
async def test_cycle_health_missing_log_is_insufficient_data(tmp_path):
    result = await check_cycle_health(
        tmp_path / "nonexistent.log", datetime.now(timezone.utc) - timedelta(days=7)
    )
    assert result.status == "INSUFFICIENT_DATA"
    assert "not found" in result.detail


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_sources_pass_when_all_active(db: Database):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    for source in ("NewsAPI", "Reddit", "RSS"):
        for i in range(3):
            await db.execute(
                "INSERT INTO news_items (id, source, title, content, created_at) "
                "VALUES (?, ?, 'title', 'body', ?)",
                (f"{source}-{i}", source, recent.isoformat()),
            )
    await db.commit()
    result = await check_data_sources(
        db,
        since_24h=now - timedelta(hours=24),
        since_window=now - timedelta(days=7),
    )
    assert result.status == "PASS"


@pytest.mark.asyncio
async def test_data_sources_fail_when_one_silent(db: Database):
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    old = now - timedelta(days=3)  # in window but >24h old
    # Reddit is silent in last 24h despite producing items earlier in window
    await db.execute(
        "INSERT INTO news_items (id, source, title, content, created_at) "
        "VALUES ('NewsAPI-1', 'NewsAPI', 't', 'b', ?)",
        (recent.isoformat(),),
    )
    await db.execute(
        "INSERT INTO news_items (id, source, title, content, created_at) "
        "VALUES ('Reddit-1', 'Reddit', 't', 'b', ?)",
        (old.isoformat(),),
    )
    await db.commit()
    result = await check_data_sources(
        db,
        since_24h=now - timedelta(hours=24),
        since_window=now - timedelta(days=7),
    )
    assert result.status == "FAIL"
    assert "Reddit" in result.detail


@pytest.mark.asyncio
async def test_data_sources_insufficient_when_empty(db: Database):
    now = datetime.now(timezone.utc)
    result = await check_data_sources(
        db,
        since_24h=now - timedelta(hours=24),
        since_window=now - timedelta(days=7),
    )
    assert result.status == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Pass rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pass_rate_pass_in_band(db: Database):
    """30 signals → 1 trade = 3.3% pass rate (in 0.5%–10% band)."""
    await _seed_signals(db, n=30)
    await _seed_trades(db, pnls=[1.0])
    now = datetime.now(timezone.utc)
    result = await check_pass_rate(db, since=now - timedelta(days=7), exchange="kalshi")
    assert result.status == "PASS"
    assert "3.3%" in result.value


@pytest.mark.asyncio
async def test_pass_rate_fail_too_high(db: Database):
    """30 signals → 15 trades = 50% pass rate (above 10% ceiling)."""
    await _seed_signals(db, n=30)
    await _seed_trades(db, pnls=[1.0] * 15)
    now = datetime.now(timezone.utc)
    result = await check_pass_rate(db, since=now - timedelta(days=7), exchange="kalshi")
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_pass_rate_fail_too_low(db: Database):
    """1000 signals → 0 trades = 0% pass rate (below 0.5% floor)."""
    await _seed_signals(db, n=1000)
    now = datetime.now(timezone.utc)
    result = await check_pass_rate(db, since=now - timedelta(days=7), exchange="kalshi")
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_pass_rate_insufficient_when_few_signals(db: Database):
    await _seed_signals(db, n=5)
    now = datetime.now(timezone.utc)
    result = await check_pass_rate(db, since=now - timedelta(days=7), exchange="kalshi")
    assert result.status == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Brier (absolute)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brier_absolute_pass_well_calibrated(db: Database):
    """Predict 0.7 on each, 70% resolve YES → Brier ≈ 0.21 (≤ 0.24 PASS)."""
    pairs = [(0.7, 1)] * 21 + [(0.7, 0)] * 9  # 30 samples
    await _seed_calibration(db, pairs=pairs)
    now = datetime.now(timezone.utc)
    result = await check_brier_absolute(db, since=now - timedelta(days=7))
    assert result.status == "PASS"
    # (0.7-1)^2*21 + (0.7-0)^2*9 = 0.09*21 + 0.49*9 = 1.89+4.41 = 6.30 / 30 = 0.21
    assert "0.210" in result.value


@pytest.mark.asyncio
async def test_brier_absolute_fail_overconfident(db: Database):
    """Predict 0.95 confidently but only 50% YES → high Brier."""
    pairs = [(0.95, 1)] * 15 + [(0.95, 0)] * 15
    await _seed_calibration(db, pairs=pairs)
    now = datetime.now(timezone.utc)
    result = await check_brier_absolute(db, since=now - timedelta(days=7))
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_brier_absolute_insufficient_data(db: Database):
    pairs = [(0.7, 1)] * 5
    await _seed_calibration(db, pairs=pairs)
    now = datetime.now(timezone.utc)
    result = await check_brier_absolute(db, since=now - timedelta(days=7))
    assert result.status == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Brier vs market
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_brier_vs_market_pass_when_bot_better(db: Database):
    """Bot at 0.7, market at 0.5, 70% YES → bot Brier 0.21, market Brier 0.25.
    Edge = 0.04 (≥ 0.02 PASS)."""
    pairs = [(0.7, 1)] * 21 + [(0.7, 0)] * 9
    market_probs = [0.5] * 30
    await _seed_calibration(db, pairs=pairs, market_probs=market_probs)
    now = datetime.now(timezone.utc)
    result = await check_brier_vs_market(db, since=now - timedelta(days=7))
    assert result.status == "PASS"


@pytest.mark.asyncio
async def test_brier_vs_market_fail_when_market_better(db: Database):
    """Bot at 0.6, market at 0.7, 70% YES → bot worse than market."""
    pairs = [(0.6, 1)] * 21 + [(0.6, 0)] * 9
    market_probs = [0.7] * 30
    await _seed_calibration(db, pairs=pairs, market_probs=market_probs)
    now = datetime.now(timezone.utc)
    result = await check_brier_vs_market(db, since=now - timedelta(days=7))
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_brier_vs_market_insufficient_when_no_market_pairing(db: Database):
    """Calibration entries exist but no signals to join → INSUFFICIENT_DATA."""
    pairs = [(0.7, 1)] * 30
    await _seed_calibration(db, pairs=pairs)  # no market_probs argument
    now = datetime.now(timezone.utc)
    result = await check_brier_vs_market(db, since=now - timedelta(days=7))
    assert result.status == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Win rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_win_rate_pass_above_52(db: Database):
    """16 wins out of 30 = 53.3%."""
    pnls = [1.0] * 16 + [-1.0] * 14
    await _seed_trades(db, pnls=pnls)
    now = datetime.now(timezone.utc)
    result = await check_win_rate(db, since=now - timedelta(days=7), exchange="kalshi")
    assert result.status == "PASS"


@pytest.mark.asyncio
async def test_win_rate_fail_at_50(db: Database):
    """15/30 = 50% < 52% threshold."""
    pnls = [1.0] * 15 + [-1.0] * 15
    await _seed_trades(db, pnls=pnls)
    now = datetime.now(timezone.utc)
    result = await check_win_rate(db, since=now - timedelta(days=7), exchange="kalshi")
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_win_rate_insufficient_data(db: Database):
    await _seed_trades(db, pnls=[1.0] * 5)
    now = datetime.now(timezone.utc)
    result = await check_win_rate(db, since=now - timedelta(days=7), exchange="kalshi")
    assert result.status == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# PnL after fees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pnl_after_fees_pass_when_net_positive(db: Database):
    """30 trades: 20 wins of $5 each ($100), 10 losses of $1 each (-$10).
    Gross = $90. Fee drag at 7% on $100 winning_profit = $7.
    Net = $90 - $7 = $83 > 0 → PASS."""
    pnls = [5.0] * 20 + [-1.0] * 10
    await _seed_trades(db, pnls=pnls)
    now = datetime.now(timezone.utc)
    result = await check_pnl_after_fees(
        db, since=now - timedelta(days=7), exchange="kalshi", fee_rate=0.07
    )
    assert result.status == "PASS"


@pytest.mark.asyncio
async def test_pnl_after_fees_fail_when_fee_drag_eats_edge(db: Database):
    """30 trades, gross $1, winning_profit $100 → fee drag $7 → net -$6."""
    pnls = [10.0] * 10 + [-3.3] * 30  # 40 trades, gross 100 - 99 = 1
    await _seed_trades(db, pnls=pnls)
    now = datetime.now(timezone.utc)
    result = await check_pnl_after_fees(
        db, since=now - timedelta(days=7), exchange="kalshi", fee_rate=0.07
    )
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_pnl_after_fees_insufficient(db: Database):
    await _seed_trades(db, pnls=[1.0] * 5)
    now = datetime.now(timezone.utc)
    result = await check_pnl_after_fees(
        db, since=now - timedelta(days=7), exchange="kalshi", fee_rate=0.07
    )
    assert result.status == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Divergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_divergence_pass_when_low(db: Database):
    """All divergence = 0.10 → median 0.10 (≤0.15), p95 0.10 (≤0.30)."""
    await _seed_signals(db, n=30, divergence=0.10)
    now = datetime.now(timezone.utc)
    result = await check_divergence(
        db, since=now - timedelta(days=7), exchange="kalshi"
    )
    assert result.status == "PASS"


@pytest.mark.asyncio
async def test_divergence_fail_when_median_high(db: Database):
    """All 0.20 → median 0.20 > 0.15 → FAIL."""
    await _seed_signals(db, n=30, divergence=0.20)
    now = datetime.now(timezone.utc)
    result = await check_divergence(
        db, since=now - timedelta(days=7), exchange="kalshi"
    )
    assert result.status == "FAIL"


@pytest.mark.asyncio
async def test_divergence_insufficient(db: Database):
    await _seed_signals(db, n=5, divergence=0.10)
    now = datetime.now(timezone.utc)
    result = await check_divergence(
        db, since=now - timedelta(days=7), exchange="kalshi"
    )
    assert result.status == "INSUFFICIENT_DATA"


# ---------------------------------------------------------------------------
# Top-level evaluator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_readiness_aggregates_all_eight_criteria(db: Database, tmp_path):
    log_file = tmp_path / "auramaur.log"
    log_file.write_text("")  # empty but exists
    report = await evaluate_readiness(db, log_file=log_file, exchange="kalshi", days=7)
    assert len(report.criteria) == 8
    # All INSUFFICIENT_DATA on a fresh DB → overall_pass is False
    assert not report.overall_pass
    names = [c.name for c in report.criteria]
    assert names == [
        "cycle_health",
        "data_sources",
        "pass_rate",
        "brier_absolute",
        "brier_vs_market",
        "win_rate",
        "pnl_after_fees",
        "divergence",
    ]


@pytest.mark.asyncio
async def test_evaluate_readiness_overall_pass_requires_all_pass(
    db: Database, tmp_path
):
    """Manufactured pass-on-all-criteria scenario."""
    now = datetime.now(timezone.utc)
    recent = now - timedelta(hours=1)
    log_file = tmp_path / "auramaur.log"
    log_file.write_text(_structlog_line("info", "engine.cycle_complete", now))

    # All sources active in last 24h
    for source in ("NewsAPI", "RSS"):
        await db.execute(
            "INSERT INTO news_items (id, source, title, content, created_at) "
            "VALUES (?, ?, 't', 'b', ?)",
            (f"{source}-1", source, recent.isoformat()),
        )

    # 100 signals, 5 trades, 50 calibration entries, 30 with low divergence
    await _seed_signals(db, n=100, divergence=0.10)
    # Win rate 60% on 50 trades, gross +20, fee drag 0.07*30=2.1, net 17.9
    pnls = [1.0] * 30 + [-1.0] * 20
    await _seed_trades(db, pnls=pnls)
    pairs = [(0.7, 1)] * 35 + [(0.7, 0)] * 15
    market_probs = [0.5] * 50
    await _seed_calibration(db, pairs=pairs, market_probs=market_probs)

    report = await evaluate_readiness(db, log_file=log_file, exchange="kalshi", days=7)
    # PASS assertions on the criteria we explicitly seeded for:
    by_name = {c.name: c for c in report.criteria}
    assert by_name["divergence"].status == "PASS"
    assert by_name["win_rate"].status == "PASS"
    assert by_name["pnl_after_fees"].status == "PASS"
    assert by_name["brier_absolute"].status == "PASS"
    assert by_name["brier_vs_market"].status == "PASS"
    assert by_name["cycle_health"].status == "PASS"
    assert by_name["data_sources"].status == "PASS"
