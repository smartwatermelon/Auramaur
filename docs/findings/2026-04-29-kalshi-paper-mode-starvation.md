# 2026-04-29 — Kalshi-only paper mode always enters cash-starved cycle

**Context:** First live run of the bot with `--exchange kalshi`. Observed only one
cycle at startup ("0 signals, 0 trades (1s)"), then nothing for 15 minutes.

## Root cause: two bugs compound each other

### Bug 1 — KalshiPositionSyncer.get_cash_balance ignores paper mode

`KalshiPositionSyncer.get_cash_balance()` unconditionally called
`self._exchange.get_balance()` — the real Kalshi REST API. On a fresh
account this returns 0 cents.

`PositionSyncer` (Polymarket) already had the correct behaviour:

```python
async def get_cash_balance(self) -> float:
    if self._settings.is_live:
        return await self._get_live_balance()
    return self._paper.balance      # paper mode: read local state
```

`KalshiPositionSyncer` had no equivalent branch — it always queried the
API. Fix: accept `paper=None` in `__init__`, return `self._paper.balance`
when `not settings.is_live and self._paper is not None`.

### Bug 2 — portfolio monitor never starts for Kalshi-only runs

`_task_portfolio_monitor` — the task that sets `AuramaurBot._last_known_cash`
from syncer results — was gated on:

```python
if self._components.get("syncer"):   # Polymarket PositionSyncer
    tasks.append(_task_portfolio_monitor())
    tasks.append(_task_position_sync())
```

`syncer` (singular) is only set when Polymarket is active. For
`--exchange kalshi` it is `None`. The portfolio monitor task was **never
created**, so `_last_known_cash` stayed at 0 indefinitely.

`syncers` (plural, `list`) contains `KalshiPositionSyncer` for Kalshi
runs. Fix: start the portfolio monitor whenever `syncers` is non-empty;
keep `position_sync` on the original `syncer` gate since it is
Polymarket-specific (fill reconciler, redemption).

## Compound effect: 15-minute cycles instead of 3-minute cycles

`_is_cash_starved()` returns `True` when `_last_known_cash < 5.0`. With
`_last_known_cash` permanently 0, `_adaptive_interval(180)` applied a
5× cash-starved multiplier: **180 × 5 = 900 s per cycle**. The bot ran
one (starved) cycle at startup then slept 15 minutes before the next.

The paper trader held $111. Zero analysis happened.

## Fix applied

`auramaur/broker/sync.py` — `KalshiPositionSyncer`:

- Accept `paper=None` kwarg in `__init__`
- `get_cash_balance()` returns `self._paper.balance` when not live

`auramaur/bot.py` — task creation:

- Gate `_task_portfolio_monitor` on `syncers` (any syncer) not `syncer`
- Gate `_task_position_sync` on `syncer` (Polymarket-specific, unchanged)
- Pass `paper=paper` to `KalshiPositionSyncer` constructor

`tests/test_kalshi.py` — `TestKalshiPositionSyncerBalance` (3 new tests):

- paper mode returns paper balance
- live mode queries exchange
- backwards-compat: `paper=None` falls through to exchange query

## Upstream PR-ability

**Strong candidate.** Anyone running `--exchange kalshi` without a
Polymarket account would hit this. The fix is minimal and surgical.
The Polymarket-symmetry argument ("align to how PositionSyncer already
works") makes the PR motivation obvious. Test coverage included.
