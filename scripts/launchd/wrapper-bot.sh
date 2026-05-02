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
REPO="/Volumes/extra-vieille/Workspaces/Auramaur"
SECRETS="${REPO}/.claude/secrets.op"
UV="/opt/homebrew/bin/uv"
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
  local key ref val
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
  done <"${SECRETS}"
}

# Preflight: external volume must be mounted.
if [[ ! -d "${REPO}" ]]; then
  log "ERROR: repo not found at ${REPO} — external volume not mounted?"
  exit 1
fi

if [[ ! -x "${UV}" ]]; then
  log "ERROR: uv not found or not executable at ${UV}"
  exit 1
fi

if [[ ! -f "${SECRETS}" ]]; then
  log "ERROR: secrets file not found at ${SECRETS}"
  exit 1
fi

unlock_keychain
log "Loading secrets from keychain"
load_secrets
log "Starting bot (exchange=${EXCHANGE})"

exec "${UV}" run --project "${REPO}" \
  auramaur run --agent --exchange "${EXCHANGE}"
