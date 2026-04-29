"""Readiness checks — gate trading goes live until all criteria pass.

The eight criteria are documented in `docs/plans/2026-04-28-deployment-plan.md` §4.
Each criterion produces a CriterionResult with one of three statuses:
  PASS              — measurable and within threshold
  FAIL              — measurable and outside threshold
  INSUFFICIENT_DATA — not enough samples to evaluate honestly

Overall readiness passes only if every criterion is PASS. INSUFFICIENT_DATA
counts as not-ready: the bot must accumulate enough resolved trades before
it can authorize a real-money flip.

Two criteria use proxies because the bot does not currently persist the
underlying signal:

  cycle_health  — parsed from auramaur.log (structlog JSON-line output).
                  Has a format-drift canary that fails the criterion if
                  more than 5% of lines are unparseable, so silent drift
                  in the log format produces a loud failure rather than
                  a falsely-clean readiness report.

  data_sources  — proxied by counts in the news_items table grouped by
                  source. A source that produced items in the 7-day
                  window but produced zero in the last 24h is flagged as
                  silent. This is "items produced", not "queries
                  succeeded" — a real outage where queries hard-fail but
                  cached items are still in the DB would not be caught.

Both proxies should eventually be replaced by direct instrumentation;
that is tracked as a future-work finding rather than blocking Phase 1.

Brier scoping note: the `calibration` table does not have an `exchange`
column (see auramaur/db/models.py — only market_id, predicted_prob,
actual_outcome, resolved_at, category, created_at). Both Brier
criteria therefore evaluate the bot's accuracy *globally* across every
exchange the bot has traded on, regardless of the `exchange` argument
passed to evaluate_readiness. For Phase 1 (Kalshi only) this is
equivalent to "Kalshi Brier"; for multi-exchange operation this would
need a schema migration to add `exchange` to calibration so each Brier
criterion can be scoped per-exchange. Tracked as a Phase 3+ prereq.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from auramaur.db.database import Database

Status = Literal["PASS", "FAIL", "INSUFFICIENT_DATA"]


@dataclass
class CriterionResult:
    name: str
    status: Status
    value: str
    threshold: str
    detail: str = ""
    n_samples: int | None = None


@dataclass
class ReadinessReport:
    timestamp: datetime
    exchange: str | None
    window_days: int
    criteria: list[CriterionResult] = field(default_factory=list)

    @property
    def overall_pass(self) -> bool:
        return bool(self.criteria) and all(c.status == "PASS" for c in self.criteria)


# ---------------------------------------------------------------------------
# Criterion 1 — cycle health (log parsing with format-drift canary)
# ---------------------------------------------------------------------------

# Patterns considered an "unhandled exception" in the structlog JSON-line
# output. The renderer is configured in auramaur/monitoring/logger.py with:
#   structlog.processors.add_log_level
#   structlog.processors.TimeStamper(fmt="iso")
#   structlog.processors.format_exc_info
# This gives every entry a `level` (lowercase), `timestamp` (ISO 8601), and
# `event` (the structlog event name). Exceptions get an `exception` key
# from format_exc_info.
_REQUIRED_KEYS = ("level", "timestamp", "event")
_ERROR_LEVELS = {"error", "critical"}


def _parse_log_for_errors(
    log_file: Path,
    since: datetime,
    sample_events_to_keep: int,
) -> tuple[int, int, int, int, list[str]]:
    """Synchronous log parser. Returns (total, well_formed, in_window,
    errors, error_events). Called via asyncio.to_thread from
    check_cycle_health to avoid blocking the event loop on large logs.
    """
    total = 0
    well_formed = 0
    in_window = 0
    errors = 0
    error_events: list[str] = []

    with log_file.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            total += 1
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if not all(k in entry for k in _REQUIRED_KEYS):
                continue
            try:
                ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                # Timestamp format drifted — count as drift, not as well-formed.
                continue
            # Increment well_formed AFTER successful timestamp parse so
            # the canary at the call site catches timestamp-format drift
            # too, not just JSON-shape drift.
            well_formed += 1
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since:
                continue
            in_window += 1
            level = str(entry.get("level", "")).lower()
            if level in _ERROR_LEVELS or "exception" in entry:
                errors += 1
                if len(error_events) < sample_events_to_keep:
                    event = entry.get("event", "")
                    error_events.append(f"{level}:{event}")
    return total, well_formed, in_window, errors, error_events


async def check_cycle_health(
    log_file: Path,
    since: datetime,
    *,
    drift_threshold_pct: float = 5.0,
    sample_events_to_keep: int = 5,
) -> CriterionResult:
    """Parse the structlog JSON-line log for ERROR/CRITICAL entries since `since`.

    Format-drift canary: if more than `drift_threshold_pct` of non-empty
    lines either fail to JSON-parse, are missing the required keys, or
    have an unparseable timestamp, the log format has drifted and the
    parser is unreliable. That returns FAIL with "format drift" as the
    reason — better to get a loud failure than a quietly-incorrect
    readiness report.

    Runs the parse loop in a worker thread (asyncio.to_thread) so a
    large log file does not stall the event loop while readiness is
    invoked alongside the running bot.
    """
    if not log_file.exists():
        return CriterionResult(
            name="cycle_health",
            status="INSUFFICIENT_DATA",
            value="—",
            threshold="0 errors",
            detail=(
                f"log file not found at {log_file.resolve()} — "
                "run `auramaur readiness` from the project root, or pass "
                "an explicit --log-file"
            ),
        )

    import asyncio

    total, well_formed, in_window, errors, error_events = await asyncio.to_thread(
        _parse_log_for_errors, log_file, since, sample_events_to_keep
    )

    if total == 0:
        return CriterionResult(
            name="cycle_health",
            status="INSUFFICIENT_DATA",
            value="—",
            threshold="0 errors",
            detail="log file is empty",
        )

    drift_pct = ((total - well_formed) / total) * 100.0
    if drift_pct > drift_threshold_pct:
        return CriterionResult(
            name="cycle_health",
            status="FAIL",
            value=f"{drift_pct:.1f}% unparseable",
            threshold=f"≤{drift_threshold_pct:.1f}% unparseable",
            detail=(
                "log format has drifted; "
                "readiness parser may be unreliable — investigate before relying on this criterion"
            ),
        )

    if errors == 0:
        return CriterionResult(
            name="cycle_health",
            status="PASS",
            value="0 errors",
            threshold="0 errors",
            detail=f"{in_window} entries in window",
        )

    return CriterionResult(
        name="cycle_health",
        status="FAIL",
        value=f"{errors} error/critical events",
        threshold="0 errors",
        detail="; ".join(error_events),
    )


# ---------------------------------------------------------------------------
# Criterion 2 — data sources (news_items proxy for source health)
# ---------------------------------------------------------------------------


async def check_data_sources(
    db: Database,
    *,
    since_24h: datetime,
    since_window: datetime,
) -> CriterionResult:
    """Flag any source that produced items in the window but zero in the
    last 24 hours.

    Uses the news_items.created_at timestamp (when the bot persisted the
    item), not published_at (which can be older than the bot's own
    history). created_at gives a fair "did this source talk to us
    recently" signal.
    """
    rows_window = await db.fetchall(
        "SELECT source, COUNT(*) AS n FROM news_items "
        "WHERE created_at >= ? GROUP BY source",
        (since_window.isoformat(),),
    )
    rows_24h = await db.fetchall(
        "SELECT source, COUNT(*) AS n FROM news_items "
        "WHERE created_at >= ? GROUP BY source",
        (since_24h.isoformat(),),
    )
    counts_window = {r["source"]: r["n"] for r in rows_window}
    counts_24h = {r["source"]: r["n"] for r in rows_24h}

    if not counts_window:
        return CriterionResult(
            name="data_sources",
            status="INSUFFICIENT_DATA",
            value="0 sources active",
            threshold="all enabled sources active in last 24h",
            detail="no news_items in window — bot may not have run yet",
        )

    silent = sorted(s for s, n in counts_window.items() if counts_24h.get(s, 0) == 0)
    if silent:
        return CriterionResult(
            name="data_sources",
            status="FAIL",
            value=f"{len(silent)} silent in 24h",
            threshold="0 silent",
            detail=f"silent: {', '.join(silent)}",
        )

    return CriterionResult(
        name="data_sources",
        status="PASS",
        value=f"{len(counts_window)} active",
        threshold="all active",
    )


# ---------------------------------------------------------------------------
# Criterion 3 — risk gate pass rate
# ---------------------------------------------------------------------------


async def check_pass_rate(
    db: Database,
    *,
    since: datetime,
    exchange: str | None,
    min_pct: float = 0.5,
    max_pct: float = 10.0,
    min_samples: int = 30,
) -> CriterionResult:
    """Pass rate = trades / signals over the window.

    Signals are recorded for every analyzed market regardless of risk-gate
    outcome (engine.py:540 inserts the signal *before* risk evaluation),
    while trades are recorded only when the gate approves. So the ratio
    is a faithful "what fraction of analyzed markets did the bot decide
    to trade".
    """
    sig_clause = ""
    sig_params: list = [since.isoformat()]
    trade_clause = ""
    trade_params: list = [since.isoformat()]
    if exchange:
        sig_clause = " AND exchange = ?"
        sig_params.append(exchange)
        trade_clause = " AND exchange = ?"
        trade_params.append(exchange)

    sig_row = await db.fetchone(
        f"SELECT COUNT(*) AS n FROM signals WHERE timestamp >= ?{sig_clause}",
        tuple(sig_params),
    )
    trade_row = await db.fetchone(
        f"SELECT COUNT(*) AS n FROM trades "
        f"WHERE timestamp >= ? AND is_paper = 1{trade_clause}",
        tuple(trade_params),
    )
    n_signals = sig_row["n"] if sig_row else 0
    n_trades = trade_row["n"] if trade_row else 0

    if n_signals < min_samples:
        return CriterionResult(
            name="pass_rate",
            status="INSUFFICIENT_DATA",
            value=f"{n_signals} signals",
            threshold=f"≥{min_samples} signals; {min_pct}%–{max_pct}% pass",
            n_samples=n_signals,
        )

    pct = (n_trades / n_signals) * 100.0 if n_signals else 0.0
    status: Status = "PASS" if min_pct <= pct <= max_pct else "FAIL"
    return CriterionResult(
        name="pass_rate",
        status=status,
        value=f"{pct:.1f}% ({n_trades}/{n_signals})",
        threshold=f"{min_pct}%–{max_pct}%",
        n_samples=n_signals,
    )


# ---------------------------------------------------------------------------
# Criteria 4 & 5 — Brier scores (absolute + relative-to-market)
# ---------------------------------------------------------------------------


async def _resolved_predictions(db: Database, since: datetime) -> list[dict]:
    """Calibration entries with paired market_prob from the first signal
    on that market. Used by both Brier criteria.

    The `predicted_prob IS NOT NULL` clause is defensive: the schema
    declares the column NOT NULL, so it should always hold, but a
    silent truncation or future schema migration could violate it.
    Better to skip the row than to TypeError mid-evaluation and abort
    the whole readiness report.
    """
    return await db.fetchall(
        """
        SELECT
            c.market_id      AS market_id,
            c.predicted_prob AS predicted_prob,
            c.actual_outcome AS actual_outcome,
            (
                SELECT s.market_prob
                FROM signals s
                WHERE s.market_id = c.market_id
                ORDER BY s.timestamp ASC
                LIMIT 1
            ) AS market_prob
        FROM calibration c
        WHERE c.actual_outcome IS NOT NULL
          AND c.predicted_prob IS NOT NULL
          AND c.resolved_at >= ?
        """,
        (since.isoformat(),),
    )


async def check_brier_absolute(
    db: Database,
    *,
    since: datetime,
    threshold: float = 0.24,
    min_samples: int = 30,
) -> CriterionResult:
    rows = await _resolved_predictions(db, since)
    if len(rows) < min_samples:
        return CriterionResult(
            name="brier_absolute",
            status="INSUFFICIENT_DATA",
            value=f"{len(rows)} resolved",
            threshold=f"≥{min_samples} resolved; Brier ≤ {threshold}",
            n_samples=len(rows),
        )
    brier = sum((r["predicted_prob"] - r["actual_outcome"]) ** 2 for r in rows) / len(
        rows
    )
    status: Status = "PASS" if brier <= threshold else "FAIL"
    return CriterionResult(
        name="brier_absolute",
        status=status,
        value=f"{brier:.3f}",
        threshold=f"≤{threshold}",
        n_samples=len(rows),
    )


async def check_brier_vs_market(
    db: Database,
    *,
    since: datetime,
    threshold: float = 0.02,
    min_samples: int = 30,
) -> CriterionResult:
    """Bot's Brier minus market's Brier on the same resolved events.

    A positive `delta` means market is better (bot worse). We require the
    bot to be at least `threshold` lower than the market — i.e.
    market_brier - bot_brier >= threshold.
    """
    rows = await _resolved_predictions(db, since)
    paired = [r for r in rows if r["market_prob"] is not None]
    if len(paired) < min_samples:
        return CriterionResult(
            name="brier_vs_market",
            status="INSUFFICIENT_DATA",
            value=f"{len(paired)} paired",
            threshold=f"≥{min_samples} paired; bot ≥{threshold} lower than market",
            n_samples=len(paired),
        )
    bot_brier = sum(
        (r["predicted_prob"] - r["actual_outcome"]) ** 2 for r in paired
    ) / len(paired)
    market_brier = sum(
        (r["market_prob"] - r["actual_outcome"]) ** 2 for r in paired
    ) / len(paired)
    edge = market_brier - bot_brier  # positive = bot better
    status: Status = "PASS" if edge >= threshold else "FAIL"
    return CriterionResult(
        name="brier_vs_market",
        status=status,
        value=f"bot {bot_brier:.3f} vs market {market_brier:.3f} (edge {edge:+.3f})",
        threshold=f"bot ≥{threshold} lower than market",
        n_samples=len(paired),
    )


# ---------------------------------------------------------------------------
# Criterion 6 — win rate on resolved trades
# ---------------------------------------------------------------------------


async def check_win_rate(
    db: Database,
    *,
    since: datetime,
    exchange: str | None,
    threshold_pct: float = 52.0,
    min_samples: int = 30,
) -> CriterionResult:
    clause = ""
    params: list = [since.isoformat()]
    if exchange:
        clause = " AND exchange = ?"
        params.append(exchange)
    rows = await db.fetchall(
        f"SELECT pnl FROM trades "
        f"WHERE timestamp >= ? AND is_paper = 1 AND pnl IS NOT NULL{clause}",
        tuple(params),
    )
    if len(rows) < min_samples:
        return CriterionResult(
            name="win_rate",
            status="INSUFFICIENT_DATA",
            value=f"{len(rows)} resolved trades",
            threshold=f"≥{min_samples} resolved; ≥{threshold_pct:.1f}% wins",
            n_samples=len(rows),
        )
    wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
    pct = wins / len(rows) * 100.0
    status: Status = "PASS" if pct >= threshold_pct else "FAIL"
    return CriterionResult(
        name="win_rate",
        status=status,
        value=f"{pct:.1f}% ({wins}/{len(rows)})",
        threshold=f"≥{threshold_pct:.1f}%",
        n_samples=len(rows),
    )


# ---------------------------------------------------------------------------
# Criterion 7 — net PnL after fees
# ---------------------------------------------------------------------------


async def check_pnl_after_fees(
    db: Database,
    *,
    since: datetime,
    exchange: str | None,
    fee_rate: float,
    min_samples: int = 30,
) -> CriterionResult:
    """Net PnL after applying the exchange's fee on profitable trades.

    Paper trades do not pay real fees, so their stored pnl is gross.
    `signal.edge` was already computed net of fees (signals.py:182), so
    by the time a paper trade exists at all the bot has cleared a
    fee-adjusted edge threshold. Here we apply the fee one more time on
    realised winning paper PnL so the readiness number reflects what
    real-money PnL on the actual exchange would have been.
    """
    clause = ""
    params: list = [since.isoformat()]
    if exchange:
        clause = " AND exchange = ?"
        params.append(exchange)
    rows = await db.fetchall(
        f"SELECT pnl FROM trades "
        f"WHERE timestamp >= ? AND is_paper = 1 AND pnl IS NOT NULL{clause}",
        tuple(params),
    )
    if len(rows) < min_samples:
        return CriterionResult(
            name="pnl_after_fees",
            status="INSUFFICIENT_DATA",
            value=f"{len(rows)} resolved trades",
            threshold=f"≥{min_samples} resolved; net PnL ≥ $0",
            n_samples=len(rows),
        )
    gross = sum(r["pnl"] or 0 for r in rows)
    winning_profit = sum(r["pnl"] for r in rows if (r["pnl"] or 0) > 0)
    fee_drag = winning_profit * fee_rate
    net = gross - fee_drag
    status: Status = "PASS" if net >= 0 else "FAIL"
    return CriterionResult(
        name="pnl_after_fees",
        status=status,
        value=f"${net:+.2f} (gross ${gross:+.2f}, fee drag ${fee_drag:.2f})",
        threshold="≥ $0",
        n_samples=len(rows),
    )


# ---------------------------------------------------------------------------
# Criterion 8 — second-opinion divergence
# ---------------------------------------------------------------------------


async def check_divergence(
    db: Database,
    *,
    since: datetime,
    exchange: str | None,
    median_threshold: float = 0.15,
    p95_threshold: float = 0.30,
    min_samples: int = 30,
) -> CriterionResult:
    clause = ""
    params: list = [since.isoformat()]
    if exchange:
        clause = " AND exchange = ?"
        params.append(exchange)
    rows = await db.fetchall(
        f"SELECT divergence FROM signals "
        f"WHERE timestamp >= ? AND divergence IS NOT NULL{clause}",
        tuple(params),
    )
    values = [r["divergence"] for r in rows if r["divergence"] is not None]
    if len(values) < min_samples:
        return CriterionResult(
            name="divergence",
            status="INSUFFICIENT_DATA",
            value=f"{len(values)} signals with second opinion",
            threshold=(
                f"≥{min_samples} signals; "
                f"median ≤{median_threshold}, p95 ≤{p95_threshold}"
            ),
            n_samples=len(values),
        )
    median = statistics.median(values)
    # statistics.quantiles(n=100) returns 99 cut points (the n−1
    # boundaries between n equal-probability intervals). Index 94 is
    # the 95th percentile. Both quantiles() and median() sort
    # internally, so no pre-sort needed.
    p95 = statistics.quantiles(values, n=100, method="inclusive")[94]
    median_ok = median <= median_threshold
    p95_ok = p95 <= p95_threshold
    status: Status = "PASS" if median_ok and p95_ok else "FAIL"
    return CriterionResult(
        name="divergence",
        status=status,
        value=f"median {median:.3f}, p95 {p95:.3f}",
        threshold=f"median ≤{median_threshold}, p95 ≤{p95_threshold}",
        n_samples=len(values),
    )


# ---------------------------------------------------------------------------
# Top-level evaluator
# ---------------------------------------------------------------------------


async def evaluate_readiness(
    db: Database,
    *,
    log_file: Path | None = None,
    exchange: str | None = None,
    days: int = 7,
    fee_rate: float | None = None,
) -> ReadinessReport:
    """Run all 8 criteria and return a ReadinessReport.

    `fee_rate` defaults to a conservative 0.07 (Kalshi's rate) if not
    provided. Phase 1 targets Kalshi so this is the right default; pass
    a different value when evaluating a different exchange.
    """
    now = datetime.now(timezone.utc)
    since_window = now - timedelta(days=days)
    since_24h = now - timedelta(hours=24)
    log_file = log_file or Path("auramaur.log")
    fee_rate = 0.07 if fee_rate is None else fee_rate

    criteria = [
        await check_cycle_health(log_file, since_window),
        await check_data_sources(db, since_24h=since_24h, since_window=since_window),
        await check_pass_rate(db, since=since_window, exchange=exchange),
        await check_brier_absolute(db, since=since_window),
        await check_brier_vs_market(db, since=since_window),
        await check_win_rate(db, since=since_window, exchange=exchange),
        await check_pnl_after_fees(
            db, since=since_window, exchange=exchange, fee_rate=fee_rate
        ),
        await check_divergence(db, since=since_window, exchange=exchange),
    ]
    return ReadinessReport(
        timestamp=now,
        exchange=exchange,
        window_days=days,
        criteria=criteria,
    )
