# Polymarket Bot Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add retry-with-backoff, VPN health probing, crash-rate alerting, and in-process email notification to the Polymarket bot so it survives transient CLOB API failures and alerts the operator when it can't recover.

**Architecture:** Three independent components: (1) an `async_retry` decorator in `auramaur/infra/retry.py` applied to 6 PolymarketClient methods, with a VPN health probe that short-circuits when the proxy is down; (2) a Python `notify` module in `auramaur/infra/notify.py` that shells out to `msmtp` for rate-limited email alerts; (3) a crash counter in the existing `wrapper-bot.sh` that tracks rapid restarts and emails the operator before suspending automatic restarts.

**Tech Stack:** Python 3.11+ asyncio, aiohttp (for VPN probe), msmtp (existing), pytest-asyncio, structlog.

**Spec:** `docs/specs/2026-05-23-polymarket-resilience-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `auramaur/infra/__init__.py` | Create (empty) | Package marker |
| `auramaur/infra/retry.py` | Create | `async_retry` decorator + `_check_vpn_health` |
| `auramaur/infra/notify.py` | Create | `send_alert` + `send_alert_rate_limited` via msmtp |
| `tests/test_retry.py` | Create | 6 tests for retry decorator |
| `tests/test_notify.py` | Create | 5 tests for notify module |
| `auramaur/exchange/client.py` | Modify (lines 1-2, 57, 280, 418, 426, 471, 483, 554) | Apply `@async_retry` to 6 methods + wire `on_exhausted` |
| `scripts/launchd/wrapper-bot.sh` | Modify (insert after line 120) | Crash counter + `send_alert` shell function |

---

### Task 1: Create infra package and notify module (TDD)

**Files:**

- Create: `auramaur/infra/__init__.py`
- Create: `auramaur/infra/notify.py`
- Create: `tests/test_notify.py`

- [ ] **Step 1: Create package marker**

```bash
mkdir -p auramaur/infra
touch auramaur/infra/__init__.py
```

- [ ] **Step 2: Write failing tests for notify module**

Create `tests/test_notify.py`:

```python
"""Tests for auramaur.infra.notify — msmtp email alerting."""

import subprocess
import time
from unittest.mock import patch, MagicMock

import pytest

from auramaur.infra.notify import send_alert, send_alert_rate_limited, _last_sent


class TestSendAlert:
    """Test send_alert shells out to msmtp correctly."""

    @patch("shutil.which", return_value="/opt/homebrew/bin/msmtp")
    @patch("subprocess.run")
    def test_sends_via_msmtp(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)

        result = send_alert("Test Subject", "Test body")

        assert result is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args.kwargs["timeout"] == 30
        # msmtp should receive the To address as the last positional arg
        cmd = call_args[0][0]
        assert cmd[0] == "/opt/homebrew/bin/msmtp"
        assert "-a" in cmd and "gmail" in cmd
        # The message fed via stdin should have RFC822 headers
        stdin_text = call_args.kwargs["input"]
        assert "Subject: Test Subject" in stdin_text
        assert "Test body" in stdin_text

    @patch("shutil.which", return_value="/opt/homebrew/bin/msmtp")
    @patch("subprocess.run")
    def test_header_injection_prevention(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0)

        send_alert("Bad\r\nBcc: attacker@evil.com\nSubject", "body")

        stdin_text = mock_run.call_args.kwargs["input"]
        assert "\r" not in stdin_text.split("\n\n")[0]
        assert "Bcc:" not in stdin_text.split("\n\n")[0]

    @patch("shutil.which", return_value=None)
    def test_noop_when_msmtp_missing(self, mock_which):
        result = send_alert("Subject", "Body")
        assert result is False

    @patch("shutil.which", return_value="/opt/homebrew/bin/msmtp")
    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="msmtp", timeout=30))
    def test_timeout_returns_false(self, mock_run, mock_which):
        result = send_alert("Subject", "Body")
        assert result is False

    @patch("shutil.which", return_value="/opt/homebrew/bin/msmtp")
    @patch("subprocess.run")
    def test_nonzero_exit_returns_false(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=1)
        result = send_alert("Subject", "Body")
        assert result is False


class TestSendAlertRateLimited:
    """Test rate-limiting prevents email storms."""

    def setup_method(self):
        _last_sent.clear()

    @patch("auramaur.infra.notify.send_alert", return_value=True)
    def test_first_call_sends(self, mock_send):
        result = send_alert_rate_limited("Subj", "Body", key="test_key")
        assert result is True
        mock_send.assert_called_once_with("Subj", "Body")

    @patch("auramaur.infra.notify.send_alert", return_value=True)
    def test_second_call_within_window_suppressed(self, mock_send):
        send_alert_rate_limited("Subj", "Body", key="test_key", min_interval=1800)
        result = send_alert_rate_limited("Subj", "Body", key="test_key", min_interval=1800)
        assert result is False
        assert mock_send.call_count == 1

    @patch("auramaur.infra.notify.send_alert", return_value=True)
    def test_different_keys_independent(self, mock_send):
        send_alert_rate_limited("Subj", "Body", key="key_a")
        result = send_alert_rate_limited("Subj", "Body", key="key_b")
        assert result is True
        assert mock_send.call_count == 2

    @patch("auramaur.infra.notify.send_alert", return_value=True)
    def test_sends_after_interval_expires(self, mock_send):
        _last_sent["test_key"] = time.monotonic() - 2000
        result = send_alert_rate_limited("Subj", "Body", key="test_key", min_interval=1800)
        assert result is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_notify.py -v`

Expected: `ModuleNotFoundError: No module named 'auramaur.infra.notify'`

- [ ] **Step 4: Implement notify module**

Create `auramaur/infra/notify.py`:

```python
"""Lightweight email alerting via msmtp — no external dependencies."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from email.utils import formatdate

import structlog

log = structlog.get_logger()

_last_sent: dict[str, float] = {}

_DEFAULT_TO = "andrew.rich@gmail.com"


def send_alert(subject: str, body: str) -> bool:
    """Send an alert email via msmtp. Returns True on success."""
    msmtp = shutil.which("msmtp")
    if not msmtp:
        log.warning("notify.msmtp_not_found")
        return False

    to_addr = os.environ.get("AURAMAUR_ALERT_TO", _DEFAULT_TO)
    from_addr = os.environ.get("AURAMAUR_ALERT_FROM", _DEFAULT_TO)

    safe_subject = subject.replace("\r", "").replace("\n", " ")

    message = (
        f"From: {from_addr}\n"
        f"To: {to_addr}\n"
        f"Subject: {safe_subject}\n"
        f"Date: {formatdate(localtime=True)}\n"
        f"\n"
        f"{body}\n"
    )

    try:
        result = subprocess.run(
            [msmtp, "-a", "gmail", to_addr],
            input=message,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("notify.msmtp_failed", returncode=result.returncode, stderr=result.stderr[:200])
            return False
        log.info("notify.sent", to=to_addr, subject=safe_subject[:60])
        return True
    except subprocess.TimeoutExpired:
        log.error("notify.msmtp_timeout")
        return False
    except Exception as e:
        log.error("notify.send_error", error=str(e))
        return False


def send_alert_rate_limited(
    subject: str,
    body: str,
    key: str = "default",
    min_interval: float = 1800,
) -> bool:
    """Send alert if at least min_interval seconds since last alert with this key."""
    now = time.monotonic()
    last = _last_sent.get(key, 0.0)
    if now - last < min_interval:
        log.debug("notify.rate_limited", key=key, seconds_since_last=round(now - last))
        return False

    sent = send_alert(subject, body)
    if sent:
        _last_sent[key] = now
    return sent
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_notify.py -v`

Expected: All 9 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add auramaur/infra/__init__.py auramaur/infra/notify.py tests/test_notify.py
git commit -m "feat(infra): add msmtp email notify module with rate limiting

Lightweight email alerting via msmtp for bot failure notifications.
Includes rate-limiting to prevent alert storms during sustained outages.

Assisted-by: Claude (Anthropic)"
```

---

### Task 2: Create async retry decorator with VPN probe (TDD)

**Files:**

- Create: `auramaur/infra/retry.py`
- Create: `tests/test_retry.py`

- [ ] **Step 1: Write failing tests for retry decorator**

Create `tests/test_retry.py`:

```python
"""Tests for auramaur.infra.retry — async retry with VPN health probe."""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from auramaur.infra.retry import async_retry


class TestAsyncRetry:
    """Test the async_retry decorator."""

    @pytest.mark.asyncio
    async def test_successful_call_no_retry(self):
        call_count = 0

        @async_retry(max_attempts=3, backoff_seconds=[0.01, 0.01, 0.01])
        async def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await succeed()
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_timeout_then_success(self):
        call_count = 0

        @async_retry(max_attempts=3, backoff_seconds=[0.01, 0.01, 0.01])
        async def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("connection timed out")
            return "recovered"

        with patch("auramaur.infra.retry._check_vpn_health", return_value=True):
            result = await fail_then_succeed()
        assert result == "recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_reraises(self):
        @async_retry(max_attempts=3, backoff_seconds=[0.01, 0.01, 0.01])
        async def always_fail():
            raise ConnectionError("refused")

        with patch("auramaur.infra.retry._check_vpn_health", return_value=True):
            with pytest.raises(ConnectionError, match="refused"):
                await always_fail()

    @pytest.mark.asyncio
    async def test_vpn_down_skips_remaining_retries(self):
        call_count = 0

        @async_retry(max_attempts=3, backoff_seconds=[0.01, 0.01, 0.01])
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise TimeoutError("timed out")

        with patch("auramaur.infra.retry._check_vpn_health", return_value=False):
            with pytest.raises(TimeoutError):
                await always_fail()
        assert call_count == 1  # Only one attempt — VPN down skipped retries

    @pytest.mark.asyncio
    async def test_on_exhausted_callback_fires(self):
        captured_exc = None

        def on_exhausted(exc):
            nonlocal captured_exc
            captured_exc = exc

        @async_retry(
            max_attempts=2,
            backoff_seconds=[0.01, 0.01],
            on_exhausted=on_exhausted,
        )
        async def always_fail():
            raise OSError("network unreachable")

        with patch("auramaur.infra.retry._check_vpn_health", return_value=True):
            with pytest.raises(OSError):
                await always_fail()
        assert captured_exc is not None
        assert "network unreachable" in str(captured_exc)

    @pytest.mark.asyncio
    async def test_non_retryable_exception_not_retried(self):
        call_count = 0

        @async_retry(max_attempts=3, backoff_seconds=[0.01, 0.01, 0.01])
        async def bad_input():
            nonlocal call_count
            call_count += 1
            raise ValueError("invalid token_id")

        with pytest.raises(ValueError, match="invalid token_id"):
            await bad_input()
        assert call_count == 1  # Not retried


class TestAsyncRetryOnSyncMethod:
    """Test that async_retry works on sync methods wrapped as coroutines."""

    @pytest.mark.asyncio
    async def test_sync_method_retried(self):
        call_count = 0

        @async_retry(max_attempts=3, backoff_seconds=[0.01, 0.01, 0.01])
        async def sync_wrapper():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("reset")
            return 42

        with patch("auramaur.infra.retry._check_vpn_health", return_value=True):
            result = await sync_wrapper()
        assert result == 42
        assert call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_retry.py -v`

Expected: `ModuleNotFoundError: No module named 'auramaur.infra.retry'`

- [ ] **Step 3: Implement retry decorator**

Create `auramaur/infra/retry.py`:

```python
"""Async retry decorator with exponential backoff and VPN health probing."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any

import structlog

log = structlog.get_logger()


async def _check_vpn_health(timeout: float = 3.0) -> bool:
    """Returns True if the Gluetun HTTP proxy at localhost:8888 is reachable."""
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.head(
                "http://localhost:8888", timeout=aiohttp.ClientTimeout(total=timeout)
            ):
                return True
    except Exception:
        return False


_DEFAULT_RETRYABLE: tuple[type[Exception], ...] = (TimeoutError, ConnectionError, OSError)
try:
    from requests.exceptions import RequestException as _ReqExc

    _DEFAULT_RETRYABLE = (*_DEFAULT_RETRYABLE, _ReqExc)
except ImportError:
    pass


def async_retry(
    max_attempts: int = 3,
    backoff_seconds: list[float] | None = None,
    retryable: tuple[type[Exception], ...] | None = None,
    on_exhausted: Callable[[Exception], None] | None = None,
) -> Callable:
    """Decorator for async methods. Retries on transient network errors."""
    if backoff_seconds is None:
        backoff_seconds = [2, 5, 15]
    if retryable is None:
        retryable = _DEFAULT_RETRYABLE

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_error: Exception | None = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:
                    if not isinstance(e, retryable):
                        raise

                    last_error = e
                    if attempt >= max_attempts:
                        break

                    log.warning(
                        "retry.attempt",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        method=fn.__qualname__,
                        error=type(e).__name__,
                        backoff=backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)],
                    )

                    vpn_ok = await _check_vpn_health()
                    if not vpn_ok:
                        log.error(
                            "retry.vpn_down",
                            method=fn.__qualname__,
                        )
                        break

                    delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                    await asyncio.sleep(delay)

            if on_exhausted is not None and last_error is not None:
                on_exhausted(last_error)

            raise last_error  # type: ignore[misc]

        return wrapper

    return decorator
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_retry.py -v`

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add auramaur/infra/retry.py tests/test_retry.py
git commit -m "feat(infra): add async_retry decorator with VPN health probe

Exponential backoff on transient network errors (TimeoutError,
ConnectionError, OSError). VPN probe at localhost:8888 short-circuits
when the Gluetun proxy is unreachable. Optional on_exhausted callback
for alerting.

Assisted-by: Claude (Anthropic)"
```

---

### Task 3: Apply @async_retry to PolymarketClient methods

**Files:**

- Modify: `auramaur/exchange/client.py`

This task modifies 6 methods to use the retry decorator. The decorator wraps the outer method, so existing try/except and sentinel returns are preserved — the retry is transparent.

Because some methods (`_load_real_positions`, `_submit_clob_order`) are sync, and the decorator is async, we need to wrap the sync CLOB calls in async wrappers within those methods. The approach: make the method `async` and use `await asyncio.to_thread()` for the blocking SDK call, or simply make the method async and call the sync SDK inline (acceptable since the bot event loop tolerates brief blocking I/O from the SDK). The simpler approach: apply the decorator to the existing async methods and, for the two sync methods, convert them to async.

**Important:** `_load_real_positions` is called from the sync `get_sellable_token_id` (line 117). We need to handle this by making `get_sellable_token_id` also async, OR by keeping `_load_real_positions` sync and NOT applying the retry decorator to it (instead, wrap the CLOB call inside it in a separate retryable helper). The spec says to retry it, but changing `get_sellable_token_id` to async would cascade changes. The pragmatic approach: extract the CLOB network call into a small async helper that the retry decorator wraps, and `_load_real_positions` remains sync, calling it via `asyncio.run()` only when needed. **Simpler still:** the decorator can detect sync functions and handle them. But let's not over-engineer. The real failure mode observed is on `place_order`, `get_order_book`, `cancel_order`, `get_order_status`, and `cancel_open_orders_for_token` — all already async. `_load_real_positions` is sync but rarely fails in the observed crash pattern. We'll apply retry to the 5 async methods now and leave `_load_real_positions` for a follow-up if needed.

**Methods to decorate (all async, no signature changes needed):**

| Method | Line | on_exhausted callback |
|--------|------|----------------------|
| `place_order` | 280 | Yes — `_on_order_retry_exhausted` |
| `get_order_status` | 426 | No |
| `cancel_order` | 471 | No |
| `cancel_open_orders_for_token` | 483 | No |
| `get_order_book` | 554 | No |

For `_submit_clob_order` (line 418): this is a sync helper called from within `place_order`'s try block. Since `place_order` itself is retried, `_submit_clob_order` doesn't need its own retry — the retry wraps the whole `place_order` method.

- [ ] **Step 1: Add imports and on_exhausted callback to client.py**

At the top of `auramaur/exchange/client.py`, after the existing imports (after line 20), add:

```python
from auramaur.infra.retry import async_retry
from auramaur.infra.notify import send_alert_rate_limited
```

Before the `PolymarketClient` class (after line 35), add the callback functions:

```python
def _on_order_retry_exhausted(exc: Exception) -> None:
    send_alert_rate_limited(
        subject="[auramaur] polymarket: order failed after retries",
        body=f"place_order failed after all retry attempts.\nLast error: {exc}\n",
        key="place_order",
    )
```

- [ ] **Step 2: Apply @async_retry to place_order**

The retry must wrap the LIVE ORDER PATH only — paper trades don't hit the network. Since the decorator wraps the entire method, paper orders will pass through on the first attempt (no exception = no retry). The decorator is transparent.

Add the decorator above `async def place_order` at line 280:

```python
    @async_retry(
        max_attempts=3,
        backoff_seconds=[2, 5, 15],
        on_exhausted=_on_order_retry_exhausted,
    )
    async def place_order(self, order: Order) -> OrderResult:
```

**Note:** The existing `except Exception` at line 408 catches CLOB errors and returns a sentinel `OrderResult(status="rejected")`. For retry to work, the exception must propagate. But the spec says "The caller's existing try/except handles the re-raised exception exactly as before" — meaning the retry wraps the *outer* method. When `place_order` catches the exception internally at line 408 and returns a sentinel, retry never fires because no exception escapes. This is actually fine for the observed failure pattern: when the bot dies, it's because the exception is NOT caught (e.g., SSL errors during `_init_clob_client` or during `create_and_post_order` in ways that bypass the generic catch).

However, to make retry effective on CLOB network errors that ARE caught, we need the `_submit_clob_order` call to NOT be caught by the inner try/except for retryable exceptions. The cleanest approach: re-raise retryable exceptions before the generic catch.

Modify the try/except in `place_order` at lines 322-416:

```python
        try:
            # ... existing code lines 323-406 unchanged ...
        except (TimeoutError, ConnectionError, OSError) as e:
            log.error("order.live_transient_error", error=str(e), market_id=order.market_id)
            raise  # Let @async_retry handle retryable errors
        except Exception as e:
            log.error("order.live_error", error=str(e), market_id=order.market_id)
            return OrderResult(
                order_id="ERROR",
                market_id=order.market_id,
                status="rejected",
                is_paper=False,
                error_message=str(e)[:200],
            )
```

The `(TimeoutError, ConnectionError, OSError)` catch re-raises transient errors so the `@async_retry` decorator can handle them. `requests.exceptions.RequestException` is included in the decorator's default `retryable` tuple (via `_DEFAULT_RETRYABLE`), so SDK-level exceptions that escape the standard hierarchy are also retried.

- [ ] **Step 3: Apply @async_retry to get_order_status**

Add decorator above `async def get_order_status` at line 426:

```python
    @async_retry(max_attempts=3, backoff_seconds=[2, 5, 15])
    async def get_order_status(self, order_id: str) -> OrderResult:
```

The existing `except Exception as e: ... raise` at lines 437-439 already re-raises, so retry works naturally.

- [ ] **Step 4: Apply @async_retry to cancel_order**

Add decorator above `async def cancel_order` at line 471. The existing except catches and returns False. For retry to work on transient errors, re-raise retryable ones:

```python
    @async_retry(max_attempts=3, backoff_seconds=[2, 5, 15])
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a live order on the CLOB. Returns True on success."""
        self._init_clob_client()
        try:
            self._clob_client.cancel(order_id)
            self._live_pending.pop(order_id, None)
            log.info("order.cancelled", order_id=order_id)
            return True
        except (TimeoutError, ConnectionError, OSError) as e:
            log.error("order_cancel.transient_error", order_id=order_id, error=str(e))
            raise
        except Exception as e:
            log.error("order_cancel.error", order_id=order_id, error=str(e))
            return False
```

- [ ] **Step 5: Apply @async_retry to cancel_open_orders_for_token**

Add decorator above `async def cancel_open_orders_for_token` at line 483:

```python
    @async_retry(max_attempts=3, backoff_seconds=[2, 5, 15])
    async def cancel_open_orders_for_token(self, token_id: str) -> int:
```

Same pattern — re-raise retryable exceptions before the generic catch. Modify the except block (lines 513-519):

```python
        except (TimeoutError, ConnectionError, OSError) as e:
            log.warning("order.stale_cancel_transient", token_id=token_id[:20], error=str(e)[:200])
            raise
        except Exception as e:
            log.warning(
                "order.stale_cancel_error",
                token_id=token_id[:20],
                error=str(e)[:200],
            )
            return 0
```

- [ ] **Step 6: Apply @async_retry to get_order_book**

Add decorator above `async def get_order_book` at line 554:

```python
    @async_retry(max_attempts=3, backoff_seconds=[2, 5, 15])
    async def get_order_book(self, token_id: str) -> OrderBook:
```

Same pattern — re-raise retryable exceptions:

```python
        except (TimeoutError, ConnectionError, OSError) as e:
            log.error("orderbook.transient_error", token_id=token_id[:20], error=str(e))
            raise
        except Exception as e:
            log.error("orderbook.error", token_id=token_id[:20], error=str(e))
            return OrderBook()
```

- [ ] **Step 7: Verify RequestException coverage**

No per-method changes needed. The `async_retry` decorator's `_DEFAULT_RETRYABLE` tuple includes `RequestException` via a guarded import at module level (`auramaur/infra/retry.py`). All 5 decorated methods use the default tuple (no explicit `retryable=` override except where `on_exhausted` is also passed), so SDK exceptions are covered automatically.

- [ ] **Step 8: Run existing tests to verify no regressions**

Run: `uv run pytest tests/ -v --timeout=60`

Expected: All existing tests pass. The decorator is transparent — paper trades don't raise network errors, so retry never activates.

- [ ] **Step 9: Commit**

```bash
git add auramaur/exchange/client.py
git commit -m "feat(exchange): apply async_retry to PolymarketClient API methods

Retry with exponential backoff (2s, 5s, 15s) on transient network
errors for place_order, get_order_status, cancel_order,
cancel_open_orders_for_token, and get_order_book. VPN health probe
short-circuits when the Gluetun proxy is unreachable. Alert email
sent when place_order exhausts all retries.

Assisted-by: Claude (Anthropic)"
```

---

### Task 4: Add crash counter and alerting to wrapper-bot.sh

**Files:**

- Modify: `scripts/launchd/wrapper-bot.sh` (insert between VPN check and bot exec)

- [ ] **Step 1: Add send_alert function to wrapper-bot.sh**

Insert after the `log()` function definition (after line 32), before `unlock_keychain()`:

```bash
# --- Alert email via msmtp ---
MAIL_TO="${AURAMAUR_ALERT_TO:-andrew.rich@gmail.com}"
MAIL_FROM="${AURAMAUR_ALERT_FROM:-andrew.rich@gmail.com}"

send_alert() {
  local subject="$1" body="$2"
  if ! command -v msmtp >/dev/null 2>&1; then
    log "WARN: msmtp not found — alert not sent: ${subject}"
    return 1
  fi
  printf 'From: %s\nTo: %s\nSubject: %s\n\n%s\n' \
    "${MAIL_FROM}" "${MAIL_TO}" "${subject}" "${body}" \
    | msmtp -a gmail "${MAIL_TO}" 2>/dev/null
  local rc=$?
  if [[ ${rc} -eq 0 ]]; then
    log "Alert sent: ${subject}"
  else
    log "WARN: msmtp failed (rc=${rc}) — alert not sent: ${subject}"
  fi
  return ${rc}
}
```

- [ ] **Step 2: Add crash counter logic before bot exec**

Insert after the VPN check block (after line 120) and before the "Starting bot" log (line 122):

```bash
# --- Crash counter: detect rapid crash loops ---
CRASH_THRESHOLD="${AURAMAUR_CRASH_THRESHOLD:-3}"
CRASH_WINDOW="${AURAMAUR_CRASH_WINDOW:-600}"
CRASH_FILE="/tmp/auramaur-${EXCHANGE}-crashes"
STARTED_FILE="/tmp/auramaur-${EXCHANGE}-started"

now=$(date +%s)

# If the bot ran for >60s last time, it was a healthy session — clear history
if [[ -f "${STARTED_FILE}" ]]; then
  last_start=$(cat "${STARTED_FILE}" 2>/dev/null || echo "0")
  elapsed=$((now - last_start))
  if [[ ${elapsed} -gt 60 ]]; then
    rm -f "${CRASH_FILE}"
  fi
fi

# Record this crash (wrapper runs = bot died or first start)
if [[ -f "${CRASH_FILE}" ]]; then
  # Filter to entries within the crash window
  recent_crashes=""
  while IFS= read -r ts; do
    [[ -z "${ts}" ]] && continue
    age=$((now - ts))
    if [[ ${age} -le ${CRASH_WINDOW} ]]; then
      recent_crashes="${recent_crashes}${ts}\n"
    fi
  done < "${CRASH_FILE}"
  printf '%b%s\n' "${recent_crashes}" "${now}" > "${CRASH_FILE}"
else
  echo "${now}" > "${CRASH_FILE}"
fi

# Count recent crashes
crash_count=0
while IFS= read -r ts; do
  [[ -n "${ts}" ]] && crash_count=$((crash_count += 1))
done < "${CRASH_FILE}"

if [[ ${crash_count} -ge ${CRASH_THRESHOLD} ]]; then
  log "ERROR: ${crash_count} crashes in ${CRASH_WINDOW}s — suspending auto-restart"
  send_alert \
    "[auramaur] ${EXCHANGE} bot: ${crash_count} crashes in $((CRASH_WINDOW / 60)) minutes" \
    "Auramaur ${EXCHANGE} bot has crashed ${crash_count} times in the last $((CRASH_WINDOW / 60)) minutes.
Automatic restarts have been suspended. Manual intervention required.

Last crash: $(date -u '+%Y-%m-%dT%H:%M:%SZ')
Log: ~/Library/Logs/auramaur/${EXCHANGE}.log

To restart: launchctl start com.auramaur.${EXCHANGE}"
  exit 0  # Clean exit — stops KeepAlive:Crashed from restarting
fi

# Record start time for healthy-session detection
echo "${now}" > "${STARTED_FILE}"
```

- [ ] **Step 3: Run shellcheck on modified script**

Run: `shellcheck -S info scripts/launchd/wrapper-bot.sh`

Expected: No errors, warnings, or info-level issues.

- [ ] **Step 4: Manual verification**

Test the crash counter logic:

```bash
# Simulate 3 rapid crashes
EXCHANGE=test
CRASH_FILE="/tmp/auramaur-test-crashes"
rm -f "${CRASH_FILE}" "/tmp/auramaur-test-started"

now=$(date +%s)
printf '%s\n%s\n%s\n' "$((now - 30))" "$((now - 15))" "${now}" > "${CRASH_FILE}"
cat "${CRASH_FILE}"
wc -l < "${CRASH_FILE}"  # Should show 3
```

- [ ] **Step 5: Commit**

```bash
git add scripts/launchd/wrapper-bot.sh
git commit -m "feat(launchd): add crash counter and email alerting to wrapper-bot.sh

Tracks crash timestamps in /tmp/auramaur-\${EXCHANGE}-crashes. After 3
crashes in 10 minutes, sends an alert via msmtp and exits cleanly to
stop launchd's KeepAlive:Crashed from restarting. Long-running sessions
(>60s) clear the crash history on next start.

Assisted-by: Claude (Anthropic)"
```

---

### Task 5: Integration verification

**Files:** None (read-only verification)

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/ -v --timeout=120`

Expected: All tests pass including new `test_retry.py` and `test_notify.py`.

- [ ] **Step 2: Lint check**

Run: `uv run ruff check auramaur/infra/ tests/test_retry.py tests/test_notify.py auramaur/exchange/client.py`

Fix any issues found.

Run: `uv run ruff format auramaur/infra/ tests/test_retry.py tests/test_notify.py auramaur/exchange/client.py`

- [ ] **Step 3: Verify decorator import chain**

```bash
uv run python -c "from auramaur.infra.retry import async_retry; print('retry OK')"
uv run python -c "from auramaur.infra.notify import send_alert, send_alert_rate_limited; print('notify OK')"
```

Expected: Both print "OK" with no import errors.

- [ ] **Step 4: Verify shellcheck passes**

Run: `shellcheck -S info scripts/launchd/wrapper-bot.sh`

Expected: Clean.

- [ ] **Step 5: Final commit if any lint/format fixes**

```bash
git add -u
git commit -m "style: lint and format fixes for resilience components

Assisted-by: Claude (Anthropic)"
```

---

## Spec Compliance Checklist

| Spec Requirement | Task | Status |
|-----------------|------|--------|
| async_retry decorator with backoff [2, 5, 15] | Task 2 | |
| VPN health probe at localhost:8888 | Task 2 | |
| Retry on TimeoutError, ConnectionError, OSError | Task 2, 3 | |
| on_exhausted callback | Task 2, 3 | |
| Apply to place_order | Task 3 | |
| Apply to get_order_book | Task 3 | |
| Apply to get_order_status | Task 3 | |
| Apply to cancel_order | Task 3 | |
| Apply to cancel_open_orders_for_token | Task 3 (spec addition) | |
| Apply to _load_real_positions | Deferred (see Spec Deviations) | |
| Apply to get_balance_allowance | Deferred (see Spec Deviations) | |
| poll_until_terminal NOT retried | Task 3 (not touched) | |
| send_alert via msmtp | Task 1 | |
| send_alert_rate_limited (30 min default) | Task 1 | |
| Header injection prevention | Task 1 | |
| No-op when msmtp missing | Task 1 | |
| Wrapper crash counter (/tmp state file) | Task 4 | |
| 3 crashes in 10 min threshold | Task 4 | |
| Alert email on crash threshold | Task 4 | |
| exit 0 to stop KeepAlive | Task 4 | |
| Long session clears crash history | Task 4 | |
| No new dependencies (aiohttp already available) | All | |
| Tests for retry (6 tests) | Task 2 | |
| Tests for notify (9 tests) | Task 1 | |

## Spec Deviations

| Spec says | Plan does | Reason |
|-----------|-----------|--------|
| Retry `_load_real_positions` | Deferred | Sync method called from sync `get_sellable_token_id` — making it async cascades changes. Observed crashes are CLOB order/book timeouts, not position loads. Can add later. |
| Retry `get_balance_allowance` | Deferred | Not a `PolymarketClient` method — called as `exchange._clob_client.get_balance_allowance()` in `bot.py:880`. Spec also says "No changes to bot.py." Adding a wrapper method would change the spec scope. Can add later. |
| 6 methods retried | 5 methods retried | See above two rows. |
| (not in spec) | Retry `cancel_open_orders_for_token` | Added because it makes the same CLOB API calls as the other retried methods and benefits from the same resilience. |

## Notes

- `_submit_clob_order` doesn't need its own retry because `place_order` (which calls it) is already retried at the outer level.
- `requests.exceptions.RequestException` is explicitly included in `_DEFAULT_RETRYABLE` via a guarded import at module load time. This ensures SDK exceptions are retried even if the `RequestException` → `OSError` inheritance chain changes in future `requests` versions.
- `aiohttp` is required for the VPN health probe. It's already a transitive dependency of the project (used by the bot's async HTTP calls). If it's not installed, the probe will fail and return False (safe — retries continue without VPN checking).
