#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_user_sim_reflect_lpo_adaptive}"
export ADV_ESTIMATOR="${ADV_ESTIMATOR:-lpo_adaptive}"
export POLICY_LOSS_MODE="${POLICY_LOSS_MODE:-vanilla}"

# Adaptive LPO keeps the original Gibbs target family but selects a = 1/tau
# per prompt by maximizing reward gain times relative effective sample size.
export LPO_PROJECTION="${LPO_PROJECTION:-forward}"
export LPO_ADAPTIVE_GRID_SIZE="${LPO_ADAPTIVE_GRID_SIZE:-32}"
export LPO_ADAPTIVE_MAX_LOGIT_GAP="${LPO_ADAPTIVE_MAX_LOGIT_GAP:-20.0}"

exec bash "$SCRIPT_DIR/reflect_demo.sh" "$@"
