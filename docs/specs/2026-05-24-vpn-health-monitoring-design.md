# VPN Health Monitoring Design

## Problem

On 2026-05-24, the Gluetun VPN container entered a broken state: the OpenVPN
tunnel had stale routes, DNS resolution failed inside the container, and the
HTTP proxy at `localhost:8888` stopped forwarding traffic. The container
entered a healthcheck restart loop but never recovered on its own.

The Polymarket bot ran for hours in this state. Every CLOB API request timed
out through the proxy, balance reads returned 0 (the `@async_retry` fallback),
and no alert was sent. The bot appeared to crash silently from the user's
perspective — it was actually running, just unable to do anything useful.

**Root cause:** The existing supervision in `wrapper-gluetun.sh` only checks
whether the container _exists and is running_, not whether the proxy is
_actually forwarding traffic_. The bot's `@async_retry` decorator detects VPN
down and short-circuits retries, but it doesn't alert or pause trading cycles.

## Goals

1. **Detect** broken VPN proxy within 60 seconds (bot-level) and 300 seconds
   (wrapper-level).
2. **Remediate** by restarting the Gluetun container when the proxy is broken
   but the container is "running" (wrapper-level).
3. **Protect** the bot from wasting Claude API tokens on analysis that can
   never execute — pause trading cycles while VPN is down (bot-level).
4. **Alert** the user via email when VPN goes down _and_ when it recovers
   (both layers).
5. **No new dependencies.** Uses existing `_check_vpn_health()` (retry.py),
   `send_alert_rate_limited` (notify.py), `send_alert` (wrapper bash
   function), and `curl` for the wrapper probe.

## Non-Goals

- Dashboard integration (can add later).
- Automatic PIA region rotation on repeated failures.
- Separate watchdog launchd agent — the monitoring lives in the existing
  wrapper and bot processes.
- Kalshi VPN monitoring (Kalshi doesn't route through the proxy).

---

## Component 1: Gluetun Wrapper — Proxy Health Check + Auto-Restart

**File:** `scripts/launchd/wrapper-gluetun.sh`

### What changes

Add a `check_proxy_health()` function that probes the proxy after
`ensure_container()` confirms the container is running. The probe uses `curl`
through the proxy to hit an external endpoint — the same check the bot's
wrapper already uses at startup (`wrapper-bot.sh:130-142`).

Add a `send_alert()` function to the gluetun wrapper (same pattern as the
one already in `wrapper-bot.sh`) for email notifications on restart events.

### Supervision loop (modified)

The existing loop runs every `SUPERVISE_INTERVAL` seconds (default 300):

```
while true:
    sleep SUPERVISE_INTERVAL
    ensure_machine()       # existing — start Podman VM if stopped
    ensure_container()     # existing — create container if missing
    check_proxy_health()   # NEW — probe proxy, restart if broken
```

### `check_proxy_health()` logic

1. Run: `curl -sf --proxy http://localhost:8888 --max-time 10 https://ipinfo.io/ip`
2. If the probe succeeds: log the exit IP, return 0.
3. If the probe fails: log the failure, run `podman restart gluetun-vpn`,
   wait up to 60 seconds for the container to reach "healthy" status
   (polling `podman inspect` every 5s), send an email alert via
   `send_alert`, return 1.

### Alert format

```
Subject: [auramaur] gluetun: proxy broken, container restarted
Body:
  The Gluetun VPN proxy at localhost:8888 was unreachable.
  Container restarted at <timestamp>.
  New exit IP: <ip or "unknown — healthcheck still pending">
  Log: ~/Library/Logs/auramaur/gluetun.log
```

### Edge cases

- **Container not running:** `ensure_container()` handles this before
  `check_proxy_health()` runs. The health check only fires when the
  container is in "running" state.
- **Restart doesn't fix it:** The next supervision cycle (300s later) will
  detect the proxy is still broken and restart again. Repeated restarts
  will generate repeated alert emails — this is intentional so the user
  notices a pattern. If alert volume becomes excessive, we can add rate
  limiting later.
- **Podman machine down:** `ensure_machine()` runs first. If the VM is
  down, the container check is skipped entirely (existing behavior).

---

## Component 2: Bot Watchdog — `_task_vpn_watchdog`

**File:** `auramaur/bot.py`

### What changes

Add a new async task `_task_vpn_watchdog` that runs every 60 seconds. It
only starts when the bot is filtering to `polymarket` (or running all
exchanges with Polymarket included). Kalshi-only instances skip it.

Add two instance attributes initialized in `__init__`:

- `self._vpn_down: bool = False` — current VPN state
- `self._vpn_down_since: float | None = None` — `time.monotonic()` timestamp
  when VPN was first detected as down (used to calculate downtime duration
  in the recovery alert)

### State machine

```
                 probe passes
  ┌──────────┐ ────────────── ┐
  │          │                │
  │ HEALTHY  │◄───────────────┘
  │          │
  └──┬───────┘
     │ probe fails
     │ → set _vpn_down = True
     │ → log "vpn_watchdog.down"
     │ → send_alert_rate_limited(key="vpn_down", min_interval=1800)
     ▼
  ┌──────────┐
  │          │ probe fails → no action (rate limiter suppresses)
  │   DOWN   │
  │          │ probe passes
  └──┬───────┘ → set _vpn_down = False
     │         → log "vpn_watchdog.recovered"
     │         → send_alert_rate_limited(key="vpn_recovered", min_interval=60)
     ▼
  ┌──────────┐
  │ HEALTHY  │ (resumes)
  └──────────┘
```

### Probe mechanism

Reuse `_check_vpn_health()` from `auramaur/infra/retry.py` — it already
does an `aiohttp` HEAD request to `http://localhost:8888` with a 3-second
timeout. No need to duplicate this logic.

### Alert format

**Down alert:**

```
Subject: [auramaur] polymarket: VPN proxy down — trading paused
Body:
  The Gluetun VPN proxy at localhost:8888 is unreachable.
  Trading cycles are paused until the proxy recovers.
  The gluetun wrapper will attempt auto-restart.

  Time: <UTC timestamp>
  Log: ~/Library/Logs/auramaur/polymarket.log
```

**Recovery alert:**

```
Subject: [auramaur] polymarket: VPN proxy recovered — trading resumed
Body:
  The Gluetun VPN proxy is reachable again.
  Trading cycles have resumed.

  Time: <UTC timestamp>
  Downtime: ~<minutes> minutes
```

### Task registration

In `bot.run()`, add the watchdog task alongside the other always-on tasks
(kill switch, cache cleanup, recalibrate). Gate it on exchange filter:

```python
if self._exchange_filter is None or self._exchange_filter == "polymarket":
    tasks.append(
        asyncio.create_task(self._task_vpn_watchdog(), name="vpn_watchdog")
    )
```

---

## Component 3: Trading Cycle Pause

**File:** `auramaur/bot.py`

### What changes

At the top of `_task_trading_cycle` and `_task_market_scan` loop iterations,
check `self._vpn_down`. If True, log a skip and sleep for the normal
interval without running the engine cycle.

```python
if self._vpn_down:
    log.info("trading_cycle.skipped_vpn_down", exchange=name)
    await asyncio.sleep(interval)
    continue
```

### Tasks that keep running during VPN outage

These tasks either don't need the proxy or already fail gracefully:

- `_task_kill_switch_monitor` — local file check, no network
- `_task_cache_cleanup` — local cleanup, no network
- `_task_recalibrate` — reads local DB
- `_task_portfolio_monitor` — will fail to sync but catches exceptions
- `_task_vpn_watchdog` — must keep running to detect recovery
- `_task_order_monitor` — monitors existing orders, `@async_retry` handles failures
- `_task_resolution_checker` — reads DB + exchange, `@async_retry` handles failures

### Tasks that pause

- `_task_trading_cycle` — the expensive one (NLP analysis + order placement)
- `_task_market_scan` — discovery calls that timeout through broken proxy

---

## Testing

### Bot watchdog tests (`tests/test_vpn_watchdog.py`)

1. **VPN healthy — no state change:** Mock `_check_vpn_health` → True. Run
   one watchdog iteration. Assert `_vpn_down` stays False, no alert sent.
2. **VPN goes down — alert fires:** Mock `_check_vpn_health` → False. Run
   one iteration. Assert `_vpn_down` is True, `send_alert_rate_limited`
   called with key `"vpn_down"`.
3. **VPN stays down — no duplicate alert:** Mock `_check_vpn_health` →
   False twice. Run two iterations. Assert `send_alert_rate_limited`
   called once (rate limiter suppresses second).
4. **VPN recovers — recovery alert:** Mock `_check_vpn_health` → False
   then True. Run two iterations. Assert `_vpn_down` is False,
   recovery alert sent.
5. **Trading cycle skips when VPN down:** Set `_vpn_down = True`. Call
   `_task_trading_cycle` for one iteration. Assert engine.run_cycle
   was NOT called.

### Wrapper tests (manual verification)

The bash wrapper changes are tested by:

1. Stopping the Gluetun container, running the supervision loop, and
   confirming the proxy health check detects the failure and restarts.
2. Verifying the alert email is sent on restart.
3. Verifying the proxy works after restart.

---

## File Summary

| File | Change |
|------|--------|
| `scripts/launchd/wrapper-gluetun.sh` | Add `send_alert()`, `check_proxy_health()`, call from supervision loop |
| `auramaur/bot.py` | Add `_vpn_down` attr, `_task_vpn_watchdog`, pause logic in trading/scan tasks, register watchdog task |
| `tests/test_vpn_watchdog.py` | New test file for bot-level watchdog |

No new dependencies. No config changes. No new launchd agents.
