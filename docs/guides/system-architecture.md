# Auramaur System Architecture

Working system overview as of 2026-05-23. Both Kalshi and Polymarket bots are live and executing trades.

## Pipeline Shape

```
discovery → data aggregation → NLP analysis → signal detection → risk evaluation → allocation → execution → reconciliation → calibration
```

Each stage is a protocol-based abstraction. Adding a new exchange or analysis mode means implementing the protocol — the pipeline doesn't change.

## Deployment Architecture

### Host

- **Machine**: MIMOLETTE (Mac mini, macOS)
- **Repo**: `/Volumes/extra-vieille/Workspaces/Auramaur/` (external SSD)
- **Runtime venv**: `~/Library/Application Support/auramaur/.venv` (internal disk — TCC blocks launchd from accessing `/Volumes/`)

### launchd Agents

Both bots run as macOS launchd user agents:

```
~/Library/LaunchAgents/com.auramaur.kalshi.plist
~/Library/LaunchAgents/com.auramaur.polymarket.plist
```

Control:

```bash
launchctl stop com.auramaur.kalshi
launchctl start com.auramaur.kalshi

launchctl stop com.auramaur.polymarket
launchctl start com.auramaur.polymarket
```

### Editable Install

The runtime venv uses an editable pip install pointing to the repo:

```
~/Library/Application Support/auramaur/.venv → /Volumes/extra-vieille/Workspaces/Auramaur/
```

This means **code changes in the repo are picked up on next bot restart** — no reinstall needed. Just:

```bash
# 1. Make code changes in the repo
# 2. Restart the affected bot
launchctl stop com.auramaur.polymarket
launchctl start com.auramaur.polymarket
```

Verify with: `python -c "import auramaur; print(auramaur.__file__)"` — should print the repo path, not site-packages.

### Update Sequence

When deploying a code change:

1. Edit code in the repo
2. Run tests: `uv run pytest`
3. Commit and push (goes through pre-commit review hooks)
4. Restart the affected bot(s): `launchctl stop/start`
5. Check logs: `tail -f ~/Library/Application\ Support/auramaur/auramaur.log`

No `pip install`, `uv sync`, or venv rebuild needed for pure Python changes. Only rebuild the venv if `pyproject.toml` dependencies change.

## VPN / Geoblock Infrastructure

### Gluetun Container

Polymarket geoblocks US IPs. The bot routes discovery requests through a Gluetun VPN proxy:

```
Polymarket bot → http://localhost:8888 → Gluetun (Podman container) → PIA VPN (Portugal) → Internet
```

The Gluetun container runs as a Podman container with PIA credentials. Configuration lives in `~/Developer/mac-server-setup/`.

The CLOB API itself does not appear to geoblock — only the Gamma discovery API does. So the proxy is used selectively.

### Proxy Configuration

The bot reads the proxy URL from the environment. The launchd plist sets:

```xml
<key>HTTPS_PROXY</key>
<string>http://localhost:8888</string>
```

## Credentials

All secrets are stored in macOS Keychain (`~/Library/Keychains/auramaur.keychain-db`), accessed via the `security` CLI:

```bash
security find-generic-password -s SERVICE_NAME -w ~/Library/Keychains/auramaur.keychain-db
```

### Polymarket Keys

| Service Name | Description |
|---|---|
| `POLYGON_PRIVATE_KEY` | MetaMask wallet private key |
| `POLYMARKET_API_KEY` | CLOB API key |
| `POLYMARKET_API_SECRET` | CLOB API secret |
| `POLYMARKET_PASSPHRASE` | CLOB API passphrase |
| `POLYMARKET_PROXY_ADDRESS` | Deposit wallet address |

### Kalshi Keys

| Service Name | Description |
|---|---|
| `KALSHI_API_KEY` | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY` | Kalshi RSA private key (PEM) |

### Key Addresses

| Role | Address |
|---|---|
| EOA (MetaMask) | `0xDa081332B788473Ec2C429F19099a5F229a0dC03` |
| Deposit Wallet (current) | `0x30c9b9dC590071743a5b674475cE179FDac25786` |
| Deposit Wallet (old) | `0x329eeaF9b507Da1A4d4a1187F47d0dFCd19c1Cc0` |

## Safety Gates

Three independent gates must ALL be open for live trading:

1. **`AURAMAUR_LIVE=true`** environment variable
2. **`execution.live=true`** in `config/defaults.yaml`
3. **`dry_run=False`** per-order flag

Plus a kill switch: if a `KILL_SWITCH` file exists in CWD, all trading halts immediately.

On-chain redemption has a fourth gate: `AURAMAUR_ENABLE_REDEMPTION=true`.

## Monitoring

### Logs

```bash
# Live bot logs
tail -f ~/Library/Application\ Support/auramaur/auramaur.log

# Structured JSON format (structlog)
# Filter for specific events:
cat auramaur.log | jq 'select(.event == "order.live")'
```

### Dashboard

Rich terminal dashboard (read-only, against the live SQLite DB):

```bash
uv run auramaur dashboard
```

Streamlit observability dashboard:

```bash
# From ~/Developer/Auramaur/observability/
./launch.sh
# Accessible at http://localhost:8501
# Binds to 0.0.0.0 by default for LAN access
```

### Database

SQLite with WAL mode. Auto-discovers DB files:

```
~/Library/Application Support/auramaur/auramaur.db      # primary
~/Library/Application Support/auramaur/auramaur_2.db     # secondary instance
```

Multiple bot instances auto-acquire DB slots via `fcntl` locking (up to 19 slots).

## Exchange Configuration

### Polymarket

- Signature type: `3` (POLY_1271) — **not** `2` (POLY_GNOSIS_SAFE)
- Routes through Gluetun proxy for Gamma API discovery
- CLOB direct for order execution
- See `polymarket-sdk-bug.md` for the SDK conundrum

### Kalshi

- Environment: `prod`
- Direct API access (no VPN needed — Kalshi is US-legal)
- RSA key authentication
