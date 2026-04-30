# Upstream Contribution Plan

**Fork:** smartwatermelon/Auramaur  
**Upstream:** DarriEy/Auramaur  
**Branch this work lives on:** `claude/docs-deployment-plan-init`

---

## Triage: what goes upstream vs stays in fork

| Commit | Description | Upstream? | Notes |
|--------|-------------|-----------|-------|
| `0bf35e1` | fix(allocator): float-exhaustion → $0.00 orders | **Yes** | Strip `paper_initial_balance` hunk — that's our preference, not a fix |
| `25262ac` | fix(kalshi): paper mode sync reads from PaperTrader | **Yes, if upstream has Kalshi** | Check first; clean cherry-pick |
| `a9bc0cd` | fix(kalshi): paper mode cash starvation (900s cycle) | **Yes, if upstream has Kalshi** | `bot.py` diff is large; may need manual rebase against upstream |
| `89e5878` | feat(risk): time_to_resolution_max_days ceiling | **Maybe** | Opinionated feature; frame as opt-in (default 0 = disabled) |
| `49204a5` | build: declare cryptography runtime dep | **Yes** | Self-contained one-liner; easy win |
| `61ea414` | docs: fix .env.example gaps | **Yes** | Verify gaps still exist in upstream before submitting |
| `77cedcc` | feat(settings): Kalshi private key as env-var | **Maybe** | Only if upstream has Kalshi |
| `9787b12` | docs: Kalshi paper-mode starvation finding | **No** | Internal ops note |
| `3d2d3b1` | chore: gitignore .claude/secrets.op | **No** | Our tooling |
| `f74c7d3` | docs: Polymarket dev-key request drafts | **No** | Our account situation |
| `0c07d76` | feat(observability): Datasette + Streamlit dashboard | **No** | Our tooling choice |

---

## Upstream PRs to open (ordered by risk/size)

### PR U-1: fix(allocator) — float-exhaustion producing $0.00 DROPPED orders

**Priority: High. Exchange-agnostic, small diff, has test.**

Cherry-pick `0bf35e1`, then strip the `paper_initial_balance` hunk:

```bash
git fetch upstream main
git checkout -b upstream/fix-allocator-float-exhaustion upstream/main
git cherry-pick 0bf35e1
# Interactive: drop the config/defaults.yaml hunk (paper_initial_balance 111→500)
git checkout HEAD -- config/defaults.yaml
# Verify allocator change only:
git diff HEAD~1 -- auramaur/broker/allocator.py
# Run tests
uv run pytest tests/ -k allocator -v
git push upstream-remote upstream/fix-allocator-float-exhaustion
```

**PR title:** `fix(allocator): guard against float-exhaustion producing $0.00 DROPPED orders`

**Body:** Explain that `remaining_capital` after repeated subtraction reaches `~1e-10` (not `0.0`), so `size = min(desired, 1e-10, cat_headroom)` is technically `> 0` but `round(size, 2) == 0.00`, producing spurious DROPPED messages and an ORDER FAILED at the end of each cycle. Fix: use `round(size, 2) <= 0` as the guard.

---

### PR U-2: build(deps): declare cryptography as runtime dependency

**Priority: High. One-liner, silent install breakage.**

```bash
git checkout -b upstream/fix-cryptography-dep upstream/main
git cherry-pick 49204a5
git push upstream-remote upstream/fix-cryptography-dep
```

Check that `kalshi-python` is a dep in upstream's `pyproject.toml` first — if upstream doesn't use the Kalshi SDK, skip this.

---

### PR U-3: fix(kalshi): paper mode sync reads from PaperTrader, not live API

**Priority: High, conditional on upstream having Kalshi.**

First, verify: does upstream's `auramaur/broker/sync.py` have `KalshiPositionSyncer`?

```bash
git checkout -b upstream/fix-kalshi-paper-sync upstream/main
git cherry-pick 25262ac
# Check for conflicts — if KalshiPositionSyncer doesn't exist upstream, skip
uv run pytest tests/test_kalshi.py -v
git push upstream-remote upstream/fix-kalshi-paper-sync
```

**PR title:** `fix(kalshi): paper mode sync reads from PaperTrader, not live API`

---

### PR U-4: fix(kalshi): paper mode cash starvation / 900s cycle

**Priority: Medium-high. Conditional on Kalshi. Large bot.py diff — may need manual work.**

The `bot.py` changes in `a9bc0cd` are significant (552 changed lines). Before cherry-picking:

```bash
git checkout -b upstream/fix-kalshi-paper-starvation upstream/main
git cherry-pick a9bc0cd
# Likely conflicts in bot.py — resolve manually, keeping only:
#   1. KalshiPositionSyncer.get_cash_balance paper branch
#   2. _task_portfolio_monitor gate on syncers (not syncer)
```

If conflicts are too hairy, write the fix fresh as a patch on upstream's bot.py rather than cherry-picking. The two logical changes are small even if the commit is large.

---

### PR U-5 (optional): feat(risk): time_to_resolution_max_days ceiling

**Priority: Low. Opinionated — upstream may disagree.**

Change the default to `0` (disabled) rather than `90` so it's opt-in:

```bash
git checkout -b upstream/feat-resolution-ceiling upstream/main
git cherry-pick 89e5878
# Edit defaults.yaml: time_to_resolution_max_days: 0  (not 90)
# Edit settings.py: time_to_resolution_max_days: int = 0
# Commit the default change
```

**PR body:** Frame as "add an optional upper bound; disabled by default." Describe the observed failure mode (buying 2030–2035 contracts with no realistic exit path) as motivation.

---

## Pre-submission checklist (each upstream PR)

- [ ] Verify the bug/feature still exists in upstream's current `main` (not already fixed)
- [ ] Confirm tests pass against upstream's codebase (not just our fork's)
- [ ] Strip any fork-specific changes (paper_initial_balance, .claude/ refs, instance naming)
- [ ] Commit message follows upstream's style (check their recent commits)
- [ ] PR description includes: root cause, observed symptoms, fix, and how to reproduce

---

## Timing

Wait until after the 2-week paper eval is complete before opening upstream PRs. By then:

- We'll have validation that the fixes hold under extended paper trading
- The allocator and sync fixes will have been exercised across many cycles
- We can include empirical results ("before: 900s cycles / after: 180s cycles") in the PR bodies
