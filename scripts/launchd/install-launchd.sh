#!/usr/bin/env bash
# Install or reinstall Auramaur LaunchAgents on the current user account.
#
# Usage: bash install-launchd.sh [--unload-only]
#
# --unload-only: unload all agents and exit without re-loading (clean shutdown).
#
# Idempotent: safe to re-run. Unloads any currently loaded agents first, then
# symlinks plists from the repo into ~/Library/LaunchAgents/, creates the log
# directory, and loads all three agents.
#
# Requires: launchctl, the repo on the external volume (for wrappers), and
# the auramaur keychain bootstrapped (for bot agents; observability needs neither).

set -euo pipefail

REPO="/Volumes/extra-vieille/Workspaces/Auramaur"
PLIST_SRC="${REPO}/scripts/launchagent"
LA_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs/auramaur"
EXPECTED_HOME="/Users/andrewrich"
KEYCHAIN="${HOME}/Library/Keychains/auramaur.keychain-db"

AGENTS=(
  com.auramaur.kalshi
  com.auramaur.polymarket
  com.auramaur.observability
)

UNLOAD_ONLY=false
if [[ "${1:-}" == "--unload-only" ]]; then
  UNLOAD_ONLY=true
fi

log() {
  local _ts
  _ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '[%s] [install-launchd] %s\n' "${_ts}" "$*"
}

if [[ "${HOME}" != "${EXPECTED_HOME}" ]]; then
  log "ERROR: HOME=${HOME} but plists hardcode ${EXPECTED_HOME} — re-generate plists for this account"
  exit 1
fi

if [[ ! -d "${REPO}" ]]; then
  log "ERROR: repo not found at ${REPO} — external volume not mounted?"
  exit 1
fi

# Unload any currently loaded agents (ignore errors if not loaded).
for label in "${AGENTS[@]}"; do
  if launchctl list "${label}" >/dev/null 2>&1; then
    log "Unloading ${label}"
    launchctl unload "${LA_DIR}/${label}.plist" 2>/dev/null || true
  fi
done

if [[ "${UNLOAD_ONLY}" == true ]]; then
  log "Unload-only mode — exiting without re-loading"
  exit 0
fi

# Validate keychain: bot agents need secrets from keychain.
# Observability does not — warn but don't block if keychain is missing.
if [[ ! -f "${KEYCHAIN}" ]]; then
  log "WARN: keychain not found at ${KEYCHAIN} — bot agents will fail"
  log "      Run: bash ${REPO}/scripts/bootstrap-keychain.sh"
else
  security unlock-keychain -p '' "${KEYCHAIN}" 2>/dev/null || true
  if ! security find-generic-password -a auramaur -s KALSHI_API_KEY -w "${KEYCHAIN}" >/dev/null 2>&1; then
    log "WARN: secrets not found in keychain — bot agents will fail"
    log "      Run: bash ${REPO}/scripts/bootstrap-keychain.sh"
  else
    log "Keychain validated: secrets present"
  fi
fi

# Create log directory.
mkdir -p "${LOG_DIR}"
log "Log directory: ${LOG_DIR}"

# Symlink plists into ~/Library/LaunchAgents/.
mkdir -p "${LA_DIR}"
for label in "${AGENTS[@]}"; do
  src="${PLIST_SRC}/${label}.plist"
  dst="${LA_DIR}/${label}.plist"
  if [[ ! -f "${src}" ]]; then
    log "ERROR: plist not found: ${src}"
    exit 1
  fi
  if [[ -L "${dst}" ]]; then
    rm "${dst}"
  elif [[ -f "${dst}" ]]; then
    log "WARN: replacing non-symlink plist at ${dst}"
    rm "${dst}"
  fi
  ln -s "${src}" "${dst}"
  log "Linked ${dst} → ${src}"
done

# Load all agents.
for label in "${AGENTS[@]}"; do
  plist="${LA_DIR}/${label}.plist"
  log "Loading ${label}"
  launchctl load "${plist}"
done

log "Done. Check status with: launchctl list | grep auramaur"
log "Logs: ${LOG_DIR}/"
