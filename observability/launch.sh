#!/usr/bin/env bash
# Launch Datasette (port 8001) and Streamlit (port 8501) against the live DB.
# Usage: ./observability/launch.sh [--datasette|--streamlit|--both]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
META="${SCRIPT_DIR}/metadata.yml"
DASH="${SCRIPT_DIR}/dashboard.py"

# Discover all exchange-namespaced DBs for datasette.
# The Streamlit dashboard does its own discovery internally.
DBS=()
DATA_DIR="${HOME}/Library/Application Support/auramaur"
for candidate in "${DATA_DIR}"/auramaur-*.db; do
  [[ -f "$candidate" ]] && DBS+=("$candidate")
done
if [[ ${#DBS[@]} -eq 0 ]]; then
  for candidate in \
    "${REPO_ROOT}/auramaur-polymarket.db" \
    "${REPO_ROOT}/auramaur-kalshi.db" \
    "${REPO_ROOT}/auramaur.db"; do
    [[ -f "$candidate" ]] && DBS+=("$candidate")
  done
fi

MODE="${1:---both}"

case "$MODE" in
  --datasette | --streamlit | --both) ;;
  *)
    echo "Usage: $0 [--datasette|--streamlit|--both]" >&2
    exit 1
    ;;
esac

if [[ ${#DBS[@]} -eq 0 ]]; then
  echo "No DBs found — run the bot at least once first." >&2
  exit 1
fi

PIDS=()
cleanup() { [[ ${#PIDS[@]} -gt 0 ]] && kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup INT TERM EXIT

if [[ "$MODE" == "--datasette" || "$MODE" == "--both" ]]; then
  echo "Starting Datasette → http://localhost:8001 (${#DBS[@]} DBs)"
  uvx datasette "${DBS[@]}" --metadata "$META" --port 8001 &
  PIDS+=($!)
fi

if [[ "$MODE" == "--streamlit" || "$MODE" == "--both" ]]; then
  echo "Starting Streamlit → http://localhost:8501"
  uvx --with streamlit --with pandas --with pyyaml --with pydantic-settings --with cryptography streamlit run "$DASH" \
    --server.port 8501 --server.address 127.0.0.1 --server.headless true &
  PIDS+=($!)
fi

echo ""
echo "Press Ctrl-C to stop."
wait
