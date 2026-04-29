# Auramaur Deployment Plan — Andrew's Fork

**Status:** Active — drafted 2026-04-28
**Owner:** Andrew Rich (`smartwatermelon/Auramaur`)
**Upstream:** `DarriEy/Auramaur` (fully sync'd as of 2026-04-28)

This document captures the decisions and rationale for taking Andrew's fork of Auramaur from "checked out, never run live" to "trading real money on multiple prediction markets." It is the shared record for the deployment effort. Findings observed during paper-trading and live operation will accumulate as separate dated documents under `docs/findings/`.

---

## 1. Context and operative deadline

Auramaur was built by Darri Eythorsson and described in his Bloomberg Opinion piece "I Built an AI Trading Platform in Six Days. That's Terrifying." (`bloomberg-20260428.pdf`, gitignored at repo root, archive.is/6L68L). The article's most actionable claim, for our purposes, is the author's own prediction that **prediction-market edge will close within ~12 months** as agentic-trading saturation grows. He continues to run his own bot, implying it works now.

That ~12-month window from 2026-04-28 is the operative deadline for prioritization. **Time-to-live-trading and time-to-validate beat infrastructural elegance.** Refactors and parent-PR-backs run in parallel with deployment, not as gates on it.

---

## 2. Exchange landscape — friction-to-edge ranking

| Exchange | API keys | VPN? | Code in repo | Cost | Edge surface |
|---|---|---|---|---|---|
| **Kalshi** | ✅ in hand | No | `auramaur/exchange/kalshi.py` (627 lines, both protocols) | **7% on profits** (per `config/defaults.yaml`) | Politics, macro, sports — broad but fee-eaten |
| **Polymarket US** | Need to request | No | Same client as Global (`exchange/client.py`) | 0% (reward tier) | Sports + handful of elections only — narrow, sharps-dominated |
| **Polymarket Global** | Need to request, must auth from VPN endpoint | Yes (PIA, persistent) | Same client | 0% (reward tier) | Full international news/politics/crypto — where the NLP edge actually lives |

The bot's data sources (NewsAPI, FRED, Reddit, Twitter, GDELT, Manifold, Metaculus, Bluesky, web search, etc. — see `bot.py:108-167`) are heavily weighted toward news/politics/macro signals. That stack is partially capitalized on Kalshi (after fees) and almost wasted on Polymarket-US's sports-and-some-elections subset. **Polymarket Global is the design target of this code.**

---

## 3. Phased deployment plan

### Phase 1 — Kalshi paper, then Kalshi live ($200 cap)

**Goal:** validate the calibration loop, risk gate, and bot health on the lowest-friction exchange before spending any infrastructure time on VPN containerization.

- **Use upstream defaults — no config tune.** Original draft of this plan called for bumping `risk.min_edge_pct` to 5.0; investigation (Kalshi-config trace, 2026-04-28) showed two reasons that was wrong:
  1. The 7% Kalshi fee is *already* subtracted from edge inside `auramaur/strategy/signals.py:182-183` before `signal.edge` reaches the risk gate. Upstream's `min_edge_pct: 3.5` is a 3.5pp *post-fee* threshold (raw edge ≥ 10.5pp), not a pre-fee threshold.
  2. At our $200 starting bankroll, `auramaur/risk/regime.py` overrides the configured values entirely (because equity < `GROWTH_EQUITY_MAX = $1000`). Phase 1 will run on the regime-derived growth-mode params:`kelly_fraction = 0.50` (half-Kelly), `max_stake = $20` (10% of equity), `min_edge_pct = 2.5`. The configured YAML values are irrelevant until equity grows past $1000.
- Verified Kalshi-relevant tests pass: `tests/test_kalshi.py`, `tests/test_kelly.py`, `tests/test_risk_checks.py`, `tests/test_triple_gate.py`, `tests/test_multi_exchange_risk.py`, `tests/test_cross_arb.py` — all 85/85 pass at this commit.
- Build `auramaur readiness` CLI subcommand that prints pass/fail on each criterion (§4) against the live SQLite DB.
- Run bot in paper mode for the rolling 7-day window (longer if Kalshi resolution cadence is slow on the markets the bot picks).
- Once readiness passes for **7 consecutive days**, gate flip is authorized via the manual ceremony in §5. Real-money cap: **$200**, with regime-effective per-trade max stake of $20 and 2.5pp net-of-fees min-edge gate.

**Why the regime override is the right thing for Phase 1:** the author's regime-switched Kelly is genuinely well-designed for our exact scenario. At $200 equity the dominant failure mode is "fail to compound" (variance is bounded by tiny stakes anyway), so half-Kelly + 2.5pp threshold gets us more trades for calibration data faster than the preservation-mode values would. Phase 1's purpose is validating the calibration loop, not maximizing per-trade EV — more trades is more valuable than higher-edge trades.

**Existing Kalshi account state:** account has a few open long-running bets at the start of this work. They can be ignored or cashed out; they don't materially affect Phase 1 because the bot tracks paper state separately (`is_paper` row tagging in `auramaur/db/`) and the live bankroll cap of $200 is small enough that the open bets aren't load-bearing.

### Phase 2 — Polymarket US paper (parallel with Phase 1, week 2)

**Goal:** add a free, no-VPN diversification target on the same already-validated infra.

- Apply for Polymarket US developer keys (request copy drafted separately).
- Once keys arrive, configure as second exchange. Run paper alongside Phase 1's Kalshi paper/live.
- Same readiness criteria (§4) before any live flip on Polymarket US.

### Phase 3 — Polymarket Global behind PIA (week 2–3)

**Goal:** unlock the design-target market access by containerizing the bot's Polymarket connection behind a kernel-enforced PIA tunnel.

- Apply for Polymarket Global developer keys (request copy drafted separately).
- Adapt the `~/Developer/mac-server-setup` haugene-in-Podman pattern (`app-setup/podman-transmission-setup.sh`, `app-setup/containers/transmission/compose.yml`, `docs/plans/2026-03-08-containerized-transmission.md`). Reusable parts: rootful Podman machine, OpenVPN-PIA-in-container with `CAP_NET_ADMIN` + `/dev/net/tun`, kill-switch behavior on tunnel drop, keychain-sourced credential injection.
- The container runs Auramaur (or just the Polymarket exchange-client subprocess if we want a narrower trust boundary). Open question — see §7.
- Phase 3 has its own readiness window before any live flip; criteria are the same (§4), measured on Polymarket-Global paper trades.

---

## 4. Readiness criteria — the gate to flip from paper to live

All criteria must hold continuously over a rolling 7-day paper window before the gates flip on a given exchange. The window may need to extend if resolution cadence is slow (Brier and win-rate criteria need ≥30 resolved markets; this is the `calibration.min_samples` default).

| # | Signal | Threshold | Why |
|---|---|---|---|
| 1 | Cycle health | Zero unhandled exceptions in `auramaur.log`; analysis cycles complete within 2× their configured `analysis_seconds` interval | A bot that crashes or stalls in paper will crash with money in live |
| 2 | Data sources | Every enabled source ≥80% successful queries; no source silently empty for >24h | Calibration depends on input diversity; a dead source biases probability estimates |
| 3 | Risk gate pass-rate | Between 0.5% and 10% of analyzed markets approved | Approving 0% means edge or confidence floors are too tight; approving >10% means the gate is rubber-stamping |
| 4 | Calibration sanity (absolute Brier) | Bot's Brier on resolved markets ≤ 0.24 | Sanity check that the bot is better than always-50/50 (which scores 0.25). Not an edge test — see #5 |
| 5 | Edge over market (relative Brier) | Bot's Brier ≥ 0.02 lower than the market price's Brier on the same resolved events | The actual edge test. ~0.02 absolute improvement is the rough margin needed to overcome Kalshi's 7% fee and bid-ask slippage |
| 6 | Win rate on resolved trades | ≥ 52% on resolved paper trades | Edge survives variance with this margin |
| 7 | Net PnL after fees | ≥ break-even on the 7-day window after applying the exchange's fee on profitable trades | Bot is net-positive *after fees*, not just gross |
| 8 | Second-opinion divergence | Median ≤ 0.15, p95 ≤ 0.30 | Divergence high = primary and second-opinion analyzers disagree often = bot's confidence is overstated |

### Why Brier (and why two of them)

Brier score is the mean squared error between predicted probabilities and binary outcomes (0/1). It penalizes confident wrongness more than cautious wrongness. Reference points:

| Brier | What it means |
|---|---|
| 0.00 | Perfect |
| 0.10–0.15 | Strong forecaster (FiveThirtyEight on close elections) |
| 0.15–0.20 | About what liquid prediction markets themselves score |
| **0.25** | **Always-50/50 baseline** |
| 0.30+ | Worse than uniform — would improve by ignoring own signal |

**Brier ≠ accuracy.** A bot can be 78% accurate and have Brier 0.30 if it bets confidently and is wrong 22% of the time on "sure things." Worked example: predicting 0.70 on a market that resolves YES adds `(0.70 − 1.0)² = 0.09`; predicting 0.70 on a NO adds `(0.70 − 0.0)² = 0.49`.

We use **two** Brier criteria because absolute Brier alone is misleading:

- **Absolute Brier (#4)** rules out "the bot is wildly miscalibrated" — a basic floor at 0.24, slightly below the 0.25 uniform baseline.
- **Relative Brier vs. market (#5)** is the actual edge test. If the market scores 0.18 on the same events and the bot scores 0.20, we're trading worse-than-consensus and bleeding fees. We need ≥ 0.02 improvement over market consensus to have any meaningful edge after Kalshi's 7% fee and slippage.

---

## 5. Gate-flip ceremony

Real-money authorization is **manual**, not automatic. The ceremony per exchange:

1. Andrew runs `auramaur readiness --exchange <name>`. If any criterion in §4 fails, stop. Diagnose. Fix locally. Reset the rolling window.
2. If all pass, Andrew runs `auramaur live --confirm --exchange <name>`. This re-runs the readiness check, prints the criteria + values, and only proceeds if all still pass.
3. The command edits `config/defaults.yaml` (or the appropriate per-exchange override) to flip `execution.live: true` for that exchange, sets `AURAMAUR_LIVE=true` in `.env`, and prints the on-chain/on-account address that's about to handle real money.
4. Bot is restarted. First live cycle is monitored interactively for at least one full analysis cycle.

The kill switch (`auramaur kill`, which `touch`es `KILL_SWITCH`) remains the always-available abort.

---

## 6. PR-back-to-upstream policy

Goal: avoid the AI-slop PR failure mode. **We do not open an upstream PR until all three are true:**

1. **The change was made for our own reasons during deployment work** — not retrospectively scoped to "what could we PR upstream."
2. **The change has run live for ≥ 3 days with no regression** — measured against the relevant readiness criteria from §4, not just "tests pass."
3. **We have written down — in `docs/findings/`, not memory — what specifically broke or wasn't optimal upstream that motivated the change.** This is the part most AI PRs skip and is the actual evidence of "thoughtfully working through the code."

Each upstream PR links to the corresponding finding in its body so the upstream maintainer can read the motivation in our words, not the diff's.

---

## 7. Open questions to resolve as we go

- **Phase 3 trust boundary:** does the Polymarket-Global container run the entire bot, or just an exchange-client subprocess that the host bot talks to over a local socket? Container = simpler, smaller blast radius if compromised. Subprocess = lets the rest of the bot stay native and use non-VPN'd data sources without proxying everything through PIA.
- **Resolution cadence on Kalshi:** 30 resolved markets in 7 days requires the bot to actually trade on markets that resolve in that window. If most edges the bot finds are on slow-resolving political markets, the readiness window will stretch. We may need to set a `min_resolution_hours` floor explicitly in Phase 1 to bias toward faster markets — TBD once we see real data.
- **Open Kalshi positions at session start:** Andrew's pre-existing bets — ignore or cash out? Doesn't materially affect Phase 1 either way; deferred until we're about to flip Kalshi live.
- **Duplicate fee tables (Crypto.com only, Phase 3+ blocker):** `auramaur/strategy/signals.py` hardcodes `EXCHANGE_FEES` while `config/defaults.yaml` declares `arbitrage.exchange_fees`. They agree on Kalshi and Polymarket but disagree on Crypto.com (0.01 vs 0.075). Documented in `docs/findings/2026-04-28-duplicate-fee-tables.md`. Not Phase 1/2 critical; must be unified before any Crypto.com routing.
- ~~**Kelly fraction for first $200:**~~ **Resolved 2026-04-28** — investigation showed `auramaur/risk/regime.py` already implements regime-switched Kelly. At $200 equity it overrides the configured 0.30 fraction with growth-mode 0.50, max-stake 10% of equity ($20), and min-edge 2.5pp. The configured value is moot below $1000 equity. No Phase-1-specific override needed.

---

## 8. What this document is not

This is the deployment plan and the rationale behind decisions made in conversation. It is not:

- A code reference (use the codebase + `CLAUDE.md`).
- A status document (use `docs/findings/` for dated observations as work progresses).
- A complete pre-flight checklist (the `auramaur readiness` command will be that, executable).

If a decision in this document is overridden later, edit the relevant section *in place* with a dated note explaining the override — don't fork the document. The shared record should reflect current intent, not a chronological diary.
