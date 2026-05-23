#!/usr/bin/env bash
# Auramaur launchd wrapper — Datasette + Streamlit observability dashboard.
# Loads exchange API keys from the auramaur keychain so the Account Overview
# section can query live balances and positions from Kalshi and Polymarket.

set -uo pipefail

REPO="${HOME}/Developer/Auramaur"
DATA_DIR="${HOME}/Library/Application Support/auramaur"
KEYCHAIN="${HOME}/Library/Keychains/auramaur.keychain-db"

log() {
  local _ts
  _ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '[%s] [auramaur-observability] %s\n' "${_ts}" "$*"
}

kc() {
  security find-generic-password -a auramaur -s "$1" -w "${KEYCHAIN}" 2>/dev/null
}

if [[ ! -d "${REPO}" ]]; then
  log "ERROR: repo not found at ${REPO}"
  exit 1
fi

if [[ ! -d "${DATA_DIR}" ]]; then
  log "ERROR: data directory not found at ${DATA_DIR}"
  exit 1
fi

if [[ ! -f "${KEYCHAIN}" ]]; then
  log "ERROR: keychain not found at ${KEYCHAIN} — run bootstrap-keychain.sh"
  exit 1
fi

# The auramaur keychain uses an empty password (set during bootstrap).
if ! security unlock-keychain -p '' "${KEYCHAIN}" 2>/dev/null; then
  log "ERROR: could not unlock keychain at ${KEYCHAIN} — run bootstrap-keychain.sh"
  exit 1
fi

# Load exchange API credentials for the Account Overview section.
# Missing keys are non-fatal — the dashboard degrades gracefully,
# showing "N/A" for exchanges without credentials.
_loaded=0
for key in KALSHI_API_KEY KALSHI_PRIVATE_KEY POLYGON_PRIVATE_KEY \
  POLYMARKET_API_KEY POLYMARKET_API_SECRET POLYMARKET_PASSPHRASE \
  POLYMARKET_PROXY_ADDRESS; do
  val="$(kc "${key}")"
  if [[ -n "${val}" ]]; then
    export "${key}=${val}"
    _loaded=$((_loaded += 1))
  else
    log "WARN: ${key} not found in keychain (exchange may show as N/A)"
  fi
done

log "Loaded ${_loaded}/7 exchange credentials from keychain"

# Show live data in the dashboard (read-only — does not enable trading gates)
# DB discovery is automatic: dashboard.py globs auramaur-*.db in DATA_DIR.
export AURAMAUR_DASHBOARD_MODE="live"

log "Starting observability stack (Datasette :8001, Streamlit :8501)"
exec "${REPO}/observability/launch.sh" --both
