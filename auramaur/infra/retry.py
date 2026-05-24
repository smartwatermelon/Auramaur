"""Async retry decorator with exponential backoff and VPN health probing."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any

import structlog

log = structlog.get_logger()

_UNSET = object()


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


_DEFAULT_RETRYABLE: tuple[type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
)
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
    fallback: Any = _UNSET,
) -> Callable:
    """Decorator for async methods. Retries on transient network errors.

    When *fallback* is provided and retries exhaust, the fallback value is
    returned instead of re-raising the last exception.  ``on_exhausted`` is
    still called (if set) before returning.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if backoff_seconds is None:
        backoff_seconds = [2, 5, 15]
    if not backoff_seconds:
        raise ValueError("backoff_seconds must contain at least one delay value")
    if retryable is None:
        retryable = _DEFAULT_RETRYABLE

    def _get_delay(attempt: int) -> float:
        return backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]

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
                        backoff=_get_delay(attempt),
                    )

                    vpn_ok = await _check_vpn_health()
                    if not vpn_ok:
                        log.error(
                            "retry.vpn_down",
                            method=fn.__qualname__,
                        )
                        break

                    await asyncio.sleep(_get_delay(attempt))

            if on_exhausted is not None and last_error is not None:
                try:
                    on_exhausted(last_error)
                except Exception as cb_err:
                    log.error("retry.callback_failed", error=type(cb_err).__name__)

            if fallback is not _UNSET:
                log.warning(
                    "retry.fallback",
                    method=fn.__qualname__,
                    error=type(last_error).__name__,
                )
                return fallback

            raise last_error  # type: ignore[misc]

        return wrapper

    return decorator
