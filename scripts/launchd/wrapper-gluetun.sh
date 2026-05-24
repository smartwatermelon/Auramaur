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

# --- Alert email via msmtp ---
# Same pattern as wrapper-bot.sh — uses msmtp directly since this
# script runs outside the Python venv.
MAIL_TO="${AURAMAUR_ALERT_TO:-andrew.rich@gmail.com}"
MAIL_FROM="${AURAMAUR_ALERT_FROM:-andrew.rich@gmail.com}"

send_alert() {
  # Send an email alert via msmtp. Args: $1=subject, $2=body.
  # Non-fatal: if msmtp is missing or fails, log a warning and continue.
  local subject="$1" body="$2"
  if ! command -v msmtp >/dev/null 2>&1; then
    log_ts "WARN: msmtp not found — alert not sent: ${subject}"
    return 1
  fi
  printf 'From: %s\nTo: %s\nSubject: %s\n\n%s\n' \
    "${MAIL_FROM}" "${MAIL_TO}" "${subject}" "${body}" \
    | msmtp -a gmail "${MAIL_TO}" 2>/dev/null
  local rc=$?
  if [[ ${rc} -eq 0 ]]; then
    log_ts "Alert sent: ${subject}"
  else
    log_ts "WARN: msmtp failed (rc=${rc}) — alert not sent: ${subject}"
  fi
  return ${rc}
}

check_proxy_health() {
  # Probe the Gluetun HTTP proxy by requesting an external endpoint through it.
  # If the probe fails, the container's tunnel is broken even though the
  # container itself is "running". Restart the container and send an alert.
  #
  # Called from the supervision loop AFTER ensure_container() confirms the
  # container is in "running" state. This catches the failure mode where
  # OpenVPN routes go stale or DNS fails inside the container.
  local proxy_ip restart_ts new_ip elapsed

  if proxy_ip=$(curl -sf --proxy http://localhost:8888 --max-time 10 https://ipinfo.io/ip 2>/dev/null); then
    log_ts "Proxy health OK (exit IP: ${proxy_ip})"
    return 0
  fi

  log_ts "ERROR: proxy health check failed — restarting gluetun-vpn container"
  podman restart gluetun-vpn 2>&1 || true

  # Wait up to 60s for the container to reach "healthy" status.
  # Podman's --health-start-period is 120s, but the proxy itself typically
  # comes up much faster. Poll every 5s to detect recovery promptly.
  elapsed=0
  while [[ "${elapsed}" -lt 60 ]]; do
    sleep 5
    elapsed=$((elapsed + 5))
    local health
    health=$(podman inspect gluetun-vpn --format '{{.State.Health.Status}}' 2>/dev/null) || health=""
    if [[ "${health}" == "healthy" ]]; then
      break
    fi
  done

  # Try to get the new exit IP for the alert
  new_ip=$(curl -sf --proxy http://localhost:8888 --max-time 10 https://ipinfo.io/ip 2>/dev/null) || new_ip="unknown — healthcheck still pending"
  restart_ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

  send_alert \
    "[auramaur] gluetun: proxy broken, container restarted" \
    "The Gluetun VPN proxy at localhost:8888 was unreachable.
Container restarted at ${restart_ts}.
New exit IP: ${new_ip}
Log: ~/Library/Logs/auramaur/gluetun.log"

  return 1
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
  # --privileged required: Podman macOS VMs don't expose /dev/net/tun to
  # unprivileged containers even with --cap-add NET_ADMIN --device flag.
  # gluetun exits with "open /dev/net/tun: permission denied" without it.
  local run_output
  if ! run_output=$(podman run -d \
    --name gluetun-vpn \
    --privileged \
    -p 8888:8888 \
    -e VPN_SERVICE_PROVIDER="private internet access" \
    -e OPENVPN_USER="${PIA_USER}" \
    -e OPENVPN_PASSWORD="${PIA_PASS}" \
    -e SERVER_REGIONS=Netherlands \
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
    continue
  fi

  # Probe the proxy — container is "running" but the tunnel may be broken.
  # check_proxy_health() restarts and alerts if the probe fails.
  check_proxy_health
done
