# 2026-04-28 — `.env.example` gaps and misleading entries

**Context:** Trying to start the bot in Phase 1 paper mode against Kalshi. Discovered that `.env.example` does not reflect the actual env-var surface the bot reads.

## Finding — Two distinct gaps in `.env.example`

### Gap 1: Missing Kalshi variables

The bot's Kalshi client (`auramaur/exchange/kalshi.py:67-69`) reads:

- `KALSHI_API_KEY` (mapped to `Settings.kalshi_api_key`)
- `KALSHI_PRIVATE_KEY_PATH` (mapped to `Settings.kalshi_private_key_path`)

Neither was listed in the upstream `.env.example`. Anyone trying to run Kalshi-first hits a confusing failure: the bot starts, the Kalshi client tries to `_init_api()`, the kalshi-python SDK refuses to authenticate without a key, and the Kalshi cycle is silently disabled. The Anthropic SDK and Polymarket SDK are listed, but Kalshi — which is one of the bot's three supported exchanges — was not.

`config/defaults.yaml` declares `kalshi.enabled: true`, so by default the bot expects to talk to Kalshi. If `.env.example` is the canonical onboarding doc, leaving Kalshi credentials out of it is a real gap.

### Gap 2: Misleading Anthropic entries

`.env.example` lists:

```
ANTHROPIC_API_KEY_PRIMARY=sk-ant-...
ANTHROPIC_API_KEY_SECONDARY=sk-ant-...
```

…with a comment that says "(two accounts for dual analysis)". The implication is that the bot calls the Anthropic SDK with these keys.

**It does not.** `auramaur/nlp/analyzer.py:_call_claude_cli` invokes the `claude` CLI as a subprocess (`subprocess.create_subprocess_exec("claude", "-p", prompt, ...)`), relying on whatever the local `claude` install is authenticated against. The two API-key settings are declared in `config/settings.py:220-221` but never read by any production code path; they are dead settings.

For long-lived non-interactive auth (servers, containers, agents), the right knob is `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, which the CLI itself reads. There is no Anthropic SDK call to direct an API key at.

The misleading entries cost time during onboarding: a new operator naturally assumes that filling them in is necessary for the bot to talk to Claude, and may go through the trouble of provisioning two API keys before discovering they are unused.

## Why this matters

The combination — missing Kalshi vars + misleading Anthropic vars — means a fresh-checkout operator who fills in `.env.example` exactly as written gets a bot that:

1. Has two unused API keys configured.
2. Cannot talk to Kalshi.
3. Cannot talk to Claude unless the local `claude` CLI happens to be authenticated, which `.env.example` does not mention.

The bot will start, log warnings, and silently produce no trades. The failure mode is "looks like it's running but does nothing", which is the worst onboarding experience.

## Fix applied in this commit

`.env.example` updated to:

- Add `CLAUDE_CODE_OAUTH_TOKEN` with a comment explaining the CLI-subprocess auth path.
- Demote the two `ANTHROPIC_API_KEY_*` entries with a comment noting they are vestigial.
- Add `KALSHI_API_KEY` and `KALSHI_PRIVATE_KEY_PATH` under a Phase 1 heading.
- Group the file by phase / purpose so a Phase-1-only operator can see at a glance which keys matter for them (Claude OAuth + Kalshi pair).
- Add `POLYMARKET_PROXY_ADDRESS` and `AURAMAUR_ENABLE_REDEMPTION` which were also missing despite being read by the bot.

## Upstream PR-ability

**Strong candidate.** Documentation-only change with a clear "this misled me, here's the actual contract" justification. Verifies easily upstream by running `grep -rE "^[A-Z_]+:" config/settings.py` and cross-checking against `.env.example`.

Suggest filing as a single PR after the local soak window. The commit message and this finding can serve as the PR body.
