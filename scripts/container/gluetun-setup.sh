#!/usr/bin/env bash
#
# gluetun-setup.sh - One-time setup for gluetun VPN container with PIA
#
# Installs Podman (if needed), creates a rootful VM named 'gluetun-vm',
# and deploys the qmcgaw/gluetun:v3 container with Private Internet Access.
# Exposes an HTTP proxy on localhost:8888 that Auramaur's Polymarket client
# uses via HTTPS_PROXY to route CLOB API traffic through a non-US IP.
#
# Prerequisites:
#   - Homebrew installed
#   - PIA credentials in the auramaur macOS keychain:
#       Service: PIA_OPENVPN_USER     Account: auramaur
#       Service: PIA_OPENVPN_PASSWORD  Account: auramaur
#       Keychain: ~/Library/Keychains/auramaur.keychain-db
#
# Usage: ./gluetun-setup.sh
#
# Author: Andrew Rich <andrew.rich@gmail.com>
# Created: 2026-05-22

set -uo pipefail

# ---------------------------------------------------------------------------
# Logging (defined early — used by all sections including Homebrew init)
# ---------------------------------------------------------------------------

log() {
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s [gluetun-setup] %s\n' "${ts}" "$1"
}

die() {
  log "FATAL: $1"
  exit 1
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MACHINE_NAME="gluetun-vm"
CONTAINER_NAME="gluetun-vpn"
CONTAINER_IMAGE="qmcgaw/gluetun:v3"
PROXY_PORT=8888
KEYCHAIN_ACCOUNT="auramaur"
KEYCHAIN_PATH="${HOME}/Library/Keychains/auramaur.keychain-db"
HEALTH_TIMEOUT=120

# ---------------------------------------------------------------------------
# Homebrew environment
# ---------------------------------------------------------------------------

HOMEBREW_PREFIX="/opt/homebrew" # Apple Silicon
if [[ -x "${HOMEBREW_PREFIX}/bin/brew" ]]; then
  brew_env=$("${HOMEBREW_PREFIX}/bin/brew" shellenv)
  eval "${brew_env}"
elif command -v brew >/dev/null 2>&1; then
  true # Homebrew already in PATH
else
  die "Homebrew not found. Install from https://brew.sh before running this script."
fi

# ---------------------------------------------------------------------------
# Section 1: Install Podman
# ---------------------------------------------------------------------------

log "--- Section 1: Podman installation ---"

if ! command -v podman >/dev/null 2>&1; then
  log "Podman not found. Installing via Homebrew..."
  brew install podman || die "brew install podman failed"
  log "Podman installed"
else
  log "Podman already installed"
fi

PODMAN_VERSION="$(podman --version | awk '{print $3}')"
log "Podman version: ${PODMAN_VERSION}"

# ---------------------------------------------------------------------------
# Section 2: Podman machine setup
# ---------------------------------------------------------------------------

log "--- Section 2: Podman machine setup ---"

MACHINE_EXISTS=false
if podman machine inspect "${MACHINE_NAME}" >/dev/null 2>&1; then
  MACHINE_EXISTS=true
fi

if [[ "${MACHINE_EXISTS}" == "false" ]]; then
  log "Initializing Podman machine '${MACHINE_NAME}' (rootful, 1 CPU, 1GB RAM, 10GB disk)..."
  podman machine init \
    --rootful \
    --cpus 1 \
    --memory 1024 \
    --disk-size 10 \
    "${MACHINE_NAME}" || die "podman machine init failed"
  log "Machine initialized"
else
  log "Machine '${MACHINE_NAME}' already exists -- skipping init"
fi

# Set as default connection (idempotent, client-side config)
podman system connection default "${MACHINE_NAME}" 2>/dev/null || true
log "Default Podman connection set to ${MACHINE_NAME}"

# Start machine if not running
MACHINE_STATE="$(podman machine inspect "${MACHINE_NAME}" \
  --format '{{.State}}' 2>/dev/null || echo "unknown")"

if [[ "${MACHINE_STATE}" != "running" ]]; then
  log "Machine state: ${MACHINE_STATE}. Starting..."
  podman machine start "${MACHINE_NAME}" || die "podman machine start failed"
  log "Machine started"
else
  log "Machine already running"
fi

# Wait for podman socket to be ready
log "Waiting for Podman socket..."
socket_ready=false
for _ in {1..30}; do
  if podman info >/dev/null 2>&1; then
    socket_ready=true
    break
  fi
  sleep 1
done

if [[ "${socket_ready}" == "false" ]]; then
  die "Podman socket did not become ready within 30s"
fi
log "Podman socket ready"

# ---------------------------------------------------------------------------
# Section 3: Validate PIA credentials in keychain
# ---------------------------------------------------------------------------

log "--- Section 3: Validate PIA credentials ---"

if [[ ! -f "${KEYCHAIN_PATH}" ]]; then
  die "Keychain not found at ${KEYCHAIN_PATH}. Create it with scripts/bootstrap-keychain.sh first."
fi

PIA_USER="$(security find-generic-password \
  -s "PIA_OPENVPN_USER" \
  -a "${KEYCHAIN_ACCOUNT}" \
  -w "${KEYCHAIN_PATH}" 2>/dev/null || true)"

PIA_PASS="$(security find-generic-password \
  -s "PIA_OPENVPN_PASSWORD" \
  -a "${KEYCHAIN_ACCOUNT}" \
  -w "${KEYCHAIN_PATH}" 2>/dev/null || true)"

if [[ -z "${PIA_USER}" ]]; then
  die "PIA_OPENVPN_USER not found in keychain (service: PIA_OPENVPN_USER, account: ${KEYCHAIN_ACCOUNT}, keychain: ${KEYCHAIN_PATH})"
fi

if [[ -z "${PIA_PASS}" ]]; then
  die "PIA_OPENVPN_PASSWORD not found in keychain (service: PIA_OPENVPN_PASSWORD, account: ${KEYCHAIN_ACCOUNT}, keychain: ${KEYCHAIN_PATH})"
fi

log "PIA credentials loaded from keychain"

# ---------------------------------------------------------------------------
# Section 4: Pull container image
# ---------------------------------------------------------------------------

log "--- Section 4: Pull container image ---"

log "Pulling ${CONTAINER_IMAGE}..."
podman pull "${CONTAINER_IMAGE}" || die "podman pull ${CONTAINER_IMAGE} failed"
log "Image pulled"

# ---------------------------------------------------------------------------
# Section 5: Run the gluetun container
# ---------------------------------------------------------------------------

log "--- Section 5: Deploy gluetun container ---"

# Remove existing container if present (idempotent)
if podman container exists "${CONTAINER_NAME}" 2>/dev/null; then
  log "Removing existing container '${CONTAINER_NAME}'..."
  podman rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

log "Creating container '${CONTAINER_NAME}'..."
# --privileged required: Podman macOS VMs don't expose /dev/net/tun to
# unprivileged containers even with --cap-add NET_ADMIN --device flag.
# gluetun exits with "open /dev/net/tun: permission denied" without it.
podman run -d \
  --name "${CONTAINER_NAME}" \
  --privileged \
  -p "${PROXY_PORT}:${PROXY_PORT}" \
  --restart unless-stopped \
  --health-cmd "wget -qO- --timeout=5 https://ipinfo.io/ip || exit 1" \
  --health-interval 60s \
  --health-start-period 120s \
  --health-retries 3 \
  -e VPN_SERVICE_PROVIDER="private internet access" \
  -e OPENVPN_USER="${PIA_USER}" \
  -e OPENVPN_PASSWORD="${PIA_PASS}" \
  -e SERVER_REGIONS="Netherlands" \
  -e HTTPPROXY="on" \
  -e HTTPPROXY_STEALTH="on" \
  -e HTTPPROXY_LOG="off" \
  -e TZ="America/Los_Angeles" \
  "${CONTAINER_IMAGE}" || die "podman run failed"

# Clear credentials from memory
PIA_USER=""
PIA_PASS=""

log "Container '${CONTAINER_NAME}' created"

# ---------------------------------------------------------------------------
# Section 6: Wait for container to become healthy
# ---------------------------------------------------------------------------

log "--- Section 6: Health check (timeout ${HEALTH_TIMEOUT}s) ---"

elapsed=0
healthy=false
while [[ ${elapsed} -lt ${HEALTH_TIMEOUT} ]]; do
  health_status="$(podman inspect "${CONTAINER_NAME}" \
    --format '{{.State.Health.Status}}' 2>/dev/null || echo "unknown")"

  if [[ "${health_status}" == "healthy" ]]; then
    healthy=true
    break
  fi

  # Check if container died
  running="$(podman inspect "${CONTAINER_NAME}" \
    --format '{{.State.Running}}' 2>/dev/null || echo "false")"
  if [[ "${running}" != "true" ]]; then
    log "Container is not running. Last 20 log lines:"
    podman logs --tail 20 "${CONTAINER_NAME}" 2>&1 || true
    die "Container exited unexpectedly"
  fi

  if [[ $((elapsed % 15)) -eq 0 ]]; then
    log "Waiting for healthy status... (${elapsed}/${HEALTH_TIMEOUT}s, current: ${health_status})"
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

if [[ "${healthy}" == "false" ]]; then
  log "Container did not become healthy within ${HEALTH_TIMEOUT}s. Last 20 log lines:"
  podman logs --tail 20 "${CONTAINER_NAME}" 2>&1 || true
  die "Health check timeout"
fi

log "Container is healthy (took ~${elapsed}s)"

# ---------------------------------------------------------------------------
# Section 7: Verify proxy
# ---------------------------------------------------------------------------

log "--- Section 7: Verify proxy ---"

EXIT_IP="$(curl --proxy "http://localhost:${PROXY_PORT}" \
  --max-time 15 -sf https://ipinfo.io 2>/dev/null || true)"

if [[ -z "${EXIT_IP}" ]]; then
  log "WARNING: Could not verify proxy. The container is healthy but curl through the proxy returned nothing."
  log "Try manually: curl --proxy http://localhost:${PROXY_PORT} https://ipinfo.io"
else
  log "Proxy verification successful. Exit IP info:"
  log "${EXIT_IP}"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

log "=== gluetun-setup complete ==="
log ""
log "The HTTP proxy is available at: http://localhost:${PROXY_PORT}"
log "To use with Auramaur, set: HTTPS_PROXY=http://localhost:${PROXY_PORT}"
log ""
log "Useful commands:"
log "  podman logs ${CONTAINER_NAME}                    # view container logs"
log "  podman inspect ${CONTAINER_NAME} --format '{{.State.Health.Status}}'  # health"
log "  curl --proxy http://localhost:${PROXY_PORT} https://ipinfo.io         # verify exit IP"
log "  podman stop ${CONTAINER_NAME}                    # stop VPN"
log "  podman start ${CONTAINER_NAME}                   # restart VPN"
