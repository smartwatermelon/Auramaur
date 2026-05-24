"""Lightweight email alerting via msmtp — no external dependencies."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from email.utils import formatdate

import structlog

log = structlog.get_logger()

_last_sent: dict[str, float] = {}
_rate_limit_lock = threading.Lock()

_DEFAULT_TO = "andrew.rich@gmail.com"


def _sanitize_header_value(value: str) -> str | None:
    """Return sanitized single-line header value, or None if it contains injected newlines."""
    # Reject values that contain CR or LF — these could inject additional headers
    if "\r" in value or "\n" in value:
        return None
    return value.strip()


def send_alert(subject: str, body: str) -> bool:
    """Send an alert email via msmtp. Returns True on success."""
    msmtp = shutil.which("msmtp")
    if not msmtp:
        log.warning("notify.msmtp_not_found")
        return False

    to_addr = os.environ.get("AURAMAUR_ALERT_TO", _DEFAULT_TO)
    from_addr = os.environ.get("AURAMAUR_ALERT_FROM", _DEFAULT_TO)

    # Reject env-var addresses containing injection characters
    safe_to = _sanitize_header_value(to_addr)
    safe_from = _sanitize_header_value(from_addr)
    if safe_to is None:
        log.error("notify.invalid_to_address")
        return False
    if safe_from is None:
        log.error("notify.invalid_from_address")
        return False

    # Strip header-injection characters from subject: take only the first line
    safe_subject = subject.replace("\r\n", "\n").split("\n")[0].strip()

    account = os.environ.get("AURAMAUR_ALERT_MSMTP_ACCOUNT", "gmail")

    message = (
        f"From: {safe_from}\n"
        f"To: {safe_to}\n"
        f"Subject: {safe_subject}\n"
        f"Date: {formatdate(localtime=True)}\n"
        f"\n"
        f"{body}\n"
    )

    try:
        result = subprocess.run(
            [msmtp, "-a", account, safe_to],
            input=message,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error(
                "notify.msmtp_failed",
                returncode=result.returncode,
                stderr=result.stderr[:200],
            )
            return False
        log.info("notify.sent", to=safe_to, subject=safe_subject[:60])
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
    """Send alert if at least min_interval seconds since last alert with this key.

    Uses ``key`` to namespace rate-limit buckets — always pass a descriptive key
    (e.g. ``key="portfolio_loss"``).  The default bucket ``"default"`` is shared
    across all callers that omit it, which can suppress unrelated alerts.

    When send_alert returns False (msmtp unavailable or failed), the timestamp is
    NOT updated, so the next call will retry immediately.
    """
    with _rate_limit_lock:
        now = time.monotonic()
        last = _last_sent.get(key, 0.0)
        if now - last < min_interval:
            log.debug(
                "notify.rate_limited", key=key, seconds_since_last=round(now - last)
            )
            return False
        # Claim the slot before releasing the lock so concurrent callers are blocked
        _last_sent[key] = now

    sent = send_alert(subject, body)
    if not sent:
        # Relinquish the slot so the next call can retry immediately
        with _rate_limit_lock:
            if _last_sent.get(key) == now:
                del _last_sent[key]
    return sent
