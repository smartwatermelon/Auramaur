#!/usr/bin/env bash
# Bootstrap the auramaur keychain with all secrets from 1Password.
#
# Reads each op:// reference from .claude/secrets.op using `op read`,
# stores the resolved values in a dedicated macOS keychain. The
# LaunchAgent wrappers read from this keychain at runtime — `op` is
# NOT invoked at runtime because it hangs under launchd Background
# sessions (macOS Gatekeeper blocks its open() syscall when there is
# no controlling terminal).
#
# Follows the ralph-burndown pattern: dedicated keychain with empty
# password, no auto-lock, FileVault provides encryption at rest.
#
# Usage (interactive, requires op to be signed in):
#   bash scripts/bootstrap-keychain.sh
#
# Re-run any time secrets change in 1Password.

set -euo pipefail

unset CDPATH

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly KC_NAME="auramaur"
readonly KC_PATH="${HOME}/Library/Keychains/${KC_NAME}.keychain-db"
readonly KC_PASSWORD=""
readonly SECRETS="${REPO}/.claude/secrets.op"

echo "==> Preflight"
if ! command -v op >/dev/null 2>&1; then
  echo "Error: 1Password CLI (op) not on PATH." >&2
  exit 1
fi

if ! op whoami >/dev/null 2>&1; then
  echo "Error: op is not signed in. Run 'op signin' or set OP_SERVICE_ACCOUNT_TOKEN." >&2
  exit 1
fi

if [[ ! -f ${SECRETS} ]]; then
  echo "Error: secrets file not found at ${SECRETS}" >&2
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

echo "==> Resolving secrets from 1Password and storing in keychain"
count=0
while IFS='=' read -r key ref; do
  [[ -z "${key}" || "${key}" == \#* ]] && continue
  ref="${ref#\"}"
  ref="${ref%\"}"
  [[ "${ref}" != op://* ]] && continue

  printf "  %-30s → " "${key}"
  val=$(op read "${ref}" 2>&1) || {
    echo "FAILED: ${val}"
    exit 1
  }
  printf "%d chars" "${#val}"

  security add-generic-password \
    -a "${KC_NAME}" \
    -s "${key}" \
    -w "${val}" \
    -U \
    "${KC_PATH}"

  retrieved=$(security find-generic-password -a "${KC_NAME}" -s "${key}" -w "${KC_PATH}")
  if [[ "${retrieved}" != "${val}" ]]; then
    echo " — VERIFY FAILED"
    exit 1
  fi
  echo " — OK"
  count=$((count + 1))
done <"${SECRETS}"

echo ""
echo "Bootstrap complete: ${count} secrets stored."
echo "  Keychain: ${KC_PATH}"
echo ""
echo "Next: run scripts/launchd/install-launchd.sh to reload the LaunchAgents."
