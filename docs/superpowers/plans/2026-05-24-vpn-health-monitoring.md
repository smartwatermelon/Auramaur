# VPN Health Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect broken VPN proxy within 60s (bot) / 300s (wrapper), auto-restart the container, pause trading cycles, and alert via email — preventing hours of silent bot failure.

**Architecture:** Two-layer monitoring. The Gluetun wrapper (`wrapper-gluetun.sh`) probes the proxy every 300s after `ensure_container()` and restarts the container if broken. The bot (`bot.py`) runs a 60s watchdog task that flips a `_vpn_down` flag, pausing `_task_trading_cycle` and `_task_market_scan` while VPN is down. Both layers send email alerts independently — the wrapper for remediation events, the bot for trading impact.

**Tech Stack:** Bash (curl probe), Python (aiohttp probe via existing `_check_vpn_health()`), msmtp (email alerts via existing `send_alert` / `send_alert_rate_limited`)

**Spec:** `docs/specs/2026-05-24-vpn-health-monitoring-design.md`

---

## Task 1: Gluetun Wrapper — Proxy Health Check + Auto-Restart + Email Alert

**Files:**

- Modify: `scripts/launchd/wrapper-gluetun.sh:1-166` (add two functions, modify supervision loop)

This task adds two functions to the gluetun wrapper script and wires one into the supervision loop. The `send_alert()` function mirrors the pattern from `wrapper-bot.sh:38-54`. The `check_proxy_health()` function probes the proxy via curl and restarts the container if the probe fails.

- [ ] **Step 1: Write the `send_alert()` function**

Add after the `log_ts()` function (after line 30), before `unlock_keychain()`:

```bash
# --- Alert email via msmtp ---
# Same pattern as wrapper-bot.sh — uses msmtp directly since this
# script runs outside the Python venv.
MAIL_TO="${AURAMAUR_ALERT_TO:-andrew.rich@gmail.com}"
MAIL_FROM="${AURAMAUR_ALERT_FROM:-andrew.rich@gmail.com}"

send_alert() {
  # Send an email alert via msmtp. Args: $1=subject, $2=body.
  # Non-fatal: if msmtp is missing or fails, log a warning and continue.
  local subject="$1" body="$2"
  if ! command -v msmtp >/dev/null 2>&1; then
    log_ts "WARN: msmtp not found — alert not sent: ${subject}"
    return 1
  fi
  printf 'From: %s\nTo: %s\nSubject: %s\n\n%s\n' \
    "${MAIL_FROM}" "${MAIL_TO}" "${subject}" "${body}" \
    | msmtp -a gmail "${MAIL_TO}" 2>/dev/null
  local rc=$?
  if [[ ${rc} -eq 0 ]]; then
    log_ts "Alert sent: ${subject}"
  else
    log_ts "WARN: msmtp failed (rc=${rc}) — alert not sent: ${subject}"
  fi
  return ${rc}
}
```

- [ ] **Step 2: Write the `check_proxy_health()` function**

Add after `send_alert()`, before `unlock_keychain()`:

```bash
check_proxy_health() {
  # Probe the Gluetun HTTP proxy by requesting an external endpoint through it.
  # If the probe fails, the container's tunnel is broken even though the
  # container itself is "running". Restart the container and send an alert.
  #
  # Called from the supervision loop AFTER ensure_container() confirms the
  # container is in "running" state. This catches the failure mode where
  # OpenVPN routes go stale or DNS fails inside the container.
  local proxy_ip restart_ts new_ip elapsed

  if proxy_ip=$(curl -sf --proxy http://localhost:8888 --max-time 10 https://ipinfo.io/ip 2>/dev/null); then
    log_ts "Proxy health OK (exit IP: ${proxy_ip})"
    return 0
  fi

  log_ts "ERROR: proxy health check failed — restarting gluetun-vpn container"
  podman restart gluetun-vpn 2>&1 || true

  # Wait up to 60s for the container to reach "healthy" status.
  # Podman's --health-start-period is 120s, but the proxy itself typically
  # comes up much faster. Poll every 5s to detect recovery promptly.
  elapsed=0
  while [[ "${elapsed}" -lt 60 ]]; do
    sleep 5
    elapsed=$((elapsed + 5))
    local health
    health=$(podman inspect gluetun-vpn --format '{{.State.Health.Status}}' 2>/dev/null) || health=""
    if [[ "${health}" == "healthy" ]]; then
      break
    fi
  done

  # Try to get the new exit IP for the alert
  new_ip=$(curl -sf --proxy http://localhost:8888 --max-time 10 https://ipinfo.io/ip 2>/dev/null) || new_ip="unknown — healthcheck still pending"
  restart_ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  send_alert \
    "[auramaur] gluetun: proxy broken, container restarted" \
    "The Gluetun VPN proxy at localhost:8888 was unreachable.
Container restarted at ${restart_ts}.
New exit IP: ${new_ip}
Log: ~/Library/Logs/auramaur/gluetun.log"

  return 1
}
```

- [ ] **Step 3: Wire `check_proxy_health()` into the supervision loop**

Modify the supervision loop (currently lines 154-165) to call `check_proxy_health()` after `ensure_container()` succeeds. Replace the existing loop:

```bash
while true; do
  sleep "${SUPERVISE_INTERVAL}"

  if ! ensure_machine; then
    log_ts "WARNING: Podman machine recovery failed, will retry next cycle"
    continue
  fi

  if ! ensure_container; then
    log_ts "WARNING: container recovery failed, will retry next cycle"
    continue
  fi

  # Probe the proxy — container is "running" but the tunnel may be broken.
  # check_proxy_health() restarts and alerts if the probe fails.
  check_proxy_health
done
```

Note two changes from the original:

1. Added `check_proxy_health` call after `ensure_container`.
2. Changed the `ensure_container` failure from a bare `if` to `continue` — if the container itself failed recovery, skip the proxy health check (it would also fail and generate a confusing alert).

- [ ] **Step 4: Run shellcheck on the modified script**

Run: `shellcheck -S info scripts/launchd/wrapper-gluetun.sh`
Expected: No errors or warnings. Fix any issues before proceeding.

- [ ] **Step 5: Commit**

```bash
git add scripts/launchd/wrapper-gluetun.sh
git commit -m "feat(infra): add proxy health check + auto-restart to gluetun wrapper

Add send_alert() and check_proxy_health() to wrapper-gluetun.sh.
The supervision loop now probes the HTTP proxy via curl after
ensure_container() confirms the container is running. If the probe
fails (broken OpenVPN tunnel, stale routes, DNS failure), the
container is restarted and an email alert is sent.

This catches the failure mode from 2026-05-24 where the container
was 'running' but the proxy was not forwarding traffic, causing
hours of silent bot failure.

Spec: docs/specs/2026-05-24-vpn-health-monitoring-design.md

Assisted-by: Claude (Anthropic)"
```

---

### Task 2: Bot VPN Watchdog — `_task_vpn_watchdog` + State Attributes

**Files:**

- Modify: `auramaur/bot.py:50-68` (add state attributes to `__init__`)
- Modify: `auramaur/bot.py` (add new `_task_vpn_watchdog` method)
- Modify: `auramaur/bot.py:2270-2326` (register watchdog task in `run()`)

This task adds the bot-level VPN watchdog: a 60-second async task that probes the proxy, manages the `_vpn_down` state machine, and sends rate-limited email alerts on state transitions.

- [ ] **Step 1: Add VPN state attributes to `__init__`**

In `auramaur/bot.py`, add two attributes after the `_arb_attempts` dict (after line 68):

```python
        # VPN watchdog state — tracks whether the Gluetun proxy is reachable.
        # When _vpn_down is True, _task_trading_cycle and _task_market_scan
        # skip their iterations to avoid wasting Claude API tokens on analysis
        # that can never execute (all CLOB requests timeout through the proxy).
        self._vpn_down: bool = False
        self._vpn_down_since: float | None = None  # monotonic timestamp
```

- [ ] **Step 2: Add the import for `time` at the top of bot.py**

Check the existing imports in `auramaur/bot.py`. If `time` is not already imported, add it alongside the other stdlib imports:

```python
import time
```

Also verify that the notify module import exists. If not present, add:

```python
from auramaur.infra.notify import send_alert_rate_limited
```

And verify the retry module import for `_check_vpn_health`. If not present, add:

```python
from auramaur.infra.retry import _check_vpn_health
```

- [ ] **Step 3: Write the `_task_vpn_watchdog` method**

Add the method to the `AuramaurBot` class, near the other `_task_*` methods (after `_task_cache_cleanup` or similar utility tasks, before the engine-specific tasks like `_task_market_scan`). The exact placement should follow the existing file's organizational pattern:

```python
    async def _task_vpn_watchdog(self) -> None:
        """Monitor VPN proxy health and pause trading when the proxy is down.

        Runs every 60 seconds. Uses _check_vpn_health() from retry.py to probe
        the Gluetun HTTP proxy at localhost:8888. Manages a two-state machine:

            HEALTHY → DOWN:  probe fails → set _vpn_down=True, log, send alert
            DOWN → HEALTHY:  probe passes → set _vpn_down=False, log, send recovery alert

        The wrapper-gluetun.sh handles remediation (container restart). This task
        handles protection: pausing trading cycles so the bot doesn't waste Claude
        API tokens on analysis that can never execute through a broken proxy.

        Only started when the bot is filtering to polymarket or running all
        exchanges. Kalshi-only instances skip it (Kalshi doesn't route through
        the proxy).
        """
        while self._running:
            try:
                vpn_ok = await _check_vpn_health()

                if vpn_ok and self._vpn_down:
                    # RECOVERY: DOWN → HEALTHY
                    downtime_minutes = 0.0
                    if self._vpn_down_since is not None:
                        downtime_minutes = (time.monotonic() - self._vpn_down_since) / 60.0
                    self._vpn_down = False
                    self._vpn_down_since = None
                    log.info(
                        "vpn_watchdog.recovered",
                        downtime_minutes=round(downtime_minutes, 1),
                    )
                    send_alert_rate_limited(
                        subject="[auramaur] polymarket: VPN proxy recovered — trading resumed",
                        body=(
                            "The Gluetun VPN proxy is reachable again.\n"
                            "Trading cycles have resumed.\n"
                            f"\nTime: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
                            f"\nDowntime: ~{round(downtime_minutes)} minutes"
                        ),
                        key="vpn_recovered",
                        min_interval=60,
                    )

                elif not vpn_ok and not self._vpn_down:
                    # TRANSITION: HEALTHY → DOWN
                    self._vpn_down = True
                    self._vpn_down_since = time.monotonic()
                    log.warning("vpn_watchdog.down")
                    send_alert_rate_limited(
                        subject="[auramaur] polymarket: VPN proxy down — trading paused",
                        body=(
                            "The Gluetun VPN proxy at localhost:8888 is unreachable.\n"
                            "Trading cycles are paused until the proxy recovers.\n"
                            "The gluetun wrapper will attempt auto-restart.\n"
                            f"\nTime: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
                            "\nLog: ~/Library/Logs/auramaur/polymarket.log"
                        ),
                        key="vpn_down",
                        min_interval=1800,
                    )

                # If vpn_ok and not _vpn_down: healthy, no action needed.
                # If not vpn_ok and _vpn_down: still down, rate limiter suppresses.

            except Exception as e:
                # The watchdog must never crash — it's the safety net.
                log.error("vpn_watchdog.error", error=str(e))

            await asyncio.sleep(60)
    ```

- [ ] **Step 4: Register the watchdog task in `run()`**

In `auramaur/bot.py`, in the `run()` method, add the VPN watchdog task registration. Add it after the position sync block (around line 2286) and before the resolution checker block (around line 2288). The gate checks the exchange filter — Kalshi doesn't use the VPN proxy:

```python
        # VPN watchdog — monitors the Gluetun proxy and pauses trading if it
        # goes down. Only needed when Polymarket is active (Kalshi doesn't route
        # through the proxy). Runs every 60s, sends email alerts on transitions.
        if self._exchange_filter is None or self._exchange_filter == "polymarket":
            tasks.append(
                asyncio.create_task(self._task_vpn_watchdog(), name="vpn_watchdog")
            )
```

- [ ] **Step 5: Run the test suite to verify nothing broke**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/ -x -q`
Expected: All existing tests pass. No import errors, no syntax errors.

- [ ] **Step 6: Commit**

```bash
git add auramaur/bot.py
git commit -m "feat(bot): add VPN watchdog task with state machine and email alerts

Add _task_vpn_watchdog to AuramaurBot — a 60-second async task that
probes the Gluetun HTTP proxy via _check_vpn_health() and manages a
HEALTHY/DOWN state machine.

On transition to DOWN: sets _vpn_down=True, logs vpn_watchdog.down,
sends rate-limited email alert (key=vpn_down, interval=1800s).

On transition to HEALTHY: sets _vpn_down=False, logs
vpn_watchdog.recovered with downtime duration, sends recovery alert
(key=vpn_recovered, interval=60s).

The task only starts when the bot filters to polymarket or runs all
exchanges. Kalshi-only instances skip it since Kalshi doesn't route
through the VPN proxy.

State attributes: _vpn_down (bool), _vpn_down_since (float|None,
monotonic timestamp for downtime calculation in recovery alerts).

Spec: docs/specs/2026-05-24-vpn-health-monitoring-design.md

Assisted-by: Claude (Anthropic)"
```

---

### Task 3: Trading Cycle Pause When VPN Is Down

**Files:**

- Modify: `auramaur/bot.py:597-608` (add skip to `_task_market_scan`)
- Modify: `auramaur/bot.py:619-640` (add skip to `_task_trading_cycle`)

This task adds the VPN-down guard to the two expensive async tasks that route through the proxy. When `_vpn_down` is True, these tasks skip their iteration and sleep for the normal interval instead of running the engine cycle (which would waste Claude API tokens on NLP analysis that can never execute).

- [ ] **Step 1: Add VPN-down skip to `_task_market_scan`**

In `auramaur/bot.py`, modify `_task_market_scan` to check `self._vpn_down` at the top of the while loop, after the kill switch check (line 600-601):

```python
    async def _task_market_scan(self, engine: TradingEngine, name: str = "") -> None:
        """Periodically scan and store markets."""
        while self._running:
            if await self._check_kill_switch():
                return
            # Skip scan while VPN proxy is down — discovery calls timeout
            # through the broken proxy and provide no useful data.
            if self._vpn_down:
                log.info("market_scan.skipped_vpn_down", exchange=name)
                await asyncio.sleep(
                    self._adaptive_interval(self.settings.intervals.market_scan_seconds)
                )
                continue
            try:
                await engine.scan_and_store_markets()
            except Exception as e:
                show_error(f"Market scan failed ({name}): {e}")
            await asyncio.sleep(
                self._adaptive_interval(self.settings.intervals.market_scan_seconds)
            )
```

- [ ] **Step 2: Add VPN-down skip to `_task_trading_cycle`**

In `auramaur/bot.py`, modify `_task_trading_cycle` to check `self._vpn_down` at the top of the while loop, after the kill switch check (line 620-621):

```python
        while self._running:
            if await self._check_kill_switch():
                return

            # Skip cycle while VPN proxy is down — NLP analysis and order
            # placement both require the proxy. Running the cycle would burn
            # Claude API tokens on analysis whose resulting orders can never
            # reach the CLOB.
            if self._vpn_down:
                log.info("trading_cycle.skipped_vpn_down", exchange=name)
                await asyncio.sleep(
                    self._adaptive_interval(self.settings.intervals.analysis_seconds)
                )
                continue

            try:
                cash = getattr(self, "_last_known_cash", 0.0)
                await engine.run_cycle(cash_available=cash)
            except Exception as e:
                show_error(f"Trading cycle failed ({name}): {e}")
```

- [ ] **Step 3: Run the test suite**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/ -x -q`
Expected: All existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add auramaur/bot.py
git commit -m "feat(bot): pause trading cycle and market scan when VPN is down

Add _vpn_down guard to _task_trading_cycle and _task_market_scan.
When the VPN watchdog detects the proxy is unreachable, these tasks
skip their iterations instead of running the engine cycle.

This prevents wasting Claude API tokens on NLP analysis whose
resulting orders can never reach the CLOB through the broken proxy.
Both tasks log a structured skip event and sleep for their normal
interval before re-checking.

Tasks that keep running during VPN outage:
- _task_kill_switch_monitor (local file check)
- _task_cache_cleanup (local cleanup)
- _task_recalibrate (reads local DB)
- _task_vpn_watchdog (must keep running to detect recovery)
- _task_order_monitor (monitors existing orders, async_retry handles failures)
- _task_resolution_checker (reads DB + exchange, async_retry handles failures)

Spec: docs/specs/2026-05-24-vpn-health-monitoring-design.md

Assisted-by: Claude (Anthropic)"
```

---

### Task 4: Tests — VPN Watchdog Test Suite

**Files:**

- Create: `tests/test_vpn_watchdog.py`

This task creates the test suite for the VPN watchdog. Five tests cover the state machine transitions and the trading cycle pause behavior. Test patterns follow `tests/test_retry.py` conventions: `@pytest.mark.asyncio`, `unittest.mock.patch` for `_check_vpn_health` and `send_alert_rate_limited`.

- [ ] **Step 1: Write the test file**

Create `tests/test_vpn_watchdog.py`:

```python
"""Tests for the VPN watchdog task in AuramaurBot.

Covers the _task_vpn_watchdog state machine and the trading cycle pause
behavior when _vpn_down is True. Uses the same mock patterns as
tests/test_retry.py — patching _check_vpn_health and send_alert_rate_limited.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_bot(exchange_filter: str | None = "polymarket"):
    """Create a minimal AuramaurBot-like object for watchdog testing.

    Uses a lightweight mock instead of the real AuramaurBot to avoid
    wiring up the full component graph (DB, exchanges, NLP, etc.).
    The watchdog only touches _running, _vpn_down, _vpn_down_since,
    and _exchange_filter — all set directly on the mock.
    """
    from auramaur.bot import AuramaurBot

    # Patch __init__ to avoid full component initialization
    with patch.object(AuramaurBot, "__init__", lambda self, **kw: None):
        bot = AuramaurBot()

    bot._running = True
    bot._vpn_down = False
    bot._vpn_down_since = None
    bot._exchange_filter = exchange_filter
    return bot


async def _run_watchdog_iterations(bot, n: int = 1):
    """Run the watchdog for n iterations then stop it.

    Overrides asyncio.sleep to count iterations instead of actually sleeping.
    After n iterations, sets _running=False so the while loop exits.
    """
    iteration = 0

    async def fake_sleep(seconds):
        nonlocal iteration
        iteration += 1
        if iteration >= n:
            bot._running = False

    with patch("asyncio.sleep", side_effect=fake_sleep):
        await bot._task_vpn_watchdog()


class TestVpnWatchdogStateTransitions:
    """Test the _task_vpn_watchdog HEALTHY/DOWN state machine."""

    @pytest.mark.asyncio
    async def test_vpn_healthy_no_state_change(self):
        """When VPN is healthy and was healthy, no alert is sent."""
        bot = _make_bot()

        with (
            patch("auramaur.bot._check_vpn_health", return_value=True),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is False
        assert bot._vpn_down_since is None
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_vpn_goes_down_sets_flag_and_alerts(self):
        """When VPN probe fails, _vpn_down is set and an alert is sent."""
        bot = _make_bot()

        with (
            patch("auramaur.bot._check_vpn_health", return_value=False),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is True
        assert bot._vpn_down_since is not None
        mock_alert.assert_called_once()
        call_kwargs = mock_alert.call_args
        assert "vpn_down" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_vpn_stays_down_no_duplicate_alert(self):
        """When VPN is already down and probe fails again, no new alert fires.

        The rate limiter in send_alert_rate_limited handles suppression in
        production. In the test, we verify the watchdog doesn't call the alert
        function again for the DOWN→DOWN non-transition.
        """
        bot = _make_bot()
        bot._vpn_down = True
        bot._vpn_down_since = time.monotonic() - 120  # down for 2 minutes

        with (
            patch("auramaur.bot._check_vpn_health", return_value=False),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is True
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_vpn_recovers_clears_flag_and_alerts(self):
        """When VPN comes back up after being down, _vpn_down is cleared."""
        bot = _make_bot()
        bot._vpn_down = True
        bot._vpn_down_since = time.monotonic() - 300  # was down for 5 minutes

        with (
            patch("auramaur.bot._check_vpn_health", return_value=True),
            patch("auramaur.bot.send_alert_rate_limited") as mock_alert,
        ):
            await _run_watchdog_iterations(bot, n=1)

        assert bot._vpn_down is False
        assert bot._vpn_down_since is None
        mock_alert.assert_called_once()
        call_kwargs = mock_alert.call_args
        assert "vpn_recovered" in str(call_kwargs)


class TestTradingCyclePause:
    """Test that trading cycle and market scan skip when _vpn_down is True."""

    @pytest.mark.asyncio
    async def test_trading_cycle_skips_when_vpn_down(self):
        """_task_trading_cycle skips engine.run_cycle when _vpn_down is True."""
        bot = _make_bot()
        bot._vpn_down = True

        # Mock the settings interval and adaptive interval
        bot.settings = MagicMock()
        bot.settings.intervals.analysis_seconds = 10
        bot._adaptive_interval = lambda x: 0.01  # fast sleep for test
        bot._check_kill_switch = AsyncMock(return_value=False)
        bot._last_known_cash = 100.0

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

        # Engine should NOT have been called — VPN is down
        engine.run_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_market_scan_skips_when_vpn_down(self):
        """_task_market_scan skips engine.scan_and_store_markets when _vpn_down is True."""
        bot = _make_bot()
        bot._vpn_down = True

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
```

- [ ] **Step 2: Run the new tests**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_vpn_watchdog.py -v`
Expected: All 6 tests pass (4 state-machine tests + 2 pause tests).

- [ ] **Step 3: Run the full test suite to verify no regressions**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/ -x -q`
Expected: All tests pass including the new ones.

- [ ] **Step 4: Commit**

```bash
git add tests/test_vpn_watchdog.py
git commit -m "test(bot): add VPN watchdog test suite

Six tests covering the _task_vpn_watchdog state machine and the
trading cycle pause behavior:

State machine tests:
1. VPN healthy, was healthy → no state change, no alert
2. VPN goes down → _vpn_down=True, down alert sent
3. VPN stays down → no duplicate alert (DOWN→DOWN non-transition)
4. VPN recovers → _vpn_down=False, recovery alert sent with downtime

Pause behavior tests:
5. _task_trading_cycle skips engine.run_cycle when _vpn_down=True
6. _task_market_scan skips scan_and_store_markets when _vpn_down=True

Uses lightweight mock bot to avoid full component initialization.
Follows test patterns from tests/test_retry.py.

Spec: docs/specs/2026-05-24-vpn-health-monitoring-design.md

Assisted-by: Claude (Anthropic)"
```

---

## Verification Checklist

After all tasks are complete, verify the full implementation:

1. `shellcheck -S info scripts/launchd/wrapper-gluetun.sh` — no issues
2. `uv run pytest tests/ -x -q` — all tests pass
3. `uv run ruff check auramaur/bot.py` — no lint errors
4. `uv run ruff format --check auramaur/bot.py` — properly formatted
5. Manual review: grep for `_vpn_down` in `bot.py` to confirm all three insertion points (init, watchdog method, run registration, two pause guards)
6. Verify commit history: 4 clean commits, each with `Assisted-by` attribution
