"""Tests for auramaur.infra.notify — msmtp email alerting."""

import os
import subprocess
import time
from unittest.mock import MagicMock, patch

from auramaur.infra.notify import _last_sent, send_alert, send_alert_rate_limited


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
        cmd = call_args[0][0]
        assert cmd[0] == "/opt/homebrew/bin/msmtp"
        assert "-a" in cmd and "gmail" in cmd
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

    @patch("shutil.which", return_value="/opt/homebrew/bin/msmtp")
    @patch("subprocess.run")
    def test_header_injection_via_to_addr_env(self, mock_run, mock_which):
        """Env-var addresses containing newlines must be rejected, not passed to msmtp."""
        with patch.dict(
            os.environ,
            {"AURAMAUR_ALERT_TO": "legit@example.com\nBcc: evil@evil.com"},
        ):
            result = send_alert("Subject", "Body")

        assert result is False
        mock_run.assert_not_called()

    @patch("shutil.which", return_value="/opt/homebrew/bin/msmtp")
    @patch("subprocess.run")
    def test_header_injection_via_from_addr_env(self, mock_run, mock_which):
        """Env-var from-addresses containing newlines must be rejected."""
        with patch.dict(
            os.environ,
            {"AURAMAUR_ALERT_FROM": "legit@example.com\nBcc: evil@evil.com"},
        ):
            result = send_alert("Subject", "Body")

        assert result is False
        mock_run.assert_not_called()

    @patch("shutil.which", return_value=None)
    def test_noop_when_msmtp_missing(self, mock_which):
        result = send_alert("Subject", "Body")
        assert result is False

    @patch("shutil.which", return_value="/opt/homebrew/bin/msmtp")
    @patch(
        "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="msmtp", timeout=30)
    )
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
        result = send_alert_rate_limited(
            "Subj", "Body", key="test_key", min_interval=1800
        )
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
        result = send_alert_rate_limited(
            "Subj", "Body", key="test_key", min_interval=1800
        )
        assert result is True

    @patch("auramaur.infra.notify.send_alert", return_value=False)
    def test_failed_send_does_not_update_timestamp(self, mock_send):
        """When send_alert fails, timestamp is not updated so next call retries."""
        result = send_alert_rate_limited(
            "Subj", "Body", key="test_key", min_interval=1800
        )
        assert result is False
        # Timestamp must NOT be persisted — next call should retry immediately
        assert "test_key" not in _last_sent
