# The Polymarket SDK Conundrum

A documentation of the `py_clob_client_v2` SDK's API key binding bug, the exhaustive debugging process, and the solution that finally worked.

## The Problem

After migrating to a new Polymarket deposit wallet, API order submission fails with:

```
the order signer address has to be the address of the API KEY
```

This happens despite correct signature generation, correct funder address, and correct signature type configuration.

## Root Cause

The `py_clob_client_v2` SDK has a known, unfixed bug with 15+ open GitHub issues. The bug is in two places:

### 1. L1 Authentication: API Key Binding

In `headers/headers.py`, line 29:

```python
POLY_ADDRESS: signer.address()
```

When you derive or create API keys via L1 authentication, the SDK always sends `POLY_ADDRESS` as the EOA (externally owned account) address — regardless of the `funder` parameter. This means **all API keys are registered on the CLOB server as belonging to the EOA**, not the deposit wallet.

### 2. L2 Authentication: Request Signing

In the same file, L2 headers also hardcode `POLY_ADDRESS: signer.address()`. Every authenticated API request identifies itself as coming from the EOA.

### The Mismatch

After wallet migration to POLY_1271 (EIP-1271 deposit wallets), the CLOB server expects:

- Orders where `maker` = deposit wallet address
- API keys bound to the deposit wallet address
- Authenticated requests identifying as the deposit wallet

But the SDK sends:

- Orders where `maker` = deposit wallet (correct, via `builder.funder`)
- API keys bound to the EOA (wrong, hardcoded)
- Requests identifying as the EOA (wrong, hardcoded)

The `maker` and `signer` fields in the signed order are correct (the `OrderBuilder` respects the `funder` parameter), but the API authentication layer doesn't match.

## What We Tried (Exhaustive)

### Attempt 1: EIP-1271 Wrapped L1 Auth (4 variations)

Tried wrapping the L1 authentication signatures in EIP-1271 format (Solady ERC-7739 `PersonalSign` wrapper), sending them with `POLY_ADDRESS` set to the deposit wallet. All four variations returned:

```
401: Invalid L1 Request headers
```

The CLOB server simply does not support EIP-1271 for L1 authentication — only for order signatures.

### Attempt 2: L2 POLY_ADDRESS Header Patching

Monkey-patched `create_level_2_headers` to override `POLY_ADDRESS` with the deposit wallet address on every request. Order submission returned:

```
the order signer address has to be the address of the API KEY
```

The API key was still bound to the EOA from when it was created. Changing the header doesn't change the server's record of which address owns the key.

### Attempt 3: Pure EOA Trading (sig_type=0)

Created a client with `signature_type=0` (EOA) and no funder, so everything — maker, signer, API key — would be the EOA address. Result:

```
maker address not allowed, please use the deposit wallet flow
```

Once an account has migrated to deposit wallets, the CLOB server rejects orders from the raw EOA.

### Attempt 4: Create/Derive Fresh API Keys

Used `client.create_or_derive_api_key()` and `client.derive_api_key()`. Both returned the **same key** every time (deterministic derivation from EOA). The key is always bound to the EOA due to the L1 header bug.

### Attempt 5: Different Signature Types

Tested `signature_type=2` (POLY_GNOSIS_SAFE) and `signature_type=3` (POLY_1271) in various combinations. With sig_type=2, balance queries returned $0 for the new deposit wallet (only the old proxy wallet had funds under sig_type=2).

## What Finally Worked

The solution required four steps, all of which are necessary:

### 1. Wallet Migration

Polymarket's web UI prompted "Upgrade your account" with a Migrate button. This deployed a new deposit wallet contract:

- **Old**: `0x329eeaF9b507Da1A4d4a1187F47d0dFCd19c1Cc0` (Gnosis Safe proxy)
- **New**: `0x30c9b9dC590071743a5b674475cE179FDac25786` (EIP-1271 deposit wallet)

### 2. Web UI Trade

Placed one manual trade through the Polymarket web UI. This step appears to register the EOA-to-deposit-wallet relationship on the CLOB server in a way that the SDK's `derive_api_key` cannot replicate. Without this, API trading fails even with correct configuration.

### 3. Signature Type Change

Changed `signature_type=2` (POLY_GNOSIS_SAFE) to `signature_type=3` (POLY_1271) in `auramaur/exchange/client.py`. The new deposit wallet requires POLY_1271 signatures; POLY_GNOSIS_SAFE was for the old proxy wallet architecture.

### 4. Keychain Update

Updated `POLYMARKET_PROXY_ADDRESS` to the new deposit wallet address in the macOS keychain.

## Why This Works (Theory)

When you trade via the Polymarket web UI, their frontend:

1. Creates the API key binding with the correct deposit wallet address (not using the SDK)
2. Registers the EOA → deposit wallet mapping on the CLOB server
3. Sets up the necessary on-chain approvals

After this one-time setup, the SDK's buggy `POLY_ADDRESS: signer.address()` header happens to work because the CLOB server now has the EOA → deposit wallet mapping and can resolve it server-side.

The SDK bug means you **cannot** bootstrap API trading purely through the SDK. The web UI trade is a hard prerequisite.

## Signature Type Reference

| Value | Name | Use Case |
|:-----:|------|----------|
| 0 | EOA | Direct externally-owned account signing |
| 1 | POLY_PROXY | Legacy Polymarket proxy contracts |
| 2 | POLY_GNOSIS_SAFE | Old Gnosis Safe multisig wallets |
| 3 | POLY_1271 | New EIP-1271 deposit wallets (current) |

## Key Takeaways

1. The SDK bug is unlikely to be fixed — it's been open for months with no movement.
2. The web UI trade prerequisite is undocumented and can only be discovered empirically.
3. If Polymarket changes their wallet architecture again, expect similar breakage.
4. The `funder` parameter in `ClobClient` correctly configures the `OrderBuilder` but has no effect on L1/L2 authentication headers.
