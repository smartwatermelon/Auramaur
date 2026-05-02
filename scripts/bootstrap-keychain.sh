#!/usr/bin/env bash
# Bootstrap the auramaur keychain with the 1Password service-account token
# so LaunchAgents can authenticate `op run` without an interactive shell.
#
# Why this exists: LaunchAgents run in a Background session with a bare
# environment — no shell profile, no OP_SERVICE_ACCOUNT_TOKEN. This script
# stores the token in a dedicated keychain that never auto-locks, and the
# wrapper reads it at runtime.
#
# Follows the ralph-burndown pattern: dedicated keychain with empty password,
# no auto-lock, FileVault provides encryption at rest.
#
# Usage:
#   echo "$OP_SERVICE_ACCOUNT_TOKEN" | bash scripts/bootstrap-keychain.sh
#   OR
#   bash scripts/bootstrap-keychain.sh    # prompts (no echo)

set -euo pipefail

unset CDPATH

readonly KC_NAME="auramaur"
readonly KC_PATH="${HOME}/Library/Keychains/${KC_NAME}.keychain-db"
readonly KC_PASSWORD=""
readonly SERVICE="op-service-account-token"
readonly ACCOUNT="auramaur"

echo "==> Preflight"
if ! command -v security >/dev/null 2>&1; then
  echo "Error: security command not found (macOS only)." >&2
  exit 1
fi

if [[ ! -f ${KC_PATH} ]]; then
  echo "==> Creating dedicated keychain at ${KC_PATH}"
  echo "    (empty password, no auto-lock — FileVault provides encryption at rest)"
  security create-keychain -p "${KC_PASSWORD}" "${KC_PATH}"
  security set-keychain-settings -u "${KC_PATH}"
else
  echo "==> Keychain already exists at ${KC_PATH}"
fi

echo "==> Unlocking keychain"
security unlock-keychain -p "${KC_PASSWORD}" "${KC_PATH}"

if [[ ! -t 0 ]]; then
  TOKEN=$(cat | tr -d '\r\n')
else
  echo ""
  echo "Paste OP_SERVICE_ACCOUNT_TOKEN (input hidden):"
  read -r -s TOKEN
  echo ""
  TOKEN=$(echo -n "${TOKEN}" | tr -d '\r\n')
fi

if [[ -z ${TOKEN} ]]; then
  echo "Error: no token provided." >&2
  exit 1
fi

echo "  (got ${#TOKEN}-char token)"

echo "==> Storing/updating 1Password service-account token"
security add-generic-password \
  -a "${ACCOUNT}" \
  -s "${SERVICE}" \
  -w "${TOKEN}" \
  -U \
  "${KC_PATH}"

echo "==> Verify readback"
RETRIEVED=$(security find-generic-password -a "${ACCOUNT}" -s "${SERVICE}" -w "${KC_PATH}")
if [[ ${RETRIEVED} != "${TOKEN}" ]]; then
  echo "Error: stored token does not match what was provided." >&2
  exit 1
fi
echo "  OK: stored ${#RETRIEVED}-char token, matches input"

echo ""
echo "Bootstrap complete."
echo "  Keychain: ${KC_PATH}"
echo "  Item:     ${ACCOUNT} @ ${SERVICE}"
echo ""
echo "Next: run install-launchd.sh to reload the LaunchAgents."
