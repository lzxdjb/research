#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_user_sim_reflect_lpo}"
export ADV_ESTIMATOR="${ADV_ESTIMATOR:-lpo}"
export POLICY_LOSS_MODE="${POLICY_LOSS_MODE:-vanilla}"

# LPO target settings.
# forward: bounded first-order forward-KL coefficient K * (w* - uniform).
# reverse: reverse-KL on-policy coefficient, close to GRPO with tau scaling.
export LPO_PROJECTION="${LPO_PROJECTION:-forward}"
export LPO_TAU="${LPO_TAU:-1.0}"

exec bash "$SCRIPT_DIR/reflect_demo.sh" "$@"
