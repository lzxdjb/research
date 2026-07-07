#!/usr/bin/env bash
set -xeuo pipefail

# Launch from /mnt/code while keeping generated data, logs, validation output,
# and checkpoints under the persistent workspace.
export DIGITAL_ONBOARDING_STORAGE_ROOT="${DIGITAL_ONBOARDING_STORAGE_ROOT:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/run_digital_onboarding_service_formal_train.sh" "$@"
