#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_user_sim_reflect_mse_gate}"
export ADV_ESTIMATOR=mse_gate

# Enables the best available per-token Fisher proxy when the active actor backend
# supports it. The estimator falls back to old_log_probs/response length otherwise.
export CALCULATE_SUM_PI_SQUARED="${CALCULATE_SUM_PI_SQUARED:-True}"

# exec bash "$SCRIPT_DIR/reflect_demo.sh" "$@"
exec bash "$SCRIPT_DIR/reflect_demo_debug.sh" "$@"
