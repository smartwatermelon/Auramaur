# Phase 1 Cost Controls Design

## Problem

On 2026-05-25, the Polymarket bot burned ~$110 in Claude API costs overnight
(10 Anthropic auto-recharge invoices in ~8 hours). The bot ran ~6-7 NLP
cycles per hour using the Opus model, each cycle invoking `claude -p` as a
subprocess. Meanwhile, the Kalshi bot consumed API tokens for days without
placing a single trade.

Three hard economic constraints now govern the project:

1. **Monthly profit must exceed inference costs** (Claude API).
2. **Monthly profit must exceed total infrastructure costs** (API + VPN +
   compute + exchange fees).
3. **No churn trading.** Never enter buy-sell-buy-sell loops that generate
   fees/slippage with no net profit.

Constraint 3 is already satisfied — the bot has no sell logic. This spec
addresses constraints 1 and 2 by reducing API spend and adding a profit
withdrawal alert so gains are harvested before they erode.

## Goals

1. **Reduce per-cycle API cost** by ~10x via model downgrade and call
   reduction.
2. **Reduce cycle frequency** during off-peak/quiet hours to near-zero
   cost overnight.
3. **Fix the intensity preset bug** so `api_intensity: "low"` actually
   applies its intended defaults.
4. **Enforce cost constraints at runtime** — an hourly watchdog that
   compares actual behavior (model, call count, cycle frequency) against
   config and fires the kill switch on violations.
5. **Alert the user to withdraw profits** when unrealized P&L crosses a
   configurable threshold — alert only, no sell logic.

## Non-Goals

- Build sell/exit logic (Phase 2).
- Implement automatic withdrawal or on-chain transfers.
- Add real-time cost tracking per Claude API call.
- Change the analysis pipeline structure or model selection logic.

---

## Component 1: Config Cost Reduction

**File:** `config/defaults.yaml`

### Changes

| Setting | Current | New | Effect |
|---------|---------|-----|--------|
| `nlp.model` | `"opus"` | `"sonnet"` | ~10x cheaper per call |
| `nlp.skip_second_opinion` | `false` | `true` | Halves calls per market |
| `nlp.max_markets_per_cycle` | `5` | `3` | 40% fewer markets analyzed |
| `nlp.daily_claude_call_budget` | `80` | `30` | Hard ceiling on daily spend |
| `intervals.off_peak_multiplier` | `4.0` | `8.0` | 24-min cycles → 48-min off-peak |
| `intervals.quiet_multiplier` | `8.0` | `24.0` | 24-min cycles → 72-min overnight |

### Intensity preset bug fix

**Problem:** `defaults.yaml` sets `api_intensity: "low"` but also explicitly
sets `skip_second_opinion: false` and `daily_claude_call_budget: 80`. The
`NLPConfig.model_post_init()` method in `config/settings.py:182-192` only
applies preset values when the current value matches the `"medium"` default.
Since `defaults.yaml` explicitly sets these fields, they override the preset
— `"low"` intensity never actually takes effect for those settings.

**Fix:** Remove the explicit `skip_second_opinion`, `max_markets_per_cycle`,
`evidence_per_source`, and `daily_claude_call_budget` lines from
`defaults.yaml`. This allows `api_intensity: "low"` to apply its preset
values from `_INTENSITY_PRESETS` in `settings.py`. Then, the new values in
the table above are set by modifying the `"low"` preset in `settings.py` to
use these tighter limits, rather than fighting with `defaults.yaml` overrides.

### Updated `"low"` preset in `settings.py`

```python
_INTENSITY_PRESETS: dict[str, dict] = {
    "low": {
        "skip_second_opinion": True,
        "max_markets_per_cycle": 3,       # was 10
        "evidence_per_source": 3,
        "daily_claude_call_budget": 30,   # was 50
    },
    ...
}
```

### Cost estimate

**Before (overnight with Opus):**

- 3-min base × 8x quiet = 24-min cycles → ~2.5 cycles/hour
- 5 markets/cycle × 2 calls each (analysis + second opinion) = ~10 calls/cycle
- ~25 calls/hour × $1.50/call (Opus) ≈ **$37.50/hour** overnight

**After (overnight with Sonnet + cost controls):**

- 3-min base × 24x quiet = 72-min cycles → ~0.8 cycles/hour
- 3 markets/cycle × 1 call each (no second opinion) = ~3 calls/cycle
- ~2.4 calls/hour × $0.15/call (Sonnet) ≈ **$0.36/hour** overnight

That is a ~100x reduction in overnight API cost. Daily budget cap of 30
calls provides an additional hard ceiling.

---

## Component 2: Profit Withdrawal Alert Task

**File:** `auramaur/bot.py`

### Config additions

**File:** `config/defaults.yaml`

New section:

```yaml
profit_alerts:
  enabled: true
  check_interval_seconds: 3600     # check P&L once per hour
  profit_threshold: 600.0          # alert when unrealized P&L >= $600
  withdrawal_amount: 300.0         # recommend withdrawing $300
  alert_cooldown_hours: 24         # don't repeat the alert more than once per day
```

**File:** `config/settings.py`

New model:

```python
class ProfitAlertConfig(BaseModel):
    enabled: bool = False
    check_interval_seconds: int = 3600
    profit_threshold: float = 600.0
    withdrawal_amount: float = 300.0
    alert_cooldown_hours: int = 24
```

Add `profit_alerts: ProfitAlertConfig` to the `Settings` class.

### Task: `_task_profit_withdrawal_alert`

A new async task that runs every `check_interval_seconds` (default 3600s).

#### Logic

1. Skip if `profit_alerts.enabled` is False.
2. Get current positions from the syncer or portfolio tracker.
3. Compute unrealized P&L using `PnLTracker.get_unrealized_pnl(positions)`.
4. If unrealized P&L >= `profit_threshold`:
   - Send an email alert via `send_alert_rate_limited` with
     key `"profit_withdrawal"` and `min_interval` derived from
     `alert_cooldown_hours * 3600`.
   - Log `"profit_alert.triggered"` with current P&L, threshold, and
     recommended withdrawal amount.
5. If unrealized P&L < `profit_threshold`:
   - Log `"profit_alert.below_threshold"` at debug level.

#### Alert format

```
Subject: [auramaur] profit alert: $X.XX unrealized — consider withdrawing $Y

Body:
  Unrealized P&L has reached $X.XX (threshold: $600.00).
  Recommended action: withdraw $300.00 from Polymarket.

  Current positions: N
  Total deployed: $D.DD
  Unrealized P&L: $X.XX

  This is an alert only — no automatic withdrawal will occur.
  To change thresholds: edit profit_alerts in config/defaults.yaml
```

#### Task registration

In `bot.run()`, register the task alongside other always-on tasks (after
the VPN watchdog block):

```python
if self.settings.profit_alerts.enabled:
    tasks.append(
        asyncio.create_task(
            self._task_profit_withdrawal_alert(), name="profit_alert"
        )
    )
```

### Position data source

The task needs access to current positions with `current_price` populated.
Two options exist:

1. **Use the syncer** (`self._components["syncer"]`): calls
   `syncer.sync()` which returns `list[LivePosition]`. This hits the
   CLOB API but only once per hour.
2. **Use the portfolio DB**: read the `portfolio` table. This avoids
   API calls but `current_price` is only as fresh as the last sync.

**Decision:** Use the syncer. The task runs once per hour, so one
additional CLOB API call per hour is negligible. This gives accurate
prices without depending on the portfolio sync frequency.

However, the syncer is Polymarket-specific. If the bot is Kalshi-only,
there's no syncer. Gate the task on having a syncer:

```python
if self.settings.profit_alerts.enabled and self._components.get("syncer"):
    tasks.append(...)
```

For multi-exchange support in the future, this can be extended to
aggregate across all exchange syncers — but for now Polymarket is the
only exchange with live capital.

### Edge cases

- **VPN down:** If the VPN is down, syncer calls will fail. The task
  should catch exceptions and skip (same pattern as other tasks). It
  does NOT check `self._vpn_down` because the hourly check is cheap
  and we want to alert even during partial outages.
- **No positions:** If syncer returns empty, unrealized P&L is $0.
  This is a normal state after full withdrawal or position expiry.
- **Cooldown:** The 24-hour cooldown prevents alert spam when P&L
  hovers around the threshold. The `send_alert_rate_limited` function
  handles this.

---

## Component 3: Cost Enforcement Watchdog

**File:** `auramaur/bot.py`

### Problem

Config values can silently fail to apply (as proven by the intensity
preset bug). The daily budget enforcement in `analyzer.py:103-107` is
in-memory and per-process — if the budget value loaded wrong, the guard
never fires. There is no second layer that asks "is this bot actually
behaving within its cost constraints?"

### Solution

A new async task `_task_cost_enforcement` that runs once per hour. It
reads the bot's actual runtime state and compares it against the
configured limits. If any check fails, it **fires the kill switch** and
sends an email alert — the same way `_check_kill_switch()` works, but
writing the `KILL_SWITCH` file rather than just reading it.

### Enforcement checks

The watchdog validates three things:

**1. Model identity**

```python
analyzer: ClaudeAnalyzer = self._components["analyzer"]
expected_model = self.settings.nlp.model
actual_model = analyzer._model

if actual_model != expected_model:
    # VIOLATION: wrong model loaded — likely costs 10x more per call
    fire_kill_switch(reason=f"model mismatch: expected {expected_model}, got {actual_model}")
```

**2. Daily call budget**

```python
budget = self.settings.nlp.daily_claude_call_budget
actual_calls = analyzer._daily_calls

if budget > 0 and actual_calls > budget:
    # VIOLATION: budget exceeded — the analyzer guard failed
    fire_kill_switch(reason=f"daily budget exceeded: {actual_calls}/{budget} calls")
```

Note: this catches the case where `budget` loaded as 0 (unlimited) due
to a config bug, but the analyzer made hundreds of calls. It also
catches the case where the analyzer's own guard somehow didn't fire.

**3. Cycle frequency**

Track wall-clock timestamps of recent trading cycle completions. If the
bot has run more cycles in the past hour than what the config should
allow, something is wrong.

```python
# Expected max cycles/hour = 3600 / (base_seconds * current_multiplier)
# Allow 50% headroom for scheduling jitter
base = self.settings.intervals.analysis_seconds
mode = self._get_schedule_mode()
if mode == "quiet":
    multiplier = self.settings.intervals.quiet_multiplier
elif mode == "off_peak":
    multiplier = self.settings.intervals.off_peak_multiplier
else:
    multiplier = 1.0

expected_interval = base * multiplier
max_cycles_per_hour = (3600 / expected_interval) * 1.5  # 50% headroom

if actual_cycles_last_hour > max_cycles_per_hour:
    fire_kill_switch(reason=f"cycle rate exceeded: {actual_cycles_last_hour} cycles/hour, max {max_cycles_per_hour:.0f}")
```

### Cycle tracking

To count cycles in the past hour, add a deque to `__init__`:

```python
from collections import deque
self._cycle_timestamps: deque[float] = deque(maxlen=200)
```

At the end of each `_task_trading_cycle` iteration (after the engine
runs), append `time.monotonic()`. The enforcement watchdog counts
entries within the last 3600 seconds.

### Kill switch firing

When a violation is detected:

1. Write the `KILL_SWITCH` file with the violation reason.
2. Send an email alert via `send_alert` (NOT rate-limited — this is
   critical).
3. Log `"cost_enforcement.violation"` at error level.
4. Set `self._running = False`.

```python
async def _fire_cost_kill_switch(self, reason: str) -> None:
    """Write KILL_SWITCH file and halt bot due to cost enforcement violation."""
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

### Task structure

```python
async def _task_cost_enforcement(self) -> None:
    """Hourly audit: verify runtime behavior matches cost config.

    Checks model identity, daily call budget adherence, and cycle
    frequency. Fires the kill switch on any violation — cost overruns
    are unrecoverable and the human must investigate.
    """
    while self._running:
        await asyncio.sleep(3600)  # first check after 1 hour of runtime
        try:
            # Check 1: model identity
            ...
            # Check 2: daily budget
            ...
            # Check 3: cycle frequency
            ...
            log.info(
                "cost_enforcement.ok",
                model=actual_model,
                daily_calls=actual_calls,
                budget=budget,
                cycles_last_hour=actual_cycles_last_hour,
            )
        except Exception as e:
            log.error("cost_enforcement.error", error=str(e))
```

### Task registration

Always-on task — register alongside kill switch monitor and cache
cleanup (not gated on exchange filter or syncer):

```python
tasks.append(
    asyncio.create_task(
        self._task_cost_enforcement(), name="cost_enforcement"
    )
)
```

### Edge cases

- **First hour:** The task sleeps 3600s before its first check, so a
  fresh bot gets one full hour before enforcement kicks in. This avoids
  false positives during startup when `_daily_calls` is 0.
- **Model name normalization:** `settings.nlp.model` might be `"sonnet"`
  while the analyzer stores it as-is. The check compares the exact
  string the analyzer received at init time, which is the same string
  passed to `--model`. No normalization needed.
- **Kill switch already active:** If `KILL_SWITCH` already exists, the
  kill switch monitor (1-second poll) will catch it before this task
  runs. The enforcement task doesn't need to check for it.
- **Budget of 0 (unlimited):** If `budget == 0`, the budget check is
  skipped (0 means unlimited). The cycle frequency check still applies.

---

## Testing

### Config tests

1. **Intensity preset applies correctly:** Create `NLPConfig` with
   `api_intensity="low"` and no explicit overrides. Assert
   `max_markets_per_cycle == 3`, `daily_claude_call_budget == 30`,
   `skip_second_opinion is True`.

2. **Explicit overrides still win:** Create `NLPConfig` with
   `api_intensity="low"` and explicit `daily_claude_call_budget=99`.
   Assert budget is 99 (not 30).

### Enforcement watchdog tests (`tests/test_cost_enforcement.py`)

1. **All checks pass — no kill switch:** Mock analyzer with correct
   model, 10 calls (under budget of 30), 2 cycles in the last hour.
   Run one watchdog iteration. Assert `KILL_SWITCH` NOT written.

2. **Model mismatch — kill switch fires:** Mock analyzer with
   `_model = "opus"` when config says `"sonnet"`. Run one iteration.
   Assert `KILL_SWITCH` written with "model mismatch" reason.

3. **Budget exceeded — kill switch fires:** Mock analyzer with
   `_daily_calls = 35` and budget of 30. Run one iteration. Assert
   `KILL_SWITCH` written with "daily budget exceeded" reason.

4. **Cycle rate exceeded — kill switch fires:** Populate
   `_cycle_timestamps` with 20 entries in the last hour when the
   quiet multiplier should allow ~0.8. Run one iteration. Assert
   `KILL_SWITCH` written with "cycle rate exceeded" reason.

5. **Budget of 0 (unlimited) — budget check skipped:** Mock analyzer
   with 500 calls and budget of 0. Run one iteration. Assert
   `KILL_SWITCH` NOT written (budget check skipped).

6. **Email alert sent on violation:** Mock `send_alert`, trigger any
   violation. Assert `send_alert` called with subject containing
   "KILL SWITCH".

### Profit alert tests (`tests/test_profit_alert.py`)

1. **Below threshold — no alert:** Mock syncer to return positions with
   total unrealized P&L of $400. Run one task iteration. Assert
   `send_alert_rate_limited` NOT called.

2. **At threshold — alert fires:** Mock syncer to return positions with
   total unrealized P&L of $600. Run one task iteration. Assert
   `send_alert_rate_limited` called with key `"profit_withdrawal"`.

3. **Above threshold — alert fires:** Same as above with $800 P&L.

4. **Cooldown suppresses repeat:** Mock syncer at $700, run two
   iterations with time less than cooldown. Assert alert sent only once.

5. **Syncer failure — no crash:** Mock syncer to raise exception. Run one
   iteration. Assert task logs error and continues.

6. **Disabled — no alert:** Set `profit_alerts.enabled = False`. Mock
   syncer at $1000. Assert no alert.

---

## File Summary

| File | Change |
|------|--------|
| `config/defaults.yaml` | Change model, multipliers; add `profit_alerts` section; remove explicit NLP overrides that fight with intensity preset |
| `config/settings.py` | Add `ProfitAlertConfig` model; update `"low"` intensity preset values; add `profit_alerts` field to `Settings` |
| `auramaur/bot.py` | Add `_cycle_timestamps` deque, `_fire_cost_kill_switch()`, `_task_cost_enforcement`, `_task_profit_withdrawal_alert`; register both in `run()`; append to `_cycle_timestamps` in `_task_trading_cycle` |
| `tests/test_cost_enforcement.py` | New test file for enforcement watchdog |
| `tests/test_profit_alert.py` | New test file for profit alert task |
| `tests/test_settings.py` or inline | Add/update test for intensity preset fix |

No new dependencies. No new launchd agents. No new infrastructure.
