#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPEN_ACCOUNT_DIR="$PROJECT_DIR/open-account"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$OPEN_ACCOUNT_DIR"
exec "$PYTHON_BIN" scripts/test_parallel_trajectories.py "$@"
