"""Tests for the cost enforcement watchdog task in bot.py.

The watchdog runs hourly and validates three invariants:
1. Model identity — analyzer._model matches settings.nlp.model
2. Daily call budget — analyzer._daily_calls <= settings.nlp.daily_claude_call_budget
3. Cycle frequency — cycles in last hour <= expected max (with 50% headroom)

Any violation writes KILL_SWITCH and sends an unrestricted email alert.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings


def _make_bot(*, model: str = "sonnet", budget: int = 30):
    """Build a minimal AuramaurBot with mocked analyzer for enforcement testing."""
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
    """All checks pass — KILL_SWITCH not written."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 10

    now = time.monotonic()
    bot._cycle_timestamps = deque([now - 1800, now - 600], maxlen=200)

    with (
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
    """Too many cycles in last hour — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 5

    now = time.monotonic()
    bot._cycle_timestamps = deque(
        [now - (i * 180) for i in range(20)],
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
    """Budget of 0 means unlimited — budget check skipped even with many calls."""
    bot, analyzer = _make_bot(model="sonnet", budget=0)
    analyzer._daily_calls = 500

    bot._cycle_timestamps = deque(maxlen=200)

    with (
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
