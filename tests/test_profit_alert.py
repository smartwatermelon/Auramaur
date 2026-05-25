"""Tests for the profit withdrawal alert task in bot.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import ProfitAlertConfig, Settings


def _make_bot(
    *,
    enabled: bool = True,
    threshold: float = 600.0,
    withdrawal: float = 300.0,
    cooldown_hours: int = 24,
):
    """Build a minimal AuramaurBot with mocked components for profit alert testing."""
    from auramaur.bot import AuramaurBot

    settings = Settings()
    settings.profit_alerts = ProfitAlertConfig(
        enabled=enabled,
        check_interval_seconds=1,
        profit_threshold=threshold,
        withdrawal_amount=withdrawal,
        alert_cooldown_hours=cooldown_hours,
    )

    bot = AuramaurBot(settings=settings)
    bot._running = True

    mock_syncer = AsyncMock()
    mock_pnl = AsyncMock()
    bot._components = {
        "syncer": mock_syncer,
        "pnl_tracker": mock_pnl,
    }

    return bot, mock_syncer, mock_pnl


def _make_position(
    market_id: str = "mkt_1", current_price: float = 0.7, unrealized_pnl: float = 0.0
):
    """Create a mock LivePosition."""
    pos = MagicMock()
    pos.market_id = market_id
    pos.current_price = current_price
    pos.unrealized_pnl = unrealized_pnl
    return pos


@pytest.mark.asyncio
async def test_below_threshold_no_alert():
    """P&L below threshold — no alert sent."""
    bot, syncer, pnl = _make_bot(threshold=600.0)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 400.0

    with patch("auramaur.bot.send_alert_rate_limited") as mock_alert:

        async def _run_one():
            bot._running = True
            task = asyncio.create_task(bot._task_profit_withdrawal_alert())
            await asyncio.sleep(0.05)
            bot._running = False
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_one()
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_at_threshold_alert_fires():
    """P&L at threshold — alert fires."""
    bot, syncer, pnl = _make_bot(threshold=600.0)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 600.0

    with patch("auramaur.bot.send_alert_rate_limited", return_value=True) as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_called_once()
        call_kwargs = mock_alert.call_args
        assert "profit_withdrawal" in str(call_kwargs)


@pytest.mark.asyncio
async def test_above_threshold_alert_fires():
    """P&L above threshold — alert fires."""
    bot, syncer, pnl = _make_bot(threshold=600.0)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 800.0

    with patch("auramaur.bot.send_alert_rate_limited", return_value=True) as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_syncer_failure_no_crash():
    """Syncer raises exception — task logs error and continues."""
    bot, syncer, pnl = _make_bot()
    syncer.sync.side_effect = RuntimeError("CLOB timeout")

    with patch("auramaur.bot.send_alert_rate_limited") as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_no_alert():
    """profit_alerts.enabled=False — no alert even above threshold."""
    bot, syncer, pnl = _make_bot(enabled=False)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 1000.0

    with patch("auramaur.bot.send_alert_rate_limited") as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_no_syncer_task_exits():
    """No syncer in components — task should exit gracefully."""
    bot, _, _ = _make_bot()
    del bot._components["syncer"]

    task = asyncio.create_task(bot._task_profit_withdrawal_alert())
    await asyncio.sleep(0.05)
    bot._running = False
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # No crash = success


@pytest.mark.asyncio
async def test_no_pnl_tracker_task_exits():
    """No pnl_tracker in components — task exits gracefully (no KeyError)."""
    bot, _, _ = _make_bot()
    del bot._components["pnl_tracker"]

    task = asyncio.create_task(bot._task_profit_withdrawal_alert())
    await asyncio.sleep(0.05)
    bot._running = False
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # No crash = success
