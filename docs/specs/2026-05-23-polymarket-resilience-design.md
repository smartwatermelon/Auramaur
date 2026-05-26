# Polymarket Bot Resilience: Retry, Alerting, and Crash Recovery

**Date:** 2026-05-23
**Status:** Draft

## Problem

The Polymarket bot exits silently when it encounters sustained CLOB API
timeouts (SSL handshake timeouts, read timeouts, 503s). Launchd restarts it
via `KeepAlive: Crashed`, but there is no alerting, no retry logic on
individual API calls, and no crash-rate tracking. The operator doesn't know
the bot is down until they manually check.

Observed failure on 2026-05-23: ~19 consecutive CLOB timeout errors over
~40 minutes, then the bot died. Launchd restarted it, but it kept dying.
Root cause was likely a VPN hiccup or Polymarket API instability.

## Solution

Three focused components, no new dependencies:

1. **Retry decorator** — async retry with exponential backoff on
   PolymarketClient methods, with VPN health probing
2. **Wrapper crash counter** — shell-level crash tracking in wrapper-bot.sh,
   msmtp email after repeated crashes
3. **Python notify module** — lightweight email alerting from within the bot
   process for retry-exhaustion events

### Non-goals

- Full circuit breaker pattern (future enhancement if needed)
- Auto-recovery (restart VPN, flush connection pool) — flagged as future work
- Kalshi bot hardening (Kalshi uses a different client with no VPN dependency)
- New external dependencies (tenacity, etc.)

---

## Component 1: Retry Decorator

**File:** `auramaur/infra/retry.py`

### Interface

```python
def async_retry(
    max_attempts: int = 3,
    backoff_seconds: list[float] = [2, 5, 15],
    retryable: tuple[type[Exception], ...] = (
        TimeoutError, ConnectionError, OSError,
        RequestException,  # requests.exceptions — covers SDK errors
    ),
    on_exhausted: Callable[[Exception], None] | None = None,
) -> Callable:
    """Decorator for async methods. Retries on transient network errors."""
```

### Behavior

1. Call the wrapped function.
2. On a retryable exception, log a warning:
   `retry.attempt attempt={n} method={name} error={type} backoff={seconds}s`
3. Before sleeping, probe VPN health: `GET http://localhost:8888` with 3s
   timeout. If the probe fails, skip remaining retries and raise immediately
   — no point retrying through a dead proxy. Log:
   `retry.vpn_down method={name} — skipping remaining retries`
4. Sleep for `backoff_seconds[attempt - 1]`.
5. After all attempts exhausted, call `on_exhausted(exception)` if provided,
   then re-raise the original exception.

The caller's existing try/except handles the re-raised exception exactly as
before (returning sentinel values like empty OrderResult, 0.0 balance, etc.).
The retry is transparent to the rest of the codebase.

### VPN health probe

```python
async def _check_vpn_health(timeout: float = 3.0) -> bool:
    """Returns True if the Gluetun HTTP proxy at localhost:8888 is reachable."""
```

Uses `aiohttp.ClientSession` with a HEAD request to `http://localhost:8888`.
Returns True on any response (even 4xx — the proxy is alive). Returns False
on connection refused, timeout, or OSError.

### Applied to PolymarketClient methods

| Method                  | Retried | Notes                                    |
|-------------------------|---------|------------------------------------------|
| `place_order()`         | Yes     | Primary failure point observed           |
| `get_balance_allowance` | Yes     | Capital detection in broker/sync, bot.py |
| `get_order_book()`      | Yes     | Price discovery                          |
| `_load_real_positions()`| Yes     | Portfolio sync                           |
| `cancel_order()`        | Yes     | Order lifecycle                          |
| `get_order_status()`    | Yes     | Order lifecycle                          |
| `poll_until_terminal()` | No      | Has its own 300s polling loop            |

### Retryable exceptions

The py-clob-client-v2 SDK surfaces network errors as:

- `requests.exceptions.ReadTimeout` (subclass of `ConnectionError`)
- `requests.exceptions.SSLError` (subclass of `ConnectionError`)
- `requests.exceptions.ConnectionError`
- `urllib3.exceptions.TimeoutError`
- Generic `Exception` with message containing "timed out", "503", or
  "Service Unavailable"

The decorator catches `(TimeoutError, ConnectionError, OSError)` by default.
For the SDK's string-message errors, PolymarketClient methods already catch
generic Exception — the retry wraps the outer method, so it sees the
re-raised exception after the method's own error handling. We add
`requests.exceptions.RequestException` to the retryable set to cover SDK
errors that don't map cleanly to the standard hierarchy.

### on_exhausted callback

When all retries fail on `place_order` or `get_balance_allowance`, the
callback sends a rate-limited alert email via `auramaur.infra.notify`.

---

## Component 2: Wrapper Crash Counter

**File:** `scripts/launchd/wrapper-bot.sh` (modifications to existing file)

### Crash tracking

**State file:** `/tmp/auramaur-${EXCHANGE}-crashes`

Each line is a Unix timestamp of a crash. On wrapper startup:

1. Read the crash file (create if absent).
2. Filter to entries within the last 600 seconds (10 minutes).
3. Rewrite the file with only recent entries + current timestamp.
4. If count >= 3: send alert email, then `exit 0` (clean exit stops
   `KeepAlive: Crashed` from restarting).

**Successful boot detection:** Before `exec`ing the bot, write the current
timestamp to `/tmp/auramaur-${EXCHANGE}-started`. On the *next* wrapper
invocation (crash recovery), if the started-timestamp is >60 seconds old,
the bot ran for a while — clear the crash history. This prevents a single
long-running session's eventual crash from counting toward the "3 in 10 min"
threshold.

### Alert email

```bash
send_alert() {
  local subject="$1" body="$2"
  printf 'From: %s\nTo: %s\nSubject: %s\n\n%s\n' \
    "${MAIL_FROM}" "${MAIL_TO}" "${subject}" "${body}" \
    | msmtp -a gmail "${MAIL_TO}"
}
```

**Subject:** `[auramaur] ${EXCHANGE} bot: ${crash_count} crashes in 10 minutes`

**Body:**

```
Auramaur ${EXCHANGE} bot has crashed ${crash_count} times in the last 10 minutes.
Automatic restarts have been suspended. Manual intervention required.

Last crash: ${timestamp}
Log: ~/Library/Logs/auramaur/${EXCHANGE}.log

To restart: launchctl start com.auramaur.${EXCHANGE}
```

**Configuration (env vars with defaults):**

- `AURAMAUR_ALERT_TO` — defaults to `andrew.rich@gmail.com`
- `AURAMAUR_ALERT_FROM` — defaults to `andrew.rich@gmail.com`
- `AURAMAUR_CRASH_THRESHOLD` — defaults to `3`
- `AURAMAUR_CRASH_WINDOW` — defaults to `600` (seconds)

**msmtp dependency:** Uses `~/.msmtprc` with the gmail account. Password
comes from `ralph-nightly.keychain-db` via `passwordeval`. No new keychain
setup needed.

### Future enhancement: auto-recovery

The crash counter `exit 0` is the plug-in point for auto-recovery. A future
version could:

1. Before giving up, attempt `launchctl stop/start com.auramaur.gluetun`
2. Wait 30s for VPN to re-establish
3. Probe `localhost:8888`
4. If VPN is back, reset crash counter and restart bot
5. If VPN is still down, send alert and exit

---

## Component 3: Python Notify Module

**File:** `auramaur/infra/notify.py`

### Interface

```python
def send_alert(subject: str, body: str) -> bool:
    """Send an alert email via msmtp. Returns True on success."""

_last_sent: dict[str, float] = {}

def send_alert_rate_limited(
    subject: str,
    body: str,
    key: str = "default",
    min_interval: float = 1800,  # 30 minutes
) -> bool:
    """Send alert if at least min_interval seconds since last alert with this key."""
```

### Behavior

- Shells out to `msmtp -a gmail` via `subprocess.run` with 30s timeout
- RFC822 headers: From, To, Subject, Date
- Header injection prevention: strip `\r` and `\n` from subject
- `AURAMAUR_ALERT_TO` env var (defaults to `andrew.rich@gmail.com`)
- No-op with warning log if `msmtp` is not found on PATH
- Returns False on any failure (timeout, msmtp exit code, missing binary)
- Rate limiting is per-key, in-process only (resets on bot restart)

### Integration with retry decorator

The retry decorator's `on_exhausted` callback for critical methods:

```python
def _on_order_retry_exhausted(exc: Exception) -> None:
    send_alert_rate_limited(
        subject=f"[auramaur] polymarket: order failed after 3 retries",
        body=f"place_order failed after 3 attempts.\nLast error: {exc}\n...",
        key="place_order",
    )
```

Similar callbacks for `get_balance_allowance` (key="balance").

---

## Testing

### Retry decorator tests (`tests/test_retry.py`)

- Test successful call (no retry)
- Test retry on TimeoutError, success on 2nd attempt
- Test all retries exhausted, exception re-raised
- Test VPN probe failure skips remaining retries
- Test on_exhausted callback fires after exhaustion
- Test non-retryable exception is not retried

### Notify tests (`tests/test_notify.py`)

- Test send_alert shells out to msmtp with correct args
- Test header injection prevention
- Test rate limiting (second call within window is suppressed)
- Test no-op when msmtp not installed
- Test timeout handling

### Wrapper crash counter (manual verification)

- Shell script logic tested via manual crash simulation
- Verify crash file is written/read correctly
- Verify alert email is sent after threshold
- Verify clean exit stops KeepAlive restart

---

## Files changed

| File | Change |
|------|--------|
| `auramaur/infra/__init__.py` | New (empty) |
| `auramaur/infra/retry.py` | New — async_retry decorator |
| `auramaur/infra/notify.py` | New — msmtp alert wrapper |
| `auramaur/exchange/client.py` | Apply @async_retry to 6 methods |
| `scripts/launchd/wrapper-bot.sh` | Add crash counter + send_alert |
| `tests/test_retry.py` | New — retry decorator tests |
| `tests/test_notify.py` | New — notify module tests |

No changes to bot.py, engine.py, or broker/sync.py. The retry decorator
wraps PolymarketClient methods transparently.

---

## Rollback

The retry decorator is additive — removing it returns to current behavior
(single attempt, sentinel on failure). The wrapper crash counter is a new
code path that only activates after 3 crashes; removing it returns to
unlimited KeepAlive restarts. Both can be disabled independently.
