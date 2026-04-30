# 2026-04-28 — Initial test-baseline findings

**Context:** Running `uv run pytest -q` for the first time on Andrew's clean checkout of the fork (which is fully sync'd with `DarriEy/Auramaur` main as of this date). Three issues surfaced. All three are pre-existing in upstream — none were introduced by Andrew's changes (there are no Andrew changes yet).

## Finding 1 — `pytest-asyncio` is silently absent on a default `uv sync`

**Symptom:** First pytest run produced ~30 warnings of the form `PytestUnknownMarkWarning: Unknown pytest.mark.asyncio - is this a typo?`, plus `PytestConfigWarning: Unknown config option: asyncio_mode`.

**Root cause:** `pyproject.toml` declares `pytest-asyncio>=0.23` under `[project.optional-dependencies] dev`, not under the runtime `dependencies`. `uv sync` (without `--extra dev`) installs only runtime deps. Result: every `@pytest.mark.asyncio`-decorated test is treated as an unknown mark — the test function is *called* by pytest but the returned coroutine is never awaited, so the test body never executes. The test passes silently with a `RuntimeWarning: coroutine '...' was never awaited` deep in the warnings noise.

**Why this matters:** with `asyncio_mode = "auto"` configured but no `pytest-asyncio` installed, **the bot's async-heavy test surface is effectively un-tested on a fresh install.** `risk/checks.py`, `exchange/paper.py`, almost every test in `tests/` includes async tests. A new contributor running `uv sync && uv run pytest` sees a green-looking suite that isn't actually exercising async code.

**Fix:** either move `pytest-asyncio` (and the rest of `dev`) into runtime `dependencies`, or document `uv sync --extra dev` prominently in `README.md` and `CLAUDE.md`. The first is the conventional Python answer (test deps are dev-time, not runtime); the second is the better fit here because the bot itself doesn't need pytest. **Recommended:** update `README.md` Quickstart to use `uv sync --extra dev` and add a line in `CLAUDE.md` clarifying that the dev extra is required for the test suite to exercise async code.

**Upstream PR-ability:** strong candidate. One-line README/CLAUDE.md change. Universal benefit to anyone running the bot.

## Finding 2 — `web3` is imported but not declared as a dependency

**Symptom:** `tests/test_onchain_redeemer.py` fails to *collect* on a clean install with `ModuleNotFoundError: No module named 'web3'`.

**Root cause:** Commit `60fe9fa Finish on-chain redemption for Gnosis Safe v1.3.0 proxy` added `auramaur/broker/onchain.py` (the on-chain redeemer) and its tests, both of which import `web3`. `web3` was never added to `pyproject.toml` `dependencies` or any optional-dependencies group.

**Why this matters:** on a clean install, the redemption path is non-functional and its test suite can't even be collected. The bot itself happens to limp along because `onchain.py` is imported lazily from inside CLI subcommands, so the import error only surfaces when a user actually runs `auramaur redeem` or `auramaur redeem-check`. On the contributing developer's machine this likely worked because `web3` was already installed as a transitive dependency of something unrelated.

**Fix:** add `web3>=7.0` to `dependencies` in `pyproject.toml`. The redeemer is core trading infrastructure, not optional.

**Upstream PR-ability:** strong candidate. One-line `pyproject.toml` change.

## Finding 3 — `test_resolution_tracker.py` fails with `RuntimeError: There is no current event loop` on Python 3.14

**Symptom:** With `pytest-asyncio` correctly installed, 9 tests in `tests/test_resolution_tracker.py` fail uniformly with:

```
RuntimeError: There is no current event loop in thread 'MainThread'.
  File ".../asyncio/events.py:715: in get_event_loop_policy"
```

The other 279 async tests across the suite (with `pytest-asyncio` installed) pass. So this is not a global async-config issue; it's specific to how `test_resolution_tracker.py` is constructed.

**Root cause (suspected — not yet confirmed):** Python 3.14 removed implicit event-loop creation from `asyncio.get_event_loop()` when called outside a coroutine. Test code that calls `asyncio.get_event_loop()` from a synchronous context (e.g., a fixture or a test helper) used to get an auto-created loop on Python ≤ 3.10, a deprecation warning on 3.11–3.13, and now hard-fails on 3.14. The bot itself uses `asyncio.run()` exclusively (verified in `cli.py`, `bot.py`), so this is a test-code issue, not a bot bug.

**Why this matters:** the resolution tracker is the closed-loop component that feeds real outcomes back into the calibration system (`auramaur/strategy/resolution_tracker.py`). Without working tests, regressions to that path will go undetected — and the calibration loop is exactly what readiness criterion #4 (Brier ≤ 0.24) depends on for the Phase 1 gate flip. **This must be fixed before Phase 1 readiness can be meaningfully evaluated.**

**Fix:** open `test_resolution_tracker.py` and identify the synchronous `get_event_loop()` call. Replace with the appropriate `pytest-asyncio` fixture (`event_loop_policy` or in-test `asyncio.new_event_loop()`). Verify on the repo's pinned Python version (currently 3.14 per `requires-python = ">=3.11"`).

**Upstream PR-ability:** medium confidence. The fix is local to one test file but Python-version-dependent. Worth verifying the upstream maintainer is also on Python 3.14 before submitting; if not, the upstream maintainer may not be hitting this and would benefit more from a `python_requires` clarification or a tox matrix.

## Summary of action items (these become tasks, not part of this finding)

- [ ] Phase 1 prerequisite: fix Finding 3 so resolution-tracker tests are real before we measure calibration.
- [ ] Phase 1 prerequisite: fix Finding 2 (declare `web3`) so a clean install can actually run the redemption path on Polymarket positions.
- [ ] Document Finding 1 by updating Quickstart (README) to `uv sync --extra dev`.
- [ ] All three are PR-back candidates per `docs/plans/2026-04-28-deployment-plan.md` §6, after we've fixed them locally and run them live for ≥ 3 days.

## Verification commands used

```bash
# Initial run — collection error
uv run pytest -q  # → 1 error (test_onchain_redeemer collection)

# After installing extras + web3
uv sync --extra dev --extra kalshi
uv pip install web3
uv run pytest -q  # → 279 passed, 9 failed in test_resolution_tracker.py
```
