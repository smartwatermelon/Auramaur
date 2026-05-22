#!/usr/bin/env bash
# Auramaur launchd wrapper — keeps the gluetun VPN container running.
#
# Invoked by the com.auramaur.gluetun LaunchAgent at login. The script
# starts the gluetun container inside a Podman machine, then enters a
# supervision loop that restarts the machine or container if either stops.
#
# This script DOES NOT EXIT under normal operation. If it exits non-zero,
# launchd's KeepAlive restarts it.
#
# PIA credentials are loaded from the auramaur macOS keychain (populated
# by bootstrap-keychain.sh). `op` is NOT invoked at runtime.

set -uo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

KEYCHAIN="${HOME}/Library/Keychains/auramaur.keychain-db"
KC_ACCOUNT="auramaur"
SUPERVISE_INTERVAL="${SUPERVISE_INTERVAL:-300}"

# PIA credentials — populated by load_pia_creds()
PIA_USER=""
PIA_PASS=""

log_ts() {
  local _ts
  _ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '[%s] [auramaur-gluetun] %s\n' "${_ts}" "$*"
}

unlock_keychain() {
  if security unlock-keychain -p '' "${KEYCHAIN}" 2>/dev/null; then
    return 0
  fi
  log_ts "ERROR: could not unlock keychain at ${KEYCHAIN} — run bootstrap-keychain.sh"
  exit 1
}

load_pia_creds() {
  PIA_USER=$(security find-generic-password -a "${KC_ACCOUNT}" -s "PIA_OPENVPN_USER" -w "${KEYCHAIN}" 2>/dev/null) || PIA_USER=""
  if [[ -z "${PIA_USER}" ]]; then
    log_ts "ERROR: PIA_OPENVPN_USER not found in keychain — run bootstrap-keychain.sh"
    exit 1
  fi

  PIA_PASS=$(security find-generic-password -a "${KC_ACCOUNT}" -s "PIA_OPENVPN_PASSWORD" -w "${KEYCHAIN}" 2>/dev/null) || PIA_PASS=""
  if [[ -z "${PIA_PASS}" ]]; then
    log_ts "ERROR: PIA_OPENVPN_PASSWORD not found in keychain — run bootstrap-keychain.sh"
    exit 1
  fi

  log_ts "PIA credentials loaded from keychain"
}

ensure_machine() {
  local state socket elapsed

  state=$(podman machine inspect gluetun-vm --format '{{.State}}' 2>/dev/null) || state=""

  if [[ "${state}" == "running" ]]; then
    return 0
  fi

  log_ts "Podman machine gluetun-vm is not running (state=${state:-missing}), starting..."
  if ! podman machine start gluetun-vm 2>&1; then
    log_ts "ERROR: failed to start gluetun-vm"
    return 1
  fi

  # Wait up to 30s for the machine socket to appear
  elapsed=0
  socket=$(podman machine inspect gluetun-vm --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null) || socket=""
  while [[ ! -S "${socket}" ]] && [[ "${elapsed}" -lt 30 ]]; do
    sleep 1
    elapsed=$((elapsed + 1))
    socket=$(podman machine inspect gluetun-vm --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null) || socket=""
  done

  if [[ ! -S "${socket}" ]]; then
    log_ts "ERROR: gluetun-vm socket not available after 30s (expected at ${socket:-unknown})"
    return 1
  fi

  log_ts "Podman machine gluetun-vm is running (waited ${elapsed}s for socket)"
  return 0
}

ensure_container() {
  local state

  state=$(podman container inspect gluetun-vpn --format '{{.State.Status}}' 2>/dev/null) || state=""

  if [[ "${state}" == "running" ]]; then
    return 0
  fi

  # Remove stale container if it exists in a non-running state
  if [[ -n "${state}" ]]; then
    log_ts "Container gluetun-vpn exists in state=${state}, removing..."
    podman rm -f gluetun-vpn 2>/dev/null || true
  fi

  log_ts "Creating gluetun-vpn container..."
  local run_output
  if ! run_output=$(podman run -d \
    --name gluetun-vpn \
    --cap-add NET_ADMIN \
    --device /dev/net/tun:/dev/net/tun \
    -p 8888:8888 \
    -e VPN_SERVICE_PROVIDER="private internet access" \
    -e OPENVPN_USER="${PIA_USER}" \
    -e OPENVPN_PASSWORD="${PIA_PASS}" \
    -e SERVER_REGIONS=Panama \
    -e HTTPPROXY=on \
    -e HTTPPROXY_STEALTH=on \
    -e HTTPPROXY_LOG=off \
    -e TZ=America/Los_Angeles \
    --restart unless-stopped \
    --health-cmd "wget -qO- --timeout=5 https://ipinfo.io/ip || exit 1" \
    --health-interval 60s \
    --health-start-period 120s \
    --health-retries 3 \
    qmcgaw/gluetun:v3 2>&1); then
    log_ts "ERROR: failed to create gluetun-vpn container: ${run_output}"
    return 1
  fi

  log_ts "Container gluetun-vpn started successfully"
  return 0
}

# --- Startup ---

unlock_keychain
load_pia_creds

if ! ensure_machine; then
  log_ts "FATAL: could not start Podman machine on initial startup"
  exit 1
fi

if ! ensure_container; then
  log_ts "FATAL: could not start gluetun container on initial startup"
  exit 1
fi

log_ts "Supervision loop starting (interval=${SUPERVISE_INTERVAL}s)"

# --- Supervision loop (runs forever) ---

while true; do
  sleep "${SUPERVISE_INTERVAL}"

  if ! ensure_machine; then
    log_ts "WARNING: Podman machine recovery failed, will retry next cycle"
    continue
  fi

  if ! ensure_container; then
    log_ts "WARNING: container recovery failed, will retry next cycle"
  fi
done
