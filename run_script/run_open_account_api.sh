#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPEN_ACCOUNT_DIR="$PROJECT_DIR/open-account"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$OPEN_ACCOUNT_DIR"

if ! "$PYTHON_BIN" -c "import requests" >/dev/null 2>&1; then
  echo "Missing Python dependency: requests" >&2
  echo "Install it with: $PYTHON_BIN -m pip install requests" >&2
  exit 1
fi

exec "$PYTHON_BIN" scripts/test_api.py "$@"
