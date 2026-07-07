#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_user_sim_reflect_simple_intentional_grpo}"
export ADV_ESTIMATOR="${ADV_ESTIMATOR:-grpo}"
export POLICY_LOSS_MODE=simple_intentional_grpo
export CALCULATE_SUM_PI_SQUARED="${CALCULATE_SUM_PI_SQUARED:-True}"

export INTENTIONAL_ETA="${INTENTIONAL_ETA:-1.0}"
export INTENTIONAL_CLIP_TARGET="${INTENTIONAL_CLIP_TARGET:-True}"
export INTENTIONAL_REQUIRE_SUM_PI_SQUARED="${INTENTIONAL_REQUIRE_SUM_PI_SQUARED:-True}"

exec bash "$SCRIPT_DIR/reflect_demo_debug.sh" "$@"
