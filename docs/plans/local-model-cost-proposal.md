# Proposal: Hybrid Local/Cloud Inference for Auramaur

**Date:** 2026-05-25
**Status:** Draft — not yet approved
**Author:** Andrew Rich (assisted by Claude)

## Problem

The bot is shut down because inference costs exceed trading revenue. At `api_intensity: low`, the bot still makes up to 30 Claude API calls/day — each strategic batch prompt is ~14.5k input tokens (Sonnet), and tool-use refinement burns Opus on top. Even conservatively, that's $5–15/day in API costs against a paper-trial edge of ~$45/day on *resolved* signals — but resolution takes weeks, capital is tied up, and the paper trial exposed cash management and portfolio refresh bugs that compressed realized returns well below theoretical edge.

The bot needs to run cheaply enough that inference cost is negligible, not a line item competing with trading profits.

## Current Cost Structure

| Stage | Model | Calls/Cycle | Tokens/Call | Purpose |
|---|---|---|---|---|
| Strategic batch | Sonnet | 1 | ~14.5k in, ~1.5k out | World model + batch probability estimates |
| Adversarial 2nd opinion | Sonnet | 0–1 | ~1k in | Cross-check (skipped at "low") |
| Tool-use refinement | Opus | 0–4 | ~1k in + web tools | Deep research on high-edge markets |
| Ensemble blend | Opus + Sonnet | 2× | varies | Multi-model weighting (disabled at "low") |

At "low" intensity with adaptive scheduling: ~8–30 API calls/day, heavily weighted toward peak hours. The expensive calls are tool-use (Opus) and ensemble (Opus + Sonnet in parallel).

## Proposal: Three-Tier Hybrid Architecture

Replace the current all-cloud pipeline with three tiers, where local models handle the volume and cloud models are reserved for high-stakes decisions.

### Tier 1 — Local screening (Ollama, free)

**Model:** Llama 3.3 70B or Qwen 3 32B (quantized to fit in RAM)
**Hardware:** Mac Studio M2 Ultra (192GB unified memory — can run 70B Q4 comfortably)
**Role:** Replace the strategic batch analyzer for routine cycles

What it does:

- Ingests the world model + market batch + evidence (same prompt structure as today)
- Produces probability estimates, confidence levels, and reasoning per market
- Updates the world model
- Runs every cycle, no API cost

What it does NOT do:

- Tool use (web search/fetch) — local models are unreliable at function calling
- Final trade decisions on high-edge markets — those escalate to Tier 2

**Key concern:** Signal quality. Local 70B models are measurably worse at calibrated probability estimation than Sonnet/Opus. The mitigation is that Tier 1 is a *filter*, not a *decider* — it identifies which markets are worth spending cloud tokens on. A false negative (missed opportunity) is acceptable; a false positive just wastes one Tier 2 call.

**Validation required before deployment:** Run Tier 1 against the last 30 days of resolved signals in shadow mode (local model produces estimates, Sonnet produces estimates, compare calibration). If local model's Brier score is >2× worse than Sonnet on the same markets, this tier is not viable.

### Tier 2 — Cloud refinement (Sonnet, paid but selective)

**Model:** Sonnet (not Opus)
**Role:** Refine Tier 1 output on markets where the local model found meaningful edge

Trigger conditions (any of):

- Tier 1 estimates edge > min_edge_pct (currently 2.5–3.5% depending on regime)
- Tier 1 confidence is MEDIUM or HIGH
- Market was flagged by the news reactor

What it does:

- Re-analyzes the specific market(s) with the full evidence set
- Produces calibrated probability + adversarial self-check in a single call
- This is the actual trade/no-trade decision

**Expected volume:** 3–8 Sonnet calls/day (vs. 15–30 today). Most cycles, Tier 1 finds nothing actionable and no cloud call is made.

**Cost estimate:** ~$0.50–2.00/day at current Sonnet pricing.

### Tier 3 — Cloud deep-dive (Opus, rare)

**Model:** Opus
**Role:** Tool-use research on the highest-conviction opportunities

Trigger conditions (all of):

- Tier 2 confirmed edge > 5%
- Confidence is HIGH
- Stake would exceed $15 (worth the research cost)

What it does:

- Web search + fetch for real-time verification
- Same tool-use analyzer as today, but triggered far less often

**Expected volume:** 0–2 Opus calls/day. Many days, zero.

**Cost estimate:** ~$0.00–1.00/day.

## Implementation

### Phase 1: Ollama integration + shadow mode (1–2 sessions)

1. Add an `OllamaAnalyzer` implementing the `MarketAnalyzer` protocol — same interface as `StrategicAnalyzer`, backed by a local Ollama HTTP API call instead of Anthropic SDK
2. Add `analysis.local_model` config field (model name for Ollama, e.g. `llama3.3:70b-instruct-q4_K_M`)
3. Add `analysis.mode = "hybrid"` that wires Tier 1 → Tier 2 → Tier 3
4. Shadow mode: run local and cloud in parallel for 48 hours, log both outputs, compare calibration — no trading decisions change

### Phase 2: Validation (offline, no code)

1. Compare Brier scores: local vs. Sonnet on the same 48h of markets
2. Compare edge detection: did local model flag the same high-edge markets Sonnet did?
3. Measure false-negative rate: markets where Sonnet found edge but local model didn't
4. Decide go/no-go based on results

### Phase 3: Cutover (1 session)

1. Switch `analysis.mode` to `"hybrid"` in `defaults.yaml`
2. Set daily_claude_call_budget to 10 (hard ceiling)
3. Monitor for 1 week in paper mode
4. If calibration holds, enable live trading

## Cost Projection

| Configuration | Est. Daily API Cost | Est. Monthly |
|---|---|---|
| Current (low intensity, all cloud, extra-usage tier) | $50–75 | $1,500–2,250 |
| Hybrid (local screening + selective cloud) | $0.50–3.00 | $15–90 |
| All-local (no cloud, Tier 1 only) | $0.00 | $0 |

The hybrid target is ~$1.50/day ($45/month). The current $50–75/day spend (extra-usage pricing) is clearly unsustainable — it exceeds even the theoretical $45/day edge from the paper trial, meaning the bot is guaranteed net-negative before trading losses are even considered.

## Risks

1. **Local model calibration is poor.** The biggest risk. Prediction markets require well-calibrated probability estimates, not just directional calls. Local models may be systematically overconfident or underconfident. Mitigation: Phase 2 validation is a hard gate.

2. **Local model is too slow.** A 70B model on M2 Ultra generates ~15–25 tok/s. A 14.5k-token prompt with 1.5k-token response takes ~60–90 seconds per batch. At low intensity with 3 markets per batch, this is fine. At higher intensity it becomes a bottleneck. Mitigation: use a smaller model (Qwen 3 32B at ~40 tok/s) if latency matters, or batch more aggressively.

3. **World model drift.** If the local model's world model updates are lower quality, the persistent state degrades over time. Mitigation: periodically (daily?) run one Sonnet cycle to "reset" the world model, or use Sonnet's world model updates exclusively.

4. **Maintenance burden.** Ollama models need manual updates, the Mac needs to stay on, thermal/power considerations. This is small but nonzero ops overhead.

5. **The math still doesn't work.** Even at $1.50/day, if the trading edge after all fixes is <$1.50/day, the bot is net negative regardless of inference cost. This proposal reduces one cost center but doesn't fix the underlying edge-vs-capital question.

## Decision Framework

If Phase 2 validation shows:

- Local Brier score within 1.5× of Sonnet → proceed to Phase 3
- Local Brier score 1.5–2× of Sonnet → proceed but with tighter Tier 2 triggers (escalate more to cloud)
- Local Brier score >2× of Sonnet → abandon local screening, consider all-Haiku cloud instead

Fallback if hybrid doesn't pan out: run the entire pipeline on Haiku ($0.25/MTok in, $1.25/MTok out) — roughly 10× cheaper than Sonnet — and validate whether Haiku's calibration is good enough. This is simpler (no Ollama infra) but still costs money.

## Not In Scope

- Changing the trading strategy or risk parameters
- Fixing the cash management / portfolio refresh bugs from the paper trial
- Switching to a non-Anthropic cloud provider
- Fine-tuning a local model on prediction market data (interesting but premature)
