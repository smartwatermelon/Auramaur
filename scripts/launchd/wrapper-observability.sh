#!/usr/bin/env bash
# Auramaur launchd wrapper — Datasette + Streamlit observability dashboard.
# No secrets required; dashboard only reads is_live and paper_initial_balance
# from settings (harmless defaults if .env is absent).

set -uo pipefail

REPO="${HOME}/Developer/Auramaur"
DATA_DIR="${HOME}/Library/Application Support/auramaur"

log() {
  local _ts
  _ts=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  printf '[%s] [auramaur-observability] %s\n' "${_ts}" "$*"
}

if [[ ! -d "${REPO}" ]]; then
  log "ERROR: repo not found at ${REPO}"
  exit 1
fi

# Point at the exchange-namespaced DB on internal disk
export AURAMAUR_DB="${DATA_DIR}/auramaur-polymarket.db"

log "Starting observability stack (Datasette :8001, Streamlit :8501)"
exec "${REPO}/observability/launch.sh" --both
