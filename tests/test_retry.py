"""Tests for auramaur.infra.retry — async retry with VPN health probe."""

from unittest.mock import patch

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
