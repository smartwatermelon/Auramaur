#!/usr/bin/env bash
# Auramaur launchd wrapper — used by both exchange bot LaunchAgents.
# Usage: wrapper-bot.sh <exchange>   (kalshi | polymarket)
#
# Checks that the external volume is mounted, then execs the bot under
# `op run` so secrets never touch disk.  Launchd's KeepAlive restarts
# us on crash; ThrottleInterval prevents tight loops.
#
# Follow the ralph-burndown pattern: absolute paths throughout, no
# bashisms that require bash 5, log timestamps in UTC ISO-8601.

set -uo pipefail

EXCHANGE="${1:?Usage: wrapper-bot.sh <kalshi|polymarket>}"
REPO="/Volumes/extra-vieille/Workspaces/Auramaur"
SECRETS="${REPO}/.claude/secrets.op"
OP="/opt/homebrew/bin/op"
UV="/opt/homebrew/bin/uv"
KEYCHAIN="${HOME}/Library/Keychains/auramaur.keychain-db"

log() {
  local _ts
  _ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '[%s] [auramaur-%s] %s\n' "${_ts}" "${EXCHANGE}" "$*"
}

unlock_keychain() {
  if security unlock-keychain -p '' "${KEYCHAIN}" 2>/dev/null; then
    return 0
  fi
  log "WARN: could not unlock keychain at ${KEYCHAIN}; op run will fail"
  return 0
}

load_op_token() {
  local token
  token=$(security find-generic-password -a auramaur -s op-service-account-token -w "${KEYCHAIN}" 2>/dev/null) || token=""
  if [[ -z "${token}" ]]; then
    log "ERROR: could not read op-service-account-token from keychain — run bootstrap-keychain.sh"
    exit 1
  fi
  export OP_SERVICE_ACCOUNT_TOKEN="${token}"
}

# Keychain: load OP_SERVICE_ACCOUNT_TOKEN so `op run` can authenticate.
unlock_keychain
load_op_token

# Preflight: external volume must be mounted.
if [[ ! -d "${REPO}" ]]; then
  log "ERROR: repo not found at ${REPO} — external volume not mounted?"
  exit 1
fi

if [[ ! -x "${OP}" ]]; then
  log "ERROR: op not found or not executable at ${OP}"
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

log "Starting bot (exchange=${EXCHANGE})"

exec "${OP}" run --env-file="${SECRETS}" \
  -- \
  "${UV}" run --project "${REPO}" \
  auramaur run --agent --exchange "${EXCHANGE}"
