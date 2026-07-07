#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_user_sim_reflect_bos_grpo}"
export ADV_ESTIMATOR=batch_opt_subspace_grpo
export POLICY_LOSS_MODE="${POLICY_LOSS_MODE:-vanilla}"

# Batch-Optimal Subspace GRPO hyperparameters.
export BOS_GRPO_K="${BOS_GRPO_K:-4}"
export BOS_GRPO_LAMBDA="${BOS_GRPO_LAMBDA:-1.0}"
export BOS_GRPO_WEIGHT_FLOOR="${BOS_GRPO_WEIGHT_FLOOR:-0.1}"
export BOS_GRPO_WEIGHT_POWER="${BOS_GRPO_WEIGHT_POWER:-1.0}"
export BOS_GRPO_MIX_WITH_VANILLA="${BOS_GRPO_MIX_WITH_VANILLA:-0.0}"
export BOS_GRPO_POSITIVE_EIGS_ONLY="${BOS_GRPO_POSITIVE_EIGS_ONLY:-True}"
export BOS_GRPO_FALLBACK_TO_VANILLA="${BOS_GRPO_FALLBACK_TO_VANILLA:-True}"
export BOS_GRPO_INCLUDE_FEATURE_STD="${BOS_GRPO_INCLUDE_FEATURE_STD:-True}"
export BOS_GRPO_NORMALIZE_FEATURES="${BOS_GRPO_NORMALIZE_FEATURES:-True}"
export BOS_GRPO_EPS="${BOS_GRPO_EPS:-1e-6}"

export EXTRA_BOS_GRPO_OVERRIDES=1

"$SCRIPT_DIR/reflect_demo.sh" "$@"
