# Phase 1 Cost Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce overnight API costs by ~100x and add runtime enforcement + profit withdrawal alerts.

**Architecture:** Three components: (1) config changes + intensity preset bug fix in `defaults.yaml` and `settings.py`, (2) profit withdrawal alert task in `bot.py` with new `ProfitAlertConfig` in `settings.py`, (3) cost enforcement watchdog task in `bot.py` with cycle-frequency tracking and kill-switch firing on violations.

**Tech Stack:** Python 3.11+, pydantic v2, asyncio, pytest-asyncio, structlog, YAML config.

**Spec:** `docs/specs/2026-05-25-phase1-cost-controls-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `config/defaults.yaml` | Modify | Change model/multipliers, remove explicit NLP overrides, add `profit_alerts` section |
| `config/settings.py` | Modify | Add `ProfitAlertConfig`, update `"low"` intensity preset, add `profit_alerts` to `Settings` |
| `auramaur/bot.py` | Modify | Add `_cycle_timestamps`, `_fire_cost_kill_switch()`, `_task_cost_enforcement`, `_task_profit_withdrawal_alert`, register tasks, timestamp recording |
| `tests/test_settings.py` | Modify | Update existing intensity preset tests, add new `ProfitAlertConfig` tests |
| `tests/test_cost_enforcement.py` | Create | Tests for cost enforcement watchdog |
| `tests/test_profit_alert.py` | Create | Tests for profit withdrawal alert task |

---

### Task 1: Config Changes — defaults.yaml + Intensity Preset Fix

**Files:**

- Modify: `config/defaults.yaml:47-66`
- Modify: `config/settings.py:136-155`
- Modify: `tests/test_settings.py:73-100`

This task fixes the intensity preset bug and updates config values. The bug: `defaults.yaml` explicitly sets NLP fields (`skip_second_opinion: false`, `max_markets_per_cycle: 5`, `evidence_per_source: 5`, `daily_claude_call_budget: 80`) which fight with the `"low"` preset. The `NLPConfig.model_post_init()` at `settings.py:182-192` only applies preset values when the current value matches the `"medium"` default — but since YAML overrides load first, the preset never fires for those fields.

**Fix:** Remove explicit NLP overrides from YAML (let the preset system handle them), and update the `"low"` preset dict in `settings.py` to use tighter values.

- [ ] **Step 1: Update the existing intensity preset tests to expect the new "low" values**

These tests at `tests/test_settings.py:73-100` currently expect the old "low" preset values (`max_markets_per_cycle=10`, `daily_claude_call_budget=50`). Update them to expect the new values (`max_markets_per_cycle=3`, `daily_claude_call_budget=30`).

Edit `tests/test_settings.py` — replace the `test_intensity_low` function:

```python
def test_intensity_low():
    from config.settings import NLPConfig

    cfg = NLPConfig(api_intensity="low")
    assert cfg.skip_second_opinion is True
    assert cfg.max_markets_per_cycle == 3
    assert cfg.evidence_per_source == 3
    assert cfg.daily_claude_call_budget == 30
```

- [ ] **Step 2: Run the updated test to verify it fails**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_settings.py::test_intensity_low -v`

Expected: FAIL — the current `"low"` preset has `max_markets_per_cycle=10` and `daily_claude_call_budget=50`.

- [ ] **Step 3: Update the `"low"` intensity preset in settings.py**

Edit `config/settings.py` — replace the `"low"` entry in `_INTENSITY_PRESETS` at line 137-141:

```python
_INTENSITY_PRESETS: dict[str, dict] = {
    "low": {
        "skip_second_opinion": True,
        "max_markets_per_cycle": 3,
        "evidence_per_source": 3,
        "daily_claude_call_budget": 30,
    },
```

Only the `"low"` dict changes. `"medium"` and `"full_blast"` stay the same.

- [ ] **Step 4: Update defaults.yaml — remove explicit NLP overrides, change model + multipliers**

Edit `config/defaults.yaml`. The `nlp:` section (lines 51-66) should become:

```yaml
nlp:
  cache_ttl_breaking_seconds: 900
  cache_ttl_slow_seconds: 7200
  model: "sonnet"
  tool_use_model: "claude-opus-4-7"
  max_tokens: 4096
  # API intensity: "low", "medium", or "full_blast"
  # low:        3 markets, 3 evidence, skip second opinion, 30 calls/day
  # medium:     10 markets, 3 evidence, keep second opinion, 100 calls/day (default)
  # full_blast: 50 markets, 10 evidence, keep second opinion, unlimited calls
  # Individual settings below override the preset when set explicitly.
  api_intensity: "low"
```

Note: the four explicit lines (`skip_second_opinion`, `max_markets_per_cycle`, `evidence_per_source`, `daily_claude_call_budget`) are REMOVED. This lets the preset system in `model_post_init()` apply the `"low"` values.

Also edit the `intervals:` section (lines 47-48):

```yaml
  off_peak_multiplier: 8.0     # 8x slower at night (3min -> 24min analysis)
  quiet_multiplier: 24.0       # 24x slower deep night (3min -> 72min)
```

- [ ] **Step 5: Run the intensity preset tests to verify they pass**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_settings.py::test_intensity_low tests/test_settings.py::test_intensity_medium_is_default tests/test_settings.py::test_intensity_full_blast tests/test_settings.py::test_intensity_explicit_override_wins -v`

Expected: All 4 PASS.

- [ ] **Step 6: Add a test that verifies defaults.yaml + preset integration**

This test loads Settings (which reads defaults.yaml), then verifies the `"low"` preset values are correctly applied through the full config pipeline. Add to `tests/test_settings.py`:

```python
def test_yaml_low_intensity_applies_preset():
    """defaults.yaml sets api_intensity='low'. Verify the preset values
    actually take effect through the full Settings() pipeline, not just
    NLPConfig in isolation. This catches the bug where explicit YAML
    overrides fought with the preset system."""
    s = Settings()
    assert s.nlp.api_intensity == "low"
    assert s.nlp.skip_second_opinion is True
    assert s.nlp.max_markets_per_cycle == 3
    assert s.nlp.daily_claude_call_budget == 30
    assert s.nlp.model == "sonnet"
```

- [ ] **Step 7: Run the new integration test**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_settings.py::test_yaml_low_intensity_applies_preset -v`

Expected: PASS.

- [ ] **Step 8: Run the full test suite**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest -x -q`

Expected: All tests pass. If `test_default_risk_params` or `test_yaml_defaults_safe` break, fix them — they may hard-code old default values.

- [ ] **Step 9: Commit**

```bash
git add config/defaults.yaml config/settings.py tests/test_settings.py
git commit -m "feat(cost): reduce API spend — sonnet model, tighter low preset, wider multipliers

Change nlp.model from opus to sonnet (~10x cheaper per call).
Update low intensity preset: max_markets 10->3, daily_budget 50->30.
Remove explicit NLP overrides from defaults.yaml that fought with the
preset system (skip_second_opinion, max_markets, evidence, budget).
Widen overnight multipliers: off_peak 4->8, quiet 8->24.

Cost estimate: overnight spend drops from ~\$37.50/hr to ~\$0.36/hr.

Assisted-by: Claude (Anthropic)"
```

---

### Task 2: ProfitAlertConfig Model in settings.py + defaults.yaml

**Files:**

- Modify: `config/settings.py:289-393`
- Modify: `config/defaults.yaml` (append)
- Modify: `tests/test_settings.py`

- [ ] **Step 1: Write tests for ProfitAlertConfig**

Add to `tests/test_settings.py`:

```python
def test_profit_alert_config_defaults():
    """ProfitAlertConfig model defaults match the spec."""
    from config.settings import ProfitAlertConfig

    cfg = ProfitAlertConfig()
    assert cfg.enabled is False
    assert cfg.check_interval_seconds == 3600
    assert cfg.profit_threshold == 600.0
    assert cfg.withdrawal_amount == 300.0
    assert cfg.alert_cooldown_hours == 24


def test_profit_alert_config_from_yaml():
    """Settings loads profit_alerts from defaults.yaml."""
    s = Settings()
    assert hasattr(s, "profit_alerts")
    assert s.profit_alerts.enabled is True
    assert s.profit_alerts.profit_threshold == 600.0
    assert s.profit_alerts.withdrawal_amount == 300.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_settings.py::test_profit_alert_config_defaults tests/test_settings.py::test_profit_alert_config_from_yaml -v`

Expected: FAIL — `ProfitAlertConfig` does not exist yet.

- [ ] **Step 3: Add ProfitAlertConfig to settings.py**

Add the new model class in `config/settings.py`, after `LoggingConfig` (around line 293) and before the `Settings` class:

```python
class ProfitAlertConfig(BaseModel):
    """Config for profit withdrawal alert task.

    When unrealized P&L crosses ``profit_threshold``, the bot sends an
    email alert recommending withdrawal of ``withdrawal_amount``. This is
    alert-only — no automatic sell or transfer logic.
    """

    enabled: bool = False
    check_interval_seconds: int = 3600
    profit_threshold: float = 600.0
    withdrawal_amount: float = 300.0
    alert_cooldown_hours: int = 24
```

Then add the field to the `Settings` class, after the `logging` field (around line 393):

```python
    profit_alerts: ProfitAlertConfig = Field(
        default_factory=lambda: ProfitAlertConfig(**_DEFAULTS.get("profit_alerts", {}))
    )
```

- [ ] **Step 4: Add profit_alerts section to defaults.yaml**

Append to the end of `config/defaults.yaml`:

```yaml

profit_alerts:
  enabled: true
  check_interval_seconds: 3600     # check P&L once per hour
  profit_threshold: 600.0          # alert when unrealized P&L >= $600
  withdrawal_amount: 300.0         # recommend withdrawing $300
  alert_cooldown_hours: 24         # don't repeat the alert more than once per day
```

- [ ] **Step 5: Run the ProfitAlertConfig tests**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_settings.py::test_profit_alert_config_defaults tests/test_settings.py::test_profit_alert_config_from_yaml -v`

Expected: Both PASS.

- [ ] **Step 6: Run full test suite**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest -x -q`

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add config/settings.py config/defaults.yaml tests/test_settings.py
git commit -m "feat(cost): add ProfitAlertConfig model and defaults.yaml section

New ProfitAlertConfig pydantic model: enabled, check_interval_seconds,
profit_threshold ($600), withdrawal_amount ($300), alert_cooldown_hours.
Defaults in YAML enable the alert with a 24-hour cooldown.

Assisted-by: Claude (Anthropic)"
```

---

### Task 3: Profit Withdrawal Alert Task in bot.py

**Files:**

- Modify: `auramaur/bot.py`
- Create: `tests/test_profit_alert.py`

The task `_task_profit_withdrawal_alert` runs every `check_interval_seconds` (default 3600s). It calls the syncer to get current positions, computes unrealized P&L via `PnLTracker.get_unrealized_pnl()`, and sends a rate-limited email alert when P&L crosses the threshold.

- [ ] **Step 1: Write test file tests/test_profit_alert.py**

```python
"""Tests for the profit withdrawal alert task in bot.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import ProfitAlertConfig, Settings


def _make_bot(
    *,
    enabled: bool = True,
    threshold: float = 600.0,
    withdrawal: float = 300.0,
    cooldown_hours: int = 24,
):
    """Build a minimal AuramaurBot with mocked components for profit alert testing."""
    from auramaur.bot import AuramaurBot

    settings = Settings()
    settings.profit_alerts = ProfitAlertConfig(
        enabled=enabled,
        check_interval_seconds=1,
        profit_threshold=threshold,
        withdrawal_amount=withdrawal,
        alert_cooldown_hours=cooldown_hours,
    )

    bot = AuramaurBot(settings=settings)
    bot._running = True

    mock_syncer = AsyncMock()
    mock_pnl = AsyncMock()
    bot._components = {
        "syncer": mock_syncer,
        "pnl_tracker": mock_pnl,
    }

    return bot, mock_syncer, mock_pnl


def _make_position(market_id: str = "mkt_1", current_price: float = 0.7, unrealized_pnl: float = 0.0):
    """Create a mock LivePosition."""
    pos = MagicMock()
    pos.market_id = market_id
    pos.current_price = current_price
    pos.unrealized_pnl = unrealized_pnl
    return pos


@pytest.mark.asyncio
async def test_below_threshold_no_alert():
    """P&L below threshold — no alert sent."""
    bot, syncer, pnl = _make_bot(threshold=600.0)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 400.0

    with patch("auramaur.bot.send_alert_rate_limited") as mock_alert:
        # Run one iteration then stop
        async def _run_one():
            bot._running = True
            task = asyncio.create_task(bot._task_profit_withdrawal_alert())
            await asyncio.sleep(0.05)
            bot._running = False
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_one()
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_at_threshold_alert_fires():
    """P&L at threshold — alert fires."""
    bot, syncer, pnl = _make_bot(threshold=600.0)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 600.0

    with patch("auramaur.bot.send_alert_rate_limited", return_value=True) as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_called_once()
        call_kwargs = mock_alert.call_args
        assert "profit_withdrawal" in str(call_kwargs)


@pytest.mark.asyncio
async def test_above_threshold_alert_fires():
    """P&L above threshold — alert fires."""
    bot, syncer, pnl = _make_bot(threshold=600.0)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 800.0

    with patch("auramaur.bot.send_alert_rate_limited", return_value=True) as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_syncer_failure_no_crash():
    """Syncer raises exception — task logs error and continues."""
    bot, syncer, pnl = _make_bot()
    syncer.sync.side_effect = RuntimeError("CLOB timeout")

    with patch("auramaur.bot.send_alert_rate_limited") as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_no_alert():
    """profit_alerts.enabled=False — no alert even above threshold."""
    bot, syncer, pnl = _make_bot(enabled=False)
    syncer.sync.return_value = [_make_position()]
    pnl.get_unrealized_pnl.return_value = 1000.0

    with patch("auramaur.bot.send_alert_rate_limited") as mock_alert:
        task = asyncio.create_task(bot._task_profit_withdrawal_alert())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_no_syncer_task_exits():
    """No syncer in components — task should exit gracefully."""
    bot, _, _ = _make_bot()
    del bot._components["syncer"]

    task = asyncio.create_task(bot._task_profit_withdrawal_alert())
    await asyncio.sleep(0.05)
    bot._running = False
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # No crash = success
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_profit_alert.py -v`

Expected: FAIL — `_task_profit_withdrawal_alert` does not exist on `AuramaurBot`.

- [ ] **Step 3: Implement `_task_profit_withdrawal_alert` in bot.py**

Add the import at the top of `auramaur/bot.py` (the `send_alert_rate_limited` import already exists at line 24):

No new imports needed — `send_alert_rate_limited` is already imported at line 24.

Add the method to `AuramaurBot`, after `_task_kill_switch_monitor` (after line 1183):

```python
    async def _task_profit_withdrawal_alert(self) -> None:
        """Hourly check: alert when unrealized P&L crosses withdrawal threshold.

        Calls the syncer to get current positions, computes unrealized P&L
        via PnLTracker, and sends a rate-limited email when P&L >=
        profit_threshold. Alert-only — no automatic sell or transfer.
        """
        from auramaur.broker.pnl import PnLTracker

        cfg = self.settings.profit_alerts
        syncer = self._components.get("syncer")
        if syncer is None:
            log.info("profit_alert.no_syncer")
            return

        pnl_tracker: PnLTracker = self._components["pnl_tracker"]

        while self._running:
            if not cfg.enabled:
                await asyncio.sleep(cfg.check_interval_seconds)
                continue

            try:
                positions = await syncer.sync()
                unrealized = await pnl_tracker.get_unrealized_pnl(positions)

                if unrealized >= cfg.profit_threshold:
                    log.info(
                        "profit_alert.triggered",
                        unrealized_pnl=round(unrealized, 2),
                        threshold=cfg.profit_threshold,
                        withdrawal=cfg.withdrawal_amount,
                    )
                    send_alert_rate_limited(
                        subject=f"[auramaur] profit alert: ${unrealized:.2f} unrealized — consider withdrawing ${cfg.withdrawal_amount:.0f}",
                        body=(
                            f"Unrealized P&L has reached ${unrealized:.2f} (threshold: ${cfg.profit_threshold:.2f}).\n"
                            f"Recommended action: withdraw ${cfg.withdrawal_amount:.2f} from Polymarket.\n\n"
                            f"Current positions: {len(positions)}\n"
                            f"Unrealized P&L: ${unrealized:.2f}\n\n"
                            f"This is an alert only — no automatic withdrawal will occur.\n"
                            f"To change thresholds: edit profit_alerts in config/defaults.yaml\n"
                        ),
                        key="profit_withdrawal",
                        min_interval=cfg.alert_cooldown_hours * 3600,
                    )
                else:
                    log.debug(
                        "profit_alert.below_threshold",
                        unrealized_pnl=round(unrealized, 2),
                        threshold=cfg.profit_threshold,
                    )
            except Exception as e:
                log.error("profit_alert.error", error=str(e))

            await asyncio.sleep(cfg.check_interval_seconds)
```

- [ ] **Step 4: Run the profit alert tests**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_profit_alert.py -v`

Expected: All 6 PASS.

- [ ] **Step 5: Run full test suite**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest -x -q`

Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add auramaur/bot.py tests/test_profit_alert.py
git commit -m "feat(cost): add profit withdrawal alert task

New _task_profit_withdrawal_alert in bot.py: hourly check of unrealized
P&L via syncer, sends rate-limited email when P&L crosses configurable
threshold ($600). Alert-only — no sell logic.

Uses ProfitAlertConfig from settings.py (added in prior commit).

Assisted-by: Claude (Anthropic)"
```

---

### Task 4: Cost Enforcement Watchdog

**Files:**

- Modify: `auramaur/bot.py:49-81` (init), `auramaur/bot.py:630-670` (trading cycle)
- Create: `tests/test_cost_enforcement.py`

This is the core enforcement layer. Three checks: model identity, daily call budget, cycle frequency. Any violation fires the kill switch via a new `_fire_cost_kill_switch()` method.

- [ ] **Step 1: Write test file tests/test_cost_enforcement.py**

```python
"""Tests for the cost enforcement watchdog task in bot.py.

The watchdog runs hourly and validates three invariants:
1. Model identity — analyzer._model matches settings.nlp.model
2. Daily call budget — analyzer._daily_calls <= settings.nlp.daily_claude_call_budget
3. Cycle frequency — cycles in last hour <= expected max (with 50% headroom)

Any violation writes KILL_SWITCH and sends an unrestricted email alert.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import Settings


def _make_bot(*, model: str = "sonnet", budget: int = 30):
    """Build a minimal AuramaurBot with mocked analyzer for enforcement testing."""
    from auramaur.bot import AuramaurBot

    settings = Settings()
    # Ensure the settings match what we expect the watchdog to compare against
    settings.nlp.model = model
    settings.nlp.daily_claude_call_budget = budget

    bot = AuramaurBot(settings=settings)
    bot._running = True

    mock_analyzer = MagicMock()
    mock_analyzer._model = model
    mock_analyzer._daily_calls = 0

    bot._components = {"analyzer": mock_analyzer}

    return bot, mock_analyzer


@pytest.mark.asyncio
async def test_all_checks_pass_no_kill_switch(tmp_path):
    """All checks pass — KILL_SWITCH not written."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 10

    # 2 cycles in last hour — well within limits
    now = time.monotonic()
    bot._cycle_timestamps = deque([now - 1800, now - 600], maxlen=200)

    with (
        patch("auramaur.bot.send_alert") as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        task = asyncio.create_task(bot._task_cost_enforcement())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_write.assert_not_called()
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_model_mismatch_fires_kill_switch(tmp_path):
    """Wrong model loaded — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._model = "opus"  # mismatch!

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        task = asyncio.create_task(bot._task_cost_enforcement())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "model mismatch" in written

        mock_alert.assert_called_once()
        assert "KILL SWITCH" in mock_alert.call_args[1].get("subject", mock_alert.call_args[0][0] if mock_alert.call_args[0] else "")


@pytest.mark.asyncio
async def test_budget_exceeded_fires_kill_switch():
    """Daily budget exceeded — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 35  # over budget

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        task = asyncio.create_task(bot._task_cost_enforcement())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "daily budget exceeded" in written


@pytest.mark.asyncio
async def test_cycle_rate_exceeded_fires_kill_switch():
    """Too many cycles in last hour — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 5

    # Simulate 20 cycles in the last hour during quiet time
    # quiet_multiplier=24, base=180 → expected_interval=4320 → max ~1.25/hr
    # 20 cycles >> 1.25 * 1.5 = 1.875
    now = time.monotonic()
    bot._cycle_timestamps = deque(
        [now - (i * 180) for i in range(20)],
        maxlen=200,
    )

    # Force quiet mode for predictable math
    with (
        patch.object(bot, "_get_schedule_mode", return_value="quiet"),
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        task = asyncio.create_task(bot._task_cost_enforcement())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "cycle rate exceeded" in written


@pytest.mark.asyncio
async def test_budget_zero_unlimited_skips_check():
    """Budget of 0 means unlimited — budget check skipped even with many calls."""
    bot, analyzer = _make_bot(model="sonnet", budget=0)
    analyzer._daily_calls = 500

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert") as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        task = asyncio.create_task(bot._task_cost_enforcement())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_write.assert_not_called()
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_email_alert_sent_on_violation():
    """Any violation sends an unrestricted email alert (not rate-limited)."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._model = "opus"  # trigger model mismatch

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text"),
    ):
        task = asyncio.create_task(bot._task_cost_enforcement())
        await asyncio.sleep(0.05)
        bot._running = False
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs if mock_alert.call_args.kwargs else {}
        args = mock_alert.call_args.args if mock_alert.call_args.args else ()
        subject = kwargs.get("subject", args[0] if args else "")
        assert "KILL SWITCH" in subject
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_cost_enforcement.py -v`

Expected: FAIL — `_task_cost_enforcement`, `_fire_cost_kill_switch`, and `_cycle_timestamps` do not exist.

- [ ] **Step 3: Add `_cycle_timestamps` deque to AuramaurBot.**init****

Edit `auramaur/bot.py` — add to `__init__`, after the VPN watchdog state block (after line 80):

```python
        # Cost enforcement — deque of time.monotonic() timestamps recording
        # when each trading cycle completed. The _task_cost_enforcement
        # watchdog counts entries in the last 3600s to detect runaway cycles.
        self._cycle_timestamps: deque[float] = deque(maxlen=200)
```

Also add the `deque` import. Add `from collections import deque` near the top imports (after line 6, `from pathlib import Path`).

- [ ] **Step 4: Add `_fire_cost_kill_switch()` method to AuramaurBot**

Add after `_check_kill_switch` (after line 607):

```python
    async def _fire_cost_kill_switch(self, reason: str) -> None:
        """Write KILL_SWITCH file and halt bot due to cost enforcement violation.

        Unlike _check_kill_switch (which reads the file), this method writes
        it. The alert uses send_alert (unrestricted) rather than
        send_alert_rate_limited — cost violations are critical and must
        always notify.
        """
        from auramaur.infra.notify import send_alert

        Path("KILL_SWITCH").write_text(f"Cost enforcement: {reason}\n")
        log.error("cost_enforcement.violation", reason=reason)
        send_alert(
            subject="[auramaur] KILL SWITCH: cost enforcement violation",
            body=(
                f"The cost enforcement watchdog detected a violation and halted the bot.\n\n"
                f"Reason: {reason}\n\n"
                f"The KILL_SWITCH file has been written. The bot will not restart until\n"
                f"you investigate and run: auramaur unkill\n"
            ),
        )
        self._running = False
```

Note: `send_alert` is imported locally to avoid a circular import. The module-level import at line 24 is `send_alert_rate_limited` — we need the unrestricted `send_alert` here.

- [ ] **Step 5: Add `_task_cost_enforcement()` method**

Add after `_fire_cost_kill_switch`:

```python
    async def _task_cost_enforcement(self) -> None:
        """Hourly audit: verify runtime behavior matches cost config.

        Three checks, any of which fires the kill switch on violation:
        1. Model identity — analyzer._model must match settings.nlp.model.
        2. Daily call budget — analyzer._daily_calls must not exceed budget.
        3. Cycle frequency — cycles in the past hour must not exceed the
           expected maximum (with 50% headroom for scheduling jitter).

        The first check runs after 3600s of runtime to avoid false positives
        during startup. Subsequent checks run every 3600s.
        """
        from auramaur.nlp.analyzer import ClaudeAnalyzer

        # Wait for one full hour before first check — gives the bot time
        # to settle and avoids false positives on startup
        first_run = True

        while self._running:
            if first_run:
                await asyncio.sleep(3600)
                first_run = False
            else:
                await asyncio.sleep(3600)

            if not self._running:
                return

            try:
                analyzer = self._components.get("analyzer")
                if analyzer is None:
                    log.debug("cost_enforcement.no_analyzer")
                    continue

                # Check 1: model identity
                expected_model = self.settings.nlp.model
                actual_model = analyzer._model
                if actual_model != expected_model:
                    await self._fire_cost_kill_switch(
                        f"model mismatch: expected {expected_model}, got {actual_model}"
                    )
                    return

                # Check 2: daily call budget
                budget = self.settings.nlp.daily_claude_call_budget
                actual_calls = analyzer._daily_calls
                if budget > 0 and actual_calls > budget:
                    await self._fire_cost_kill_switch(
                        f"daily budget exceeded: {actual_calls}/{budget} calls"
                    )
                    return

                # Check 3: cycle frequency
                now = time.monotonic()
                cutoff = now - 3600
                cycles_last_hour = sum(
                    1 for ts in self._cycle_timestamps if ts > cutoff
                )

                base = self.settings.intervals.analysis_seconds
                mode = self._get_schedule_mode()
                if mode == "quiet":
                    multiplier = self.settings.intervals.quiet_multiplier
                elif mode == "off_peak":
                    multiplier = self.settings.intervals.off_peak_multiplier
                else:
                    multiplier = 1.0

                expected_interval = base * multiplier
                max_cycles_per_hour = (3600 / expected_interval) * 1.5

                if cycles_last_hour > max_cycles_per_hour:
                    await self._fire_cost_kill_switch(
                        f"cycle rate exceeded: {cycles_last_hour} cycles/hour, max {max_cycles_per_hour:.0f}"
                    )
                    return

                log.info(
                    "cost_enforcement.ok",
                    model=actual_model,
                    daily_calls=actual_calls,
                    budget=budget,
                    cycles_last_hour=cycles_last_hour,
                    max_cycles=round(max_cycles_per_hour),
                )

            except Exception as e:
                log.error("cost_enforcement.error", error=str(e))
```

- [ ] **Step 6: Record cycle timestamps in _task_trading_cycle**

Edit `_task_trading_cycle` at `auramaur/bot.py:630-670`. After the `engine.run_cycle()` call succeeds (inside the try block, around line 655), append the timestamp:

```python
                await engine.run_cycle(cash_available=cash)
                self._cycle_timestamps.append(time.monotonic())
```

The full try block becomes:

```python
            try:
                cash = getattr(self, "_last_known_cash", 0.0)
                await engine.run_cycle(cash_available=cash)
                self._cycle_timestamps.append(time.monotonic())
            except Exception as e:
                show_error(f"Trading cycle failed ({name}): {e}")
```

- [ ] **Step 7: Update tests to use sleep(0) for fast iteration**

The tests mock the sleep to 0 so they run instantly. However, the implementation sleeps 3600s. To make tests fast, the tests use `asyncio.sleep(0.05)` after creating the task, then cancel. The enforcement task uses `asyncio.sleep(3600)` which means the task won't have run its checks in 0.05s.

We need to patch `asyncio.sleep` in the tests to return immediately. Update `tests/test_cost_enforcement.py` — add a fixture and update all tests to use it:

Actually, looking at the tests more carefully, they need to patch the initial 3600s sleep. The simplest approach: make the enforcement task check interval configurable, or patch `asyncio.sleep`. Let's use a simpler pattern — extract the check logic into a testable method `_run_cost_enforcement_check` and test that directly:

Replace the test file with:

```python
"""Tests for the cost enforcement watchdog task in bot.py.

The watchdog runs hourly and validates three invariants:
1. Model identity — analyzer._model matches settings.nlp.model
2. Daily call budget — analyzer._daily_calls <= settings.nlp.daily_claude_call_budget
3. Cycle frequency — cycles in last hour <= expected max (with 50% headroom)

Any violation writes KILL_SWITCH and sends an unrestricted email alert.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from config.settings import Settings


def _make_bot(*, model: str = "sonnet", budget: int = 30):
    """Build a minimal AuramaurBot with mocked analyzer for enforcement testing."""
    from auramaur.bot import AuramaurBot

    settings = Settings()
    settings.nlp.model = model
    settings.nlp.daily_claude_call_budget = budget

    bot = AuramaurBot(settings=settings)
    bot._running = True

    mock_analyzer = MagicMock()
    mock_analyzer._model = model
    mock_analyzer._daily_calls = 0

    bot._components = {"analyzer": mock_analyzer}

    return bot, mock_analyzer


@pytest.mark.asyncio
async def test_all_checks_pass_no_kill_switch():
    """All checks pass — KILL_SWITCH not written."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 10

    now = time.monotonic()
    bot._cycle_timestamps = deque([now - 1800, now - 600], maxlen=200)

    with (
        patch("auramaur.bot.send_alert") as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_not_called()
        mock_alert.assert_not_called()
        assert bot._running is True


@pytest.mark.asyncio
async def test_model_mismatch_fires_kill_switch():
    """Wrong model loaded — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._model = "opus"

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "model mismatch" in written
        assert bot._running is False


@pytest.mark.asyncio
async def test_budget_exceeded_fires_kill_switch():
    """Daily budget exceeded — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 35

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "daily budget exceeded" in written
        assert bot._running is False


@pytest.mark.asyncio
async def test_cycle_rate_exceeded_fires_kill_switch():
    """Too many cycles in last hour — kill switch fires."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._daily_calls = 5

    now = time.monotonic()
    bot._cycle_timestamps = deque(
        [now - (i * 180) for i in range(20)],
        maxlen=200,
    )

    with (
        patch.object(bot, "_get_schedule_mode", return_value="quiet"),
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert "cycle rate exceeded" in written
        assert bot._running is False


@pytest.mark.asyncio
async def test_budget_zero_unlimited_skips_check():
    """Budget of 0 means unlimited — budget check skipped even with many calls."""
    bot, analyzer = _make_bot(model="sonnet", budget=0)
    analyzer._daily_calls = 500

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert") as mock_alert,
        patch.object(Path, "write_text") as mock_write,
    ):
        await bot._run_cost_enforcement_check()

        mock_write.assert_not_called()
        mock_alert.assert_not_called()
        assert bot._running is True


@pytest.mark.asyncio
async def test_email_alert_sent_on_violation():
    """Any violation sends an unrestricted email alert (not rate-limited)."""
    bot, analyzer = _make_bot(model="sonnet", budget=30)
    analyzer._model = "opus"

    bot._cycle_timestamps = deque(maxlen=200)

    with (
        patch("auramaur.bot.send_alert", return_value=True) as mock_alert,
        patch.object(Path, "write_text"),
    ):
        await bot._run_cost_enforcement_check()

        mock_alert.assert_called_once()
        subject = mock_alert.call_args.kwargs.get(
            "subject", mock_alert.call_args.args[0] if mock_alert.call_args.args else ""
        )
        assert "KILL SWITCH" in subject
```

- [ ] **Step 8: Refactor implementation — extract `_run_cost_enforcement_check()`**

The tests call `_run_cost_enforcement_check()` directly (no sleep). Update `_task_cost_enforcement` to delegate to this method. Rewrite both methods in `bot.py`:

```python
    async def _run_cost_enforcement_check(self) -> None:
        """Run all three cost enforcement checks once.

        Called by _task_cost_enforcement on its hourly schedule. Separated
        for testability — tests call this directly without waiting 3600s.
        """
        from auramaur.infra.notify import send_alert

        analyzer = self._components.get("analyzer")
        if analyzer is None:
            log.debug("cost_enforcement.no_analyzer")
            return

        # Check 1: model identity
        expected_model = self.settings.nlp.model
        actual_model = analyzer._model
        if actual_model != expected_model:
            await self._fire_cost_kill_switch(
                f"model mismatch: expected {expected_model}, got {actual_model}"
            )
            return

        # Check 2: daily call budget
        budget = self.settings.nlp.daily_claude_call_budget
        actual_calls = analyzer._daily_calls
        if budget > 0 and actual_calls > budget:
            await self._fire_cost_kill_switch(
                f"daily budget exceeded: {actual_calls}/{budget} calls"
            )
            return

        # Check 3: cycle frequency
        now = time.monotonic()
        cutoff = now - 3600
        cycles_last_hour = sum(1 for ts in self._cycle_timestamps if ts > cutoff)

        base = self.settings.intervals.analysis_seconds
        mode = self._get_schedule_mode()
        if mode == "quiet":
            multiplier = self.settings.intervals.quiet_multiplier
        elif mode == "off_peak":
            multiplier = self.settings.intervals.off_peak_multiplier
        else:
            multiplier = 1.0

        expected_interval = base * multiplier
        max_cycles_per_hour = (3600 / expected_interval) * 1.5

        if cycles_last_hour > max_cycles_per_hour:
            await self._fire_cost_kill_switch(
                f"cycle rate exceeded: {cycles_last_hour} cycles/hour, max {max_cycles_per_hour:.0f}"
            )
            return

        log.info(
            "cost_enforcement.ok",
            model=actual_model,
            daily_calls=actual_calls,
            budget=budget,
            cycles_last_hour=cycles_last_hour,
            max_cycles=round(max_cycles_per_hour),
        )

    async def _task_cost_enforcement(self) -> None:
        """Hourly audit: verify runtime behavior matches cost config.

        Sleeps 3600s between checks. First check after one full hour of
        runtime to avoid false positives during startup.
        """
        while self._running:
            await asyncio.sleep(3600)
            if not self._running:
                return
            try:
                await self._run_cost_enforcement_check()
            except Exception as e:
                log.error("cost_enforcement.error", error=str(e))
```

- [ ] **Step 9: Run the cost enforcement tests**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest tests/test_cost_enforcement.py -v`

Expected: All 6 PASS.

- [ ] **Step 10: Run full test suite**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest -x -q`

Expected: All tests pass.

- [ ] **Step 11: Commit**

```bash
git add auramaur/bot.py tests/test_cost_enforcement.py
git commit -m "feat(cost): add cost enforcement watchdog with kill switch

New _task_cost_enforcement runs hourly, checking three invariants:
1. Model identity (analyzer._model vs config)
2. Daily call budget adherence
3. Cycle frequency (via _cycle_timestamps deque)

Any violation fires kill switch via _fire_cost_kill_switch() — writes
KILL_SWITCH file, sends unrestricted email, halts bot. Extracted
_run_cost_enforcement_check() for testability.

Also records cycle timestamps in _task_trading_cycle for frequency
tracking.

Assisted-by: Claude (Anthropic)"
```

---

### Task 5: Task Registration + Integration Verification

**Files:**

- Modify: `auramaur/bot.py:2387-2460`

Register both new tasks in `bot.run()` and run the full test suite to verify everything integrates cleanly.

- [ ] **Step 1: Add `send_alert` import to bot.py module level**

The `_fire_cost_kill_switch` method uses a local import of `send_alert`. For consistency and to avoid the import on every call, add it to the module-level imports at `auramaur/bot.py:24`:

Change line 24 from:

```python
from auramaur.infra.notify import send_alert_rate_limited
```

to:

```python
from auramaur.infra.notify import send_alert, send_alert_rate_limited
```

Then update `_fire_cost_kill_switch` to remove the local import and use the module-level one.

- [ ] **Step 2: Register cost enforcement task in bot.run()**

Add after the always-on tasks block (after line 2391, after the `recalibrate` task):

```python
        # Cost enforcement watchdog — hourly audit of model, budget, and
        # cycle frequency. Fires kill switch on any violation. Always-on,
        # not gated on exchange filter or syncer.
        tasks.append(
            asyncio.create_task(
                self._task_cost_enforcement(), name="cost_enforcement"
            )
        )
```

- [ ] **Step 3: Register profit withdrawal alert task in bot.run()**

Add after the VPN watchdog block (after line 2411), before the resolution checker:

```python
        # Profit withdrawal alert — hourly P&L check, sends email when
        # unrealized profit crosses configurable threshold. Requires a
        # syncer to fetch current positions with live prices.
        if self.settings.profit_alerts.enabled and self._components.get("syncer"):
            tasks.append(
                asyncio.create_task(
                    self._task_profit_withdrawal_alert(), name="profit_alert"
                )
            )
```

- [ ] **Step 4: Run full test suite**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run pytest -x -q`

Expected: All tests pass.

- [ ] **Step 5: Run linter**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run ruff check . && uv run ruff format --check .`

Expected: Clean. Fix any issues.

- [ ] **Step 6: Verify defaults.yaml loads correctly end-to-end**

Run: `cd /Volumes/extra-vieille/Workspaces/Auramaur && uv run python -c "from config.settings import Settings; s = Settings(); print(f'model={s.nlp.model}'); print(f'intensity={s.nlp.api_intensity}'); print(f'skip_2nd={s.nlp.skip_second_opinion}'); print(f'max_mkt={s.nlp.max_markets_per_cycle}'); print(f'budget={s.nlp.daily_claude_call_budget}'); print(f'off_peak={s.intervals.off_peak_multiplier}'); print(f'quiet={s.intervals.quiet_multiplier}'); print(f'profit_enabled={s.profit_alerts.enabled}'); print(f'threshold={s.profit_alerts.profit_threshold}')"`

Expected output:

```
model=sonnet
intensity=low
skip_2nd=True
max_mkt=3
budget=30
off_peak=8.0
quiet=24.0
profit_enabled=True
threshold=600.0
```

- [ ] **Step 7: Commit**

```bash
git add auramaur/bot.py
git commit -m "feat(cost): register cost enforcement + profit alert tasks in bot.run()

Both tasks registered in run():
- cost_enforcement: always-on, hourly audit with kill-switch
- profit_alert: gated on enabled + syncer presence, hourly P&L check

Also hoisted send_alert to module-level import for consistency.

Assisted-by: Claude (Anthropic)"
```

---

## Post-Implementation Checklist

After all 5 tasks are complete:

1. Run `uv run pytest -x -q` — all tests pass
2. Run `uv run ruff check . && uv run ruff format --check .` — clean
3. Verify the end-to-end config check from Task 5 Step 6
4. Create PR via `gh pr create`
5. Monitor CI via `bash ~/.claude/scripts/post-push-status.sh <PR#>`
