#!/usr/bin/env bash
# Auramaur launchd wrapper — used by both exchange bot LaunchAgents.
# Usage: wrapper-bot.sh <exchange>   (kalshi | polymarket)
#
# Loads secrets from the auramaur macOS keychain (populated by
# bootstrap-keychain.sh) and execs the bot. `op` is NOT invoked at
# runtime — it hangs under launchd Background sessions due to macOS
# Gatekeeper blocking its open() syscall without a controlling terminal.
#
# Follow the ralph-burndown pattern: absolute paths throughout, no
# bashisms that require bash 5, log timestamps in UTC ISO-8601.

set -uo pipefail

EXCHANGE="${1:?Usage: wrapper-bot.sh <kalshi|polymarket>}"
REPO="${HOME}/Developer/Auramaur"
INTERNAL_SECRETS="${HOME}/Library/Application Support/auramaur/.claude/secrets.op"
SECRETS="${INTERNAL_SECRETS}"
if [[ ! -f "${SECRETS}" ]]; then
  SECRETS="${REPO}/.claude/secrets.op"
fi
# Venv on internal disk avoids macOS TCC "Operation not permitted"
# when launchd-spawned Python reads .venv/pyvenv.cfg on external volumes.
VENV_BIN="${HOME}/Library/Application Support/auramaur/.venv/bin"
KEYCHAIN="${HOME}/Library/Keychains/auramaur.keychain-db"
KC_ACCOUNT="auramaur"

log() {
  local _ts
  _ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '[%s] [auramaur-%s] %s\n' "${_ts}" "${EXCHANGE}" "$*"
}

unlock_keychain() {
  if security unlock-keychain -p '' "${KEYCHAIN}" 2>/dev/null; then
    return 0
  fi
  log "ERROR: could not unlock keychain at ${KEYCHAIN} — run bootstrap-keychain.sh"
  exit 1
}

load_secrets() {
  local key ref val secrets_content
  # Read the file with retry — launchd can deliver signals (EINTR)
  # during open() on external volumes when respawning after a kill.
  local _try
  for _try in 1 2 3; do
    secrets_content="$(cat "${SECRETS}" 2>/dev/null)" && break
    log "WARN: could not read ${SECRETS} (attempt ${_try}/3), retrying..."
    sleep 2
  done
  if [[ -z "${secrets_content}" ]]; then
    log "ERROR: could not read ${SECRETS} after 3 attempts"
    exit 1
  fi
  while IFS='=' read -r key ref; do
    [[ -z "${key}" || "${key}" == \#* ]] && continue
    ref="${ref#\"}"
    ref="${ref%\"}"
    [[ "${ref}" != op://* ]] && continue
    val=$(security find-generic-password -a "${KC_ACCOUNT}" -s "${key}" -w "${KEYCHAIN}" 2>/dev/null) || val=""
    if [[ -z "${val}" ]]; then
      log "ERROR: secret ${key} not found in keychain — run bootstrap-keychain.sh"
      exit 1
    fi
    export "${key}=${val}"
  done <<<"${secrets_content}"
}

if [[ ! -d "${REPO}" ]]; then
  log "ERROR: repo not found at ${REPO}"
  exit 1
fi

if [[ ! -x "${VENV_BIN}/auramaur" ]]; then
  log "ERROR: launchd venv not found at ${VENV_BIN}/auramaur"
  log "  One-time setup (creates venv on internal disk for TCC compatibility):"
  log "    uv venv '${HOME}/Library/Application Support/auramaur/.venv'"
  log "    uv pip install --python '${VENV_BIN}/python' '${REPO}'"
  exit 1
fi

if [[ ! -f "${SECRETS}" ]]; then
  log "ERROR: secrets file not found at ${SECRETS}"
  exit 1
fi

unlock_keychain
log "Loading secrets from keychain"
# Ignore SIGTERM during secret loading — launchd's process management
# can deliver signals that interrupt the file read (EINTR on the
# shell redirection), causing secrets to silently not load.
trap '' TERM
load_secrets
trap - TERM

# Per-exchange live-trading gate. AURAMAUR_LIVE is the first of three
# gates required for real orders (see config/settings.py::is_live).
# Exchanges not listed here default to paper mode.
case "${EXCHANGE}" in
  kalshi) export AURAMAUR_LIVE=true ;;
  *) export AURAMAUR_LIVE=false ;;
esac

# For polymarket, verify the VPN proxy is reachable before starting.
# On failure, exit non-zero so launchd restarts us after ThrottleInterval.
if [[ "${EXCHANGE}" == "polymarket" ]]; then
  log "Checking gluetun proxy at localhost:8888..."
  for _attempt in $(seq 1 12); do
    if curl -sf --proxy http://localhost:8888 --max-time 5 https://ipinfo.io/ip >/dev/null 2>&1; then
      proxy_ip=$(curl -sf --proxy http://localhost:8888 --max-time 5 https://ipinfo.io/ip)
      log "Gluetun proxy ready (exit IP: ${proxy_ip})"
      break
    fi
    if [[ "${_attempt}" -eq 12 ]]; then
      log "ERROR: gluetun proxy not reachable after 60s — aborting"
      exit 1
    fi
    sleep 5
  done
fi

log "Starting bot (exchange=${EXCHANGE}, live=${AURAMAUR_LIVE:-false})"

export PYTHONUNBUFFERED=1
exec "${VENV_BIN}/auramaur" run --agent --exchange "${EXCHANGE}"
