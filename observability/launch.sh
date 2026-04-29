#!/usr/bin/env bash
# Launch Datasette (port 8001) and Streamlit (port 8501) against the live DB.
# Usage: ./observability/launch.sh [--datasette|--streamlit|--both]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DB="${REPO_ROOT}/auramaur.db"
META="${SCRIPT_DIR}/metadata.yml"
DASH="${SCRIPT_DIR}/dashboard.py"

MODE="${1:---both}"

case "$MODE" in
  --datasette | --streamlit | --both) ;;
  *)
    echo "Usage: $0 [--datasette|--streamlit|--both]" >&2
    exit 1
    ;;
esac

if [[ ! -f "$DB" ]]; then
  echo "DB not found: $DB — run the bot at least once first." >&2
  exit 1
fi

PIDS=()
cleanup() { [[ ${#PIDS[@]} -gt 0 ]] && kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup INT TERM EXIT

if [[ "$MODE" == "--datasette" || "$MODE" == "--both" ]]; then
  echo "Starting Datasette → http://localhost:8001"
  uvx datasette "$DB" --metadata "$META" --port 8001 &
  PIDS+=($!)
fi

if [[ "$MODE" == "--streamlit" || "$MODE" == "--both" ]]; then
  echo "Starting Streamlit → http://localhost:8501"
  uvx --with streamlit --with pandas streamlit run "$DASH" \
    --server.port 8501 --server.address 127.0.0.1 --server.headless true &
  PIDS+=($!)
fi

echo ""
echo "Press Ctrl-C to stop."
wait
