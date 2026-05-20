"""Tests for exchange-namespaced DB slot acquisition and CLI --db option."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from auramaur.bot import AuramaurBot
from auramaur.cli import run as cli_run


def _make_bot(**kwargs) -> AuramaurBot:
    """Instantiate AuramaurBot with a mock Settings to avoid env reads."""
    return AuramaurBot(settings=MagicMock(), **kwargs)


def _mock_flock_open():
    """Return a context-manager patch pair that makes flock always succeed."""
    mock_fh = MagicMock()
    open_patch = patch("builtins.open", return_value=mock_fh)
    flock_patch = patch("fcntl.flock")
    return open_patch, flock_patch


def test_db_name_with_exchange_polymarket():
    """When exchange_filter='polymarket', DB name is auramaur-polymarket.db."""
    bot = _make_bot(exchange_filter="polymarket")
    open_patch, flock_patch = _mock_flock_open()
    with open_patch, flock_patch:
        result = bot._acquire_db_path()
    assert result == "auramaur-polymarket.db"


def test_db_name_with_exchange_kalshi():
    """When exchange_filter='kalshi', DB name is auramaur-kalshi.db."""
    bot = _make_bot(exchange_filter="kalshi")
    open_patch, flock_patch = _mock_flock_open()
    with open_patch, flock_patch:
        result = bot._acquire_db_path()
    assert result == "auramaur-kalshi.db"


def test_db_name_without_exchange():
    """Without exchange_filter, first auto-detect slot returns auramaur.db."""
    bot = _make_bot(exchange_filter=None)
    open_patch, flock_patch = _mock_flock_open()
    with open_patch, flock_patch:
        result = bot._acquire_db_path()
    assert result == "auramaur.db"


def test_explicit_db_path_overrides_exchange():
    """Explicit db_path takes priority over exchange_filter."""
    bot = _make_bot(db_path="custom.db", exchange_filter="kalshi")
    open_patch, flock_patch = _mock_flock_open()
    with open_patch, flock_patch:
        result = bot._acquire_db_path()
    assert result == "custom.db"


def test_exchange_lock_already_held_raises():
    """When the exchange-namespaced DB is locked, RuntimeError is raised."""
    bot = _make_bot(exchange_filter="polymarket")
    mock_fh = MagicMock()
    with (
        patch("builtins.open", return_value=mock_fh),
        patch("fcntl.flock", side_effect=OSError("locked")),
    ):
        try:
            bot._acquire_db_path()
            raise AssertionError("Expected RuntimeError")
        except RuntimeError as e:
            assert "already locked" in str(e)
            assert "polymarket" in str(e)


def test_cli_db_option_passed_to_bot():
    """--db CLI option passes db_path to AuramaurBot constructor."""
    runner = CliRunner()
    captured = {}

    def fake_bot_init(self, settings=None, db_path=None, exchange_filter=None):
        captured["db_path"] = db_path
        captured["exchange_filter"] = exchange_filter
        self.settings = MagicMock()
        self._running = False
        self._components = {}
        self._db_path = db_path
        self._exchange_filter = exchange_filter
        self._lock_file = None
        self._rebalance_cooldowns = {}
        self._exit_failures = set()
        self._arb_attempts = {}

    async def fake_run(self):
        return

    with (
        patch.object(AuramaurBot, "__init__", fake_bot_init),
        patch.object(AuramaurBot, "run", fake_run),
        patch("auramaur.cli.Settings"),
    ):
        result = runner.invoke(cli_run, ["--db", "custom.db", "--exchange", "kalshi"])

    assert result.exit_code == 0, result.output
    assert captured["db_path"].endswith("custom.db")
    assert captured["exchange_filter"] == "kalshi"
