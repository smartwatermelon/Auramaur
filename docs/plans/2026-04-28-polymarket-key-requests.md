# Polymarket Developer-Key Request Drafts

**Status:** Draft 2026-04-28 — usage TBD pending Polymarket's current onboarding path
**Owner:** Andrew Rich

This document holds copy ready to use when applying for Polymarket developer credentials on Andrew's two existing accounts (US-restricted and Global). Both drafts are tuned to the realities of Phase 1 of `docs/plans/2026-04-28-deployment-plan.md` — modest capital cap, paper-validated, kill-switch operational.

## Important — confirm the application path first

The bot already calls `py_clob_client.ApiCreds(api_key, api_secret, api_passphrase)` to authenticate orders (see `auramaur/exchange/client.py:308-381`). Polymarket's CLOB has historically supported **self-service API-credential derivation**: sign a one-time message with the wallet's private key and the SDK returns a (key, secret, passphrase) triple that the bot can then use indefinitely. If that path is still open in 2026, **no formal application is needed** — the §3 self-service checklist below is the entire workflow.

Before sending either of the request drafts in §1 or §2, check:

1. Polymarket's developer/docs site (likely `docs.polymarket.com`) for a current onboarding flow.
2. Whether your account has API access available in its dashboard.
3. The output of `py_clob_client.ClobClient.create_api_key()` — if it returns credentials directly, you're done.

If a formal application *is* required (e.g. for elevated rate limits, larger position caps, or post-2026 policy changes), use the §1 or §2 template as a starting point.

---

## §1. Request template — Polymarket US

**Use this if:** Polymarket US (us.polymarket.com or the US-restricted variant) requires a developer-access application separate from self-service derivation.

**Send to:** developer/partnerships email or form on the US site (look up; do not guess)

**Subject:** Developer API access for autonomous prediction-market trading bot — small-cap personal project

**Body:**

> I am writing to request developer API credentials on my existing Polymarket US account ({{registered email}}, account holder Andrew Rich).
>
> **What I'm building.** A personal autonomous trading bot that uses LLM-driven news analysis to estimate probabilities on prediction markets and trades when there's edge after fees. The codebase is open source (a fork of github.com/DarriEy/Auramaur — the project profiled in Bloomberg Opinion's "I Built an AI Trading Platform in Six Days" piece, 2026-04-28). I am running it as a learning project on top of a real-but-modest capital cap, not as a business or a fund.
>
> **Capital and venue scope on Polymarket US.** Phase 1 is capped at $200 USD across all positions, with a per-market max stake of $20 (10% of equity, regime-switched). I understand the US-version venue is restricted to sports markets and a small set of wide-open election markets — both are in scope and the bot's analysis stack (Claude, news/RSS aggregation, Reddit, FRED, web search) is wired to handle them. I do not anticipate a need for elevated rate limits or larger position caps during Phase 1.
>
> **Risk management.** Every order passes through 15 independent pre-trade risk checks (kill switch, drawdown, max stake, daily loss limit, edge floor, liquidity floor, spread cap, time-to-resolution, second-opinion divergence, etc.). Live trading requires three independent gates to all be open (env var + config flag + per-order flag); a `KILL_SWITCH` file on disk halts every order path immediately. Phase 1 only goes live after a 7-day paper-trading window where the bot demonstrates calibration (Brier score), edge over market consensus (relative Brier), and net-of-fees profitability — see §4 of `docs/plans/2026-04-28-deployment-plan.md` in the repo for the readiness criteria.
>
> **Compliance.** I am a US resident and understand the venue rules for the US version. I will not attempt to access markets or features outside US jurisdiction from this account. KYC has been completed for the existing account; I am happy to provide additional verification on request.
>
> **Operational details I can confirm on request.** Wallet address, account email, expected daily order volume (Phase 1: ≤ 50 orders/day), data residency for the bot's logs (Northern California, on my own hardware).
>
> Thank you for considering. Happy to answer any questions or provide additional context.
>
> Best,
> Andrew Rich
> {{contact email}}
> github.com/{{github}}

---

## §2. Request template — Polymarket Global

**Use this if:** Polymarket Global requires a developer-access application separate from self-service derivation.

**Send to:** developer/partnerships email or form on the Global site

**Subject:** Developer API access for autonomous prediction-market trading bot — small-cap personal project

**Body (delta from §1 in *italic*):**

> I am writing to request developer API credentials on my existing Polymarket Global account ({{registered email}}, account holder Andrew Rich).
>
> **What I'm building.** [Same as §1.]
>
> **Capital and venue scope on Polymarket Global.** *Phase 3 of my deployment plan targets Polymarket Global as the primary venue once Kalshi (Phase 1, $200 cap) and Polymarket US (Phase 2, paper-only initial) have validated the bot's calibration end-to-end. Initial cap on Global will be $500 USD across all positions, with a per-market max stake of $50 (10% of equity, same regime-switched policy as Phase 1). I would expect to scale gradually if and only if the bot's measured edge over market consensus stays positive across a rolling 7-day window — and to scale down or kill if it does not.*
>
> **Risk management.** [Same as §1.]
>
> **Compliance.** *I am a US resident; I understand that Polymarket Global is not available to US persons and I have a separate account on Polymarket US for US-eligible markets (covered under a parallel request, §1 of the same document). All Global trading from my end will route through a non-US VPN endpoint at the network layer (PIA, kernel-enforced via a single-purpose Podman container running OpenVPN — the bot's Polymarket Global connection has no path to the public internet that bypasses the tunnel; if the VPN drops, the container's network namespace fails closed). KYC has been completed for the existing account; happy to provide additional verification on request.*
>
> **Operational details I can confirm on request.** [Same as §1.]
>
> Thank you for considering. Happy to answer any questions or provide additional context.
>
> Best,
> Andrew Rich
> {{contact email}}
> github.com/{{github}}

---

## §3. Self-service derivation checklist (probably the actual path)

**Use this if:** Polymarket's CLOB still supports self-service API-credential derivation in 2026 (the historical norm — confirm before assuming).

For each of the two accounts (US, Global) separately:

```bash
# 1. Make sure the wallet for the target account is funded with at
#    least a small USDC balance on Polygon (matches what the account
#    would actually trade). The bot uses POLYGON_PRIVATE_KEY from .env.
#    Do NOT reuse a key across accounts.

# 2. Get the wallet address that the private key derives.
uv run python -c "
from eth_account import Account
import os
acct = Account.from_key(os.environ['POLYGON_PRIVATE_KEY'])
print('address:', acct.address)
"

# 3. Verify the address matches the address Polymarket has registered
#    for this account (look in account settings or /me on the website).

# 4. Bootstrap the API credential triple via py-clob-client. The SDK
#    handles the EIP-712 message signing under the hood.
uv run python -c "
from py_clob_client.client import ClobClient
import os
c = ClobClient(
    'https://clob.polymarket.com',
    key=os.environ['POLYGON_PRIVATE_KEY'],
    chain_id=137,
)
creds = c.create_or_derive_api_creds()
print('api_key:', creds.api_key)
print('api_secret:', creds.api_secret)
print('api_passphrase:', creds.api_passphrase)
"

# 5. Add the three values to .env (per-account; do NOT mix US and Global
#    creds in the same .env). The bot reads them via:
#       POLYMARKET_API_KEY
#       POLYMARKET_API_SECRET
#       POLYMARKET_PASSPHRASE
#       POLYMARKET_PROXY_ADDRESS  (the wallet address from step 2)

# 6. Smoke test: confirm the credentials can fetch open orders without
#    placing any.
uv run python -c "
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
import os
c = ClobClient(
    'https://clob.polymarket.com',
    key=os.environ['POLYGON_PRIVATE_KEY'],
    chain_id=137,
)
c.set_api_creds(ApiCreds(
    api_key=os.environ['POLYMARKET_API_KEY'],
    api_secret=os.environ['POLYMARKET_API_SECRET'],
    api_passphrase=os.environ['POLYMARKET_PASSPHRASE'],
))
print('open orders:', len(c.get_orders()))
"
```

**Important — Global account derivation must run from a non-US IP.** Polymarket's CLOB will refuse the credential bootstrap (and many subsequent calls) if the connecting IP is US-geolocated and the account is the Global one. Run step 4 either:

- Via the Phase-3 PIA-VPN container (preferred — same trust boundary the bot will use in production), or
- Via a one-shot `proxychains4`/`mullvad` session manually, after which the credentials persist regardless of where you connect from. (Many users report credential *use* works from a US IP once derived, but *derivation itself* requires a non-US IP. Verify before assuming.)

The US account derivation runs from a US IP normally.

**Phase 1 paper trading does NOT need either set of credentials.** Paper-mode orders never reach the CLOB; the bot routes them to `auramaur/exchange/paper.py::PaperTrader.execute` whenever any of the three live gates is closed. Derive credentials only when you're preparing to flip a specific account live, not earlier.

---

## §4. What this document is not

- It is not a guide to Polymarket's account-creation, KYC, or wallet-funding flow. Those are upstream of API credentials and Polymarket's own docs are authoritative.
- It is not a contract — the request templates in §1 and §2 are starting points, not legally-vetted language.
- It is not a substitute for reading Polymarket's current developer documentation. Confirm the application channel and the credential-derivation process before sending §1 or §2 or running §3.
