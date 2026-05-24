"""Tests for the VPN watchdog task in AuramaurBot.

Covers the _task_vpn_watchdog state machine and the trading cycle pause
behavior when _vpn_down is True.  Uses the same mock patterns as
tests/test_retry.py — patching _check_vpn_health and send_alert_rate_limited
at the *module level* in auramaur.bot (where they were imported), not at
their definition site in auramaur.infra.*.

Six tests in two groups:

  TestVpnWatchdogStateTransitions (4 tests):
    1. HEALTHY -> HEALTHY: no state change, no alert
    2. HEALTHY -> DOWN:    _vpn_down=True set, "vpn_down" alert sent
    3. DOWN -> DOWN:       no duplicate alert (guard prevents re-entry)
    4. DOWN -> HEALTHY:    _vpn_down=False cleared, "vpn_recovered" alert sent

  TestTradingCyclePause (2 tests):
    5. _task_trading_cycle skips engine.run_cycle when _vpn_down=True
    6. _task_market_scan skips engine.scan_and_store_markets when _vpn_down=True
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bot(exchange_filter: str | None = "polymarket"):
    """Create a minimal AuramaurBot-like object for watchdog testing.

    Uses a lightweight mock instead of the real AuramaurBot to avoid
    wiring up the full component graph (DB, exchanges, NLP, etc.).
    The watchdog only touches _running, _vpn_down, _vpn_down_since,
    and _exchange_filter — all set directly on the mock.

    We patch AuramaurBot.__init__ so that ``__new__`` gives us a real
    instance (preserving method resolution order) while skipping the
    heavyweight constructor that would try to connect to databases,
    exchanges, etc.
    """
    from auramaur.bot import AuramaurBot

    # Patch __init__ to avoid full component initialization.
    with patch.object(AuramaurBot, "__init__", lambda self, **kw: None):
        bot = AuramaurBot()

    # Seed the three fields that the watchdog reads/writes.
    bot._running = True
    bot._vpn_down = False
    bot._vpn_down_since = None
    bot._exchange_filter = exchange_filter
    return bot


async def _run_watchdog_iterations(bot, n: int = 1):
    """Run the watchdog for *n* iterations then stop it.

    Overrides ``asyncio.sleep`` to count iterations instead of actually
    sleeping.  After *n* iterations, sets ``_running=False`` so the
    ``while self._running`` loop exits cleanly.
    """
    iteration = 0

    async def fake_sleep(seconds):
        nonlocal iteration
        iteration += 1
        if iteration >= n:
            bot._running = False

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await bot._task_vpn_watchdog()


# ---------------------------------------------------------------------------
# State-machine tests
# ---------------------------------------------------------------------------


class TestVpnWatchdogStateTransitions:
    """Test the _task_vpn_watchdog HEALTHY/DOWN state machine.

    Each test patches the VPN probe (``_check_vpn_health``) and the alert
    function (``send_alert_rate_limited``) at the module level in
    ``auramaur.bot`` — which is where the imports bound those names.
    """

    @pytest.mark.asyncio
    async def test_vpn_healthy_no_state_change(self):
        """When VPN is healthy and was healthy, no alert is sent.

        Starting state:  _vpn_down=False, _vpn_down_since=None.
        Probe returns:   True.
        Expected result: no state change, no alert.  The watchdog's
        ``elif not vpn_ok`` branch is not taken, and the ``if vpn_ok
        and self._vpn_down`` branch is skipped because _vpn_down is
        already False.
        """
        bot = _make_bot()

        with (
            patch(
                "auramaur.bot._check_vpn_health",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is False
        assert bot._vpn_down_since is None
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_vpn_goes_down_sets_flag_and_alerts(self):
        """When VPN probe fails, _vpn_down is set and a "vpn_down" alert fires.

        Starting state:  _vpn_down=False (healthy).
        Probe returns:   False.
        Expected result: _vpn_down=True, _vpn_down_since is set,
        ``send_alert_rate_limited`` called once with key="vpn_down".
        """
        bot = _make_bot()

        with (
            patch(
                "auramaur.bot._check_vpn_health",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is True
        assert bot._vpn_down_since is not None
        mock_alert.assert_called_once()
        # Verify the alert was filed under the "vpn_down" rate-limit key
        # by checking the keyword argument directly (not a loose substring).
        assert mock_alert.call_args.kwargs["key"] == "vpn_down"

    @pytest.mark.asyncio
    async def test_vpn_stays_down_no_duplicate_alert(self):
        """When VPN is already down and probe fails again, no alert fires.

        Starting state:  _vpn_down=True, _vpn_down_since set 2 min ago.
        Probe returns:   False.
        Expected result: _vpn_down stays True.  The watchdog's elif guard
        (``not vpn_ok and not self._vpn_down``) is False, so the DOWN
        transition branch is never entered — ``send_alert_rate_limited``
        is not called at all.  (Rate limiting is secondary; the branch
        isn't even reached.)
        """
        bot = _make_bot()
        bot._vpn_down = True
        bot._vpn_down_since = time.monotonic() - 120  # down for 2 minutes

        with (
            patch(
                "auramaur.bot._check_vpn_health",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is True
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_vpn_recovers_clears_flag_and_alerts(self):
        """When VPN comes back up after being down, _vpn_down is cleared.

        Starting state:  _vpn_down=True, _vpn_down_since set 5 min ago.
        Probe returns:   True.
        Expected result: _vpn_down=False, _vpn_down_since=None,
        ``send_alert_rate_limited`` called once with key="vpn_recovered".
        """
        bot = _make_bot()
        bot._vpn_down = True
        bot._vpn_down_since = time.monotonic() - 300  # was down for 5 minutes

        with (
            patch(
                "auramaur.bot._check_vpn_health",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is False
        assert bot._vpn_down_since is None
        mock_alert.assert_called_once()
        # Verify the alert was filed under the "vpn_recovered" rate-limit key
        # by checking the keyword argument directly (not a loose substring).
        assert mock_alert.call_args.kwargs["key"] == "vpn_recovered"


# ---------------------------------------------------------------------------
# Pause-behavior tests
# ---------------------------------------------------------------------------


class TestTradingCyclePause:
    """Test that trading cycle and market scan skip when _vpn_down is True.

    Both _task_trading_cycle and _task_market_scan check ``self._vpn_down``
    early in each loop iteration — before calling the engine.  When the flag
    is True, the iteration logs a skip and sleeps without touching the engine.

    These tests verify the engine method is NOT called when _vpn_down=True.
    """

    @pytest.mark.asyncio
    async def test_trading_cycle_skips_when_vpn_down(self):
        """_task_trading_cycle skips engine.run_cycle when _vpn_down is True.

        The trading cycle has a preliminary loop that waits for
        ``_last_known_cash > 0``.  We pre-set that attribute so the
        preliminary loop exits immediately on the first check, then the
        main loop runs one iteration and exits.

        Expected: engine.run_cycle is never called because the _vpn_down
        guard fires first (after the kill-switch check).
        """
        bot = _make_bot()
        bot._vpn_down = True

        # Attributes the trading cycle reads.
        bot.settings = MagicMock()
        bot.settings.intervals.analysis_seconds = 10
        bot._adaptive_interval = lambda x: 0.01  # fast sleep for test
        bot._check_kill_switch = AsyncMock(return_value=False)
        bot._last_known_cash = 100.0  # skip the preliminary wait loop

        engine = MagicMock()
        engine.run_cycle = AsyncMock()

        iteration = 0

        async def fake_sleep(seconds):
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                bot._running = False

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await bot._task_trading_cycle(engine, name="polymarket")

        # Engine should NOT have been called — VPN is down.
        engine.run_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_market_scan_skips_when_vpn_down(self):
        """_task_market_scan skips engine.scan_and_store_markets when _vpn_down is True.

        Expected: engine.scan_and_store_markets is never called because
        the _vpn_down guard fires before the engine call.
        """
        bot = _make_bot()
        bot._vpn_down = True

        # Attributes the market scan reads.
        bot.settings = MagicMock()
        bot.settings.intervals.market_scan_seconds = 10
        bot._adaptive_interval = lambda x: 0.01
        bot._check_kill_switch = AsyncMock(return_value=False)

        engine = MagicMock()
        engine.scan_and_store_markets = AsyncMock()

        iteration = 0

        async def fake_sleep(seconds):
            nonlocal iteration
            iteration += 1
            if iteration >= 1:
                bot._running = False

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await bot._task_market_scan(engine, name="polymarket")

        engine.scan_and_store_markets.assert_not_called()
