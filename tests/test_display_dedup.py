"""Tests for show_portfolio() status-line deduplication."""

from unittest.mock import MagicMock, patch

import auramaur.monitoring.display as display_mod


def _reset():
    display_mod._last_status_key = ""
    display_mod._status_repeat_count = 0


def test_60_identical_calls_print_twice():
    _reset()
    with patch.object(display_mod, "console", new_callable=MagicMock) as mock_console:
        for _ in range(60):
            display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "starved")
        assert mock_console.print.call_count == 2


def test_status_change_prints_immediately():
    _reset()
    with patch.object(display_mod, "console", new_callable=MagicMock) as mock_console:
        display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "starved")
        display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "starved")
        display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "starved")
        display_mod.show_portfolio(200.0, 0.0, 5, 0.0, "starved")
        assert mock_console.print.call_count == 2


def test_120_calls_summary_includes_skip_count():
    _reset()
    with patch.object(display_mod, "console", new_callable=MagicMock) as mock_console:
        for _ in range(120):
            display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "starved")

        summary_calls = [
            call
            for call in mock_console.print.call_args_list
            if "repeated" in str(call)
        ]
        assert len(summary_calls) >= 1
        assert "59" in str(summary_calls[0])


def test_mode_change_prints_immediately():
    _reset()
    with patch.object(display_mod, "console", new_callable=MagicMock) as mock_console:
        display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "peak")
        display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "starved")
        assert mock_console.print.call_count == 2


def test_drawdown_change_prints_immediately():
    _reset()
    with patch.object(display_mod, "console", new_callable=MagicMock) as mock_console:
        display_mod.show_portfolio(100.0, 0.0, 5, 0.0, "normal")
        display_mod.show_portfolio(100.0, 0.0, 5, 2.5, "normal")
        assert mock_console.print.call_count == 2
