"""Tests for the cost enforcement watchdog task in bot.py.

The watchdog runs hourly and validates three invariants:
1. Model identity — analyzer._model matches settings.nlp.model
2. Daily call budget — analyzer._daily_calls <= settings.nlp.daily_claude_call_budget
3. Cycle frequency — cycles in last hour <= expected max (with 50% headroom)

Each check is independent: a missing _model attribute only skips check 1,
not checks 2 and 3. Any violation writes KILL_SWITCH and sends an
unrestricted email alert.

_cycle_timestamps stores (timestamp, schedule_mode) tuples. The cycle-rate
check only counts entries whose mode matches the current mode, preventing
false positives on mode transitions (e.g. peak → off-peak).
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings


def _make_bot(*, model: str = "sonnet", budget: int = 30):
    """Build a minimal AuramaurBot with mocked analyzer for enforcement testing.

    Returns (bot, mock_analyzer). The mock_analyzer has _model and _daily_calls
    attributes matching the requested values, and the bot's _components dict
    is wired up with the mock under the "analyzer" key.
    """
    from auramaur.bot import AuramaurBot

    settings = Settings()
    settings.nlp.model = model
    settings.nlp.daily_claude_call_budget = budget

    bot = AuramaurBot(settings=settings)
    bot._running = True

    mock_analyzer = MagicMock()
    mock_analyzer._model = model
    mock_analyzer._daily_calls = 0

    bot._components = {"analyzer": mock_analyzer}

    return bot, mock_analyzer


@pytest.mark.asyncio
async def test_all_checks_pass_no_kill_switch():
    """All checks pass — KILL_SWITCH not written.

    _get_schedule_mode is pinned to "peak" so the expected cycle cap is
    deterministic regardless of wall-clock time or bot cash state.
    Cycle timestamps use (timestamp, mode) tuples matching the pinned mode.
    """
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 10

    now = time.monotonic()
    bot._cycle_timestamps = deque(
        [(now - 1800, "peak"), (now - 600, "peak")], maxlen=200
    )

    with (
        patch.object(bot, "_get_schedule_mode", return_value="peak"),
        patch("auramaur.bot.send_alert") as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_not_called()
        mock_alert.assert_not_called()
        assert bot._running is True


@pytest.mark.asyncio
async def test_model_mismatch_fires_kill_switch():
    """Wrong model loaded — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._model = "opus"

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True),
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "model mismatch" in written
        assert bot._running is False


@pytest.mark.asyncio
async def test_budget_exceeded_fires_kill_switch():
    """Daily budget exceeded — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 35

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True),
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "daily budget exceeded" in written
        assert bot._running is False


@pytest.mark.asyncio
async def test_cycle_rate_exceeded_fires_kill_switch():
    """Too many cycles in last hour — kill switch fires.

    Cycle timestamps are tagged with "quiet" mode (matching the mocked
    _get_schedule_mode). With quiet multiplier=24 and analysis_seconds=180,
    expected_interval=4320s → max_cycles=(3600/4320)*1.5≈1.25. 20 cycles
    in the last hour far exceeds this cap.
    """
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 5

    now = time.monotonic()
    bot._cycle_timestamps = deque(
        [(now - (i * 180), "quiet") for i in range(20)],
        maxlen=200,
    )

    with (
        patch.object(bot, "_get_schedule_mode", return_value="quiet"),
        patch("auramaur.bot.send_alert", return_value=True),
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "cycle rate exceeded" in written
        assert bot._running is False


@pytest.mark.asyncio
async def test_budget_zero_unlimited_skips_check():
    """Budget of 0 means unlimited — budget check skipped even with many calls.

    _get_schedule_mode is pinned to "peak" so the cycle-frequency cap is
    deterministic regardless of wall-clock time or bot cash state.
    """
    bot, analyzer = _make_bot(model="sonnet", budget=0)
    analyzer._daily_calls = 500

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch.object(bot, "_get_schedule_mode", return_value="peak"),
        patch("auramaur.bot.send_alert") as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_not_called()
        mock_alert.assert_not_called()
        assert bot._running is True


@pytest.mark.asyncio
async def test_email_alert_sent_on_violation():
    """Any violation sends an unrestricted email alert (not rate-limited)."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._model = "opus"

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text"),
    ):
        await bot._run_cost_enforcement_check()

        mock_alert.assert_called_once()
        subject = mock_alert.call_args.kwargs.get(
            "subject", mock_alert.call_args.args[0] if mock_alert.call_args.args else ""
        )
        assert "KILL SWITCH" in subject


@pytest.mark.asyncio
async def test_mode_transition_no_false_positive():
    """Peak-rate cycles are ignored when mode transitions to off_peak.

    Scenario: 20 peak-rate cycles in the last hour, but mode just
    flipped to off_peak. The watchdog should only count off_peak cycles
    (of which there are 0), so no kill switch fires.
    """
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 5

    now = time.monotonic()
    # 20 recent cycles, all tagged as "peak" — simulates the hour just
    # before a mode transition to off_peak.
    bot._cycle_timestamps = deque(
        [(now - (i * 180), "peak") for i in range(20)],
        maxlen=200,
    )

    with (
        patch.object(bot, "_get_schedule_mode", return_value="off_peak"),
        patch("auramaur.bot.send_alert") as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_not_called()
        mock_alert.assert_not_called()
        assert bot._running is True


@pytest.mark.asyncio
async def test_checks_independent_of_model_attr():
    """Budget and cycle checks still run when analyzer lacks _model.

    This covers future analyzer types (e.g. EnsembleAnalyzer) that have
    _models (plural) instead of _model. The model-identity check is
    skipped, but budget and cycle checks still fire if violated.
    """
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    del analyzer._model  # simulate an analyzer without _model
    analyzer._daily_calls = 50  # exceeds budget of 30

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True),
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        # Budget check should still fire even without _model
        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "daily budget exceeded" in written
        assert bot._running is False
