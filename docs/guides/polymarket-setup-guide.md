# Polymarket Setup Guide

How to go from zero to a funded Polymarket account accessible via API — including the VPN dance required for US-based traders.

## Prerequisites

- **MetaMask** browser extension (Polygon network configured)
- **Coinbase** account (or any fiat-to-USDC on-ramp)
- **VPN** with a non-US, non-restricted endpoint (we use PIA with Portugal)
- A debit card for Coinbase purchases

## Step 1: Sign Up for Polymarket

1. Connect to VPN — use **Portugal** (or any non-US, non-restricted country). Avoid Panama; it's flagged as restricted.
2. Open a fresh browser window (or incognito) and go to `polymarket.com`.
3. Click **Sign Up** and choose **MetaMask** as the wallet provider.
4. Approve the MetaMask connection. Polymarket will create your account linked to your Ethereum address.

> **VPN note**: The VPN must be active for both signup AND ongoing trading. Polymarket geoblocks US IPs. PIA's Portugal endpoint works reliably; other providers/countries may vary.

## Step 2: Complete KYC

Polymarket requires KYC to trade. The VPN toggle sequence matters:

1. **Start with VPN ON** (Portugal). Navigate to Account Settings and begin KYC.
2. The KYC provider (Jumio/similar) will ask for ID verification. You may need to **toggle VPN OFF** briefly during the ID photo/selfie capture step — some KYC providers flag VPN connections.
3. Submit your real identity documents (passport, driver's license).
4. **Toggle VPN back ON** after submission.
5. Wait for approval (usually minutes to hours).

> **Important**: Use your real identity. KYC is non-negotiable for trading. The VPN is for geo-access, not identity concealment.

## Step 3: Link Coinbase to Polymarket

Coinbase is the easiest USDC on-ramp for US users:

1. If you don't have a **Coinbase** account, create one at `coinbase.com` and complete their KYC.
2. In Polymarket, go to **Deposit** → look for the Coinbase option or copy your Polygon USDC deposit address.
3. Note your Polymarket deposit wallet address (visible in the Deposit modal). This is a Polygon-network smart contract wallet.

## Step 4: Link a Debit Card to Coinbase

1. In Coinbase, go to **Settings** → **Payment Methods** → **Add a Payment Method**.
2. Add your debit card. Credit cards typically don't work for crypto purchases.
3. Verify the card with any micro-transaction if prompted.

## Step 5: Purchase USDC on Coinbase

1. In Coinbase, go to **Buy/Sell** → search for **USDC**.
2. Buy the desired amount of USDC. It should be available instantly for debit card purchases.
3. USDC on Coinbase is natively on Ethereum. You'll bridge to Polygon in the next step (Polymarket handles this automatically if you use their deposit flow).

## Step 6: Transfer USDC to Polymarket

There are two paths:

### Option A: Polymarket's built-in deposit (recommended)

1. In Polymarket (VPN ON), click **Deposit**.
2. Connect Coinbase or paste your Coinbase USDC withdrawal to the displayed Polygon address.
3. Polymarket bridges from Ethereum to Polygon automatically if needed.

### Option B: Manual Polygon transfer

1. In Coinbase, go to **Send/Receive** → USDC.
2. Paste your Polymarket deposit wallet address.
3. Select **Polygon** as the network (NOT Ethereum — wrong network = lost funds).
4. Confirm and send. Transaction takes ~2 minutes on Polygon.

> **Verification**: After transfer, check your Polymarket balance in the web UI. It should appear within a few minutes.

## Step 7: Wallet Migration (if prompted)

As of mid-2025, Polymarket is migrating users to a new "deposit wallet" architecture:

1. You may see an **"Upgrade your account"** banner with a **Migrate** button.
2. Click **Migrate**. This deploys a new deposit wallet contract and transfers your funds.
3. Note the **new deposit wallet address** — you'll need it for API configuration.

> This migration is a one-time event. The new deposit wallet uses EIP-1271 signatures (signature type 3 / POLY_1271) instead of the old Gnosis Safe proxy (signature type 2).

## Step 8: Place One Web UI Trade

**This step is critical for API trading.** After migration, you must place at least one trade through the Polymarket web UI before the API will accept orders. This appears to register the EOA-to-deposit-wallet mapping on the CLOB server.

1. With VPN on, find any active market.
2. Place a small trade (minimum $1).
3. The trade must execute — a cancelled limit order doesn't count.

## Step 9: API Key Setup

Once you've placed a web UI trade:

1. Your API keys can be derived programmatically using the `py_clob_client_v2` SDK (see `polymarket-sdk-bug.md` for the caveats).
2. Store keys securely. In Auramaur, we use macOS Keychain with service-name lookup:
   - `POLYMARKET_API_KEY`
   - `POLYMARKET_API_SECRET`
   - `POLYMARKET_PASSPHRASE`
   - `POLYMARKET_PROXY_ADDRESS` (the deposit wallet address)
   - `POLYGON_PRIVATE_KEY` (your MetaMask private key)

## VPN Requirements Summary

| Action | VPN Required? | Notes |
|--------|:---:|-------|
| Sign up | YES | Non-US endpoint |
| KYC | TOGGLE | Off during ID capture, on otherwise |
| Deposit USDC | NO | On-chain, no geo-check |
| Web UI trading | YES | Every session |
| API trading | NO* | Bot uses Gluetun proxy for Gamma API; CLOB itself doesn't geocheck |

*The Polymarket CLOB API does not appear to geoblock. However, the Gamma discovery API does. The bot routes through a VPN proxy for discovery while hitting the CLOB directly for orders.

## Troubleshooting

- **"Trading restricted in your region"**: Switch VPN countries. Portugal works; Panama doesn't.
- **"Upgrade your account"**: Complete the wallet migration (Step 7).
- **API orders rejected after migration**: Place a web UI trade first (Step 8). See `polymarket-sdk-bug.md`.
- **Balance shows $0 via API**: Check you're querying with the correct `signature_type` (3 for POLY_1271, not 2).
