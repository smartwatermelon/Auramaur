# 2026-04-28 — Duplicate exchange-fee tables disagree

**Context:** Tracing the edge calculation while scoping Phase 1 Kalshi config. Discovered two parallel sources of truth for per-exchange fees that disagree on Crypto.com.

## Finding — `signals.py` hardcoded fees vs. `defaults.yaml` configured fees

There are two exchange-fee tables in the repo:

**1. Hardcoded in `auramaur/strategy/signals.py:17-21`:**

```python
EXCHANGE_FEES: dict[str, float] = {
    "polymarket": 0.0,    # 0% for reward tier accounts
    "kalshi": 0.07,       # 7% fee on winnings
    "cryptodotcom": 0.01, # ~1% fee
}
```

**2. Configured in `config/defaults.yaml:116-119`:**

```yaml
arbitrage:
  exchange_fees:
    polymarket: 0.0
    kalshi: 0.07
    cryptodotcom: 0.075
```

**They agree on Polymarket (0.0) and Kalshi (0.07) but disagree on Crypto.com (0.01 vs 0.075 — a 7.5× difference).**

## Why it matters

Different code paths consume different tables:

- **Per-trade risk gate** (`auramaur/strategy/signals.py::detect_edge` → `auramaur/risk/checks.py::check_min_edge`): uses the **hardcoded** `EXCHANGE_FEES`. So `signal.edge` is computed as `claude_prob - market_prob - fee_rate_hardcoded`, where `fee_rate_hardcoded["cryptodotcom"] = 0.01`.
- **Arbitrage scanner** (`auramaur/strategy/arbitrage_scanner.py`): uses the **configured** `settings.arbitrage.exchange_fees` (initialized in `bot.py:324` from `s.arbitrage.exchange_fees`). So cross-exchange arb computations use 0.075 for Crypto.com.

Net effect: on Crypto.com markets, the edge gate would *approve* a trade based on a 1% fee assumption while the arb scanner would *reject* the same trade as unprofitable based on 7.5%. A user editing `defaults.yaml` to update Crypto.com's fee would only fix half the bot's behavior.

## Scope and Phase 1 impact

**Not Phase 1 critical.** Phase 1 targets Kalshi only, where both tables agree on 0.07. Phase 2 (Polymarket US) is also unaffected — both tables agree on 0.0. The bug only bites if/when Crypto.com is enabled (`cryptodotcom.enabled: true` in `defaults.yaml`).

**Latent for Phase 3+.** If we ever route through Crypto.com (the codebase has the wiring under `auramaur/exchange/cryptodotcom.py` and a Crypto.com config block), we'll need to fix this before any live trading there.

## Suggested fix

Replace the hardcoded constant in `signals.py` with a dependency injection of the fee table from settings. Two reasonable shapes:

**Option A — pass at signal-detection call site:**

```python
# signals.py
def detect_edge(market: Market, analysis: AnalysisResult, *, exchange_fees: dict[str, float]) -> Signal | None:
    fee_rate = exchange_fees.get(market.exchange, 0.0)
    ...
```

Update `auramaur/strategy/engine.py` to pass `self.settings.arbitrage.exchange_fees` (or a renamed `settings.exchange_fees` — see below).

**Option B — module-level setter at bot init:**

Less idiomatic, breaks pure-function-ness of `detect_edge`. Don't do this.

While doing the fix, consider renaming `arbitrage.exchange_fees` → top-level `exchange_fees` in `defaults.yaml` since the table is consumed by both the arb scanner and (post-fix) the edge gate; "arbitrage.exchange_fees" is misleading.

## Upstream PR-ability

**Strong candidate**, but only after soak. The fix changes Crypto.com edge-gate behavior in ways that depend on which value (0.01 hardcoded or 0.075 configured) is correct for the upstream maintainer's actual Crypto.com account. The defaults.yaml comment says "~0.075% maker/taker" which is suspect — typical Crypto.com fees are 0.075% (basis points, not percent), i.e., 0.00075 as a fraction. Either both values are wrong, or the comment is wrong, or the fraction-vs-percent units differ between the tables.

This needs verification with the upstream maintainer before PRing — it's not a clean "fix the typo" PR, it's a "the fee accounting has divergent assumptions about units" finding. Worth opening as an issue first to confirm intended units, then PR with the fix.

## Action items (tracked separately)

- [ ] (Phase 3 prereq) Unify the fee tables: pass settings.exchange_fees (or renamed) into `detect_edge`. Verify Crypto.com unit (fraction vs. percent vs. basis-point fraction) with upstream maintainer.
- [ ] No Phase 1/2 impact — defer until Crypto.com is on the roadmap.
