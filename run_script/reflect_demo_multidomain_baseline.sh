#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-multidomain_grpo_baseline}"
export ADV_ESTIMATOR=grpo
export POLICY_LOSS_MODE="${POLICY_LOSS_MODE:-vanilla}"

"$SCRIPT_DIR/reflect_demo_multidomain.sh" "$@"
