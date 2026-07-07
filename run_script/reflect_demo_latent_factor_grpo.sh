#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_user_sim_reflect_latent_factor_grpo}"
export ADV_ESTIMATOR=latent_factor_grpo
export POLICY_LOSS_MODE="${POLICY_LOSS_MODE:-vanilla}"

# Use the Megatron CountSketch proxy S(e_y - pi) when the no-padding
# Megatron actor path is active. Latent GRPO falls back to old rollout-side
# token features if this tensor is unavailable.
export CALCULATE_UPDATE_SKETCH="${CALCULATE_UPDATE_SKETCH:-True}"
export UPDATE_SKETCH_DIM="${UPDATE_SKETCH_DIM:-64}"
export UPDATE_SKETCH_SEED="${UPDATE_SKETCH_SEED:-17}"

export LATENT_FACTOR_K="${LATENT_FACTOR_K:-8}"
export LATENT_FACTOR_HIDDEN_DIM="${LATENT_FACTOR_HIDDEN_DIM:-32}"
export LATENT_FACTOR_AUX_STEPS="${LATENT_FACTOR_AUX_STEPS:-8}"
export LATENT_FACTOR_LR="${LATENT_FACTOR_LR:-1e-2}"
export LATENT_FACTOR_TAU="${LATENT_FACTOR_TAU:-1.0}"
export LATENT_FACTOR_BALANCE="${LATENT_FACTOR_BALANCE:-True}"
export LATENT_FACTOR_BALANCE_ITERS="${LATENT_FACTOR_BALANCE_ITERS:-4}"
export LATENT_FACTOR_PRESERVE_SCALAR_MEAN="${LATENT_FACTOR_PRESERVE_SCALAR_MEAN:-True}"
export LATENT_FACTOR_RESIDUAL_CORRECTION="${LATENT_FACTOR_RESIDUAL_CORRECTION:-True}"
export LATENT_FACTOR_MIX_WITH_VANILLA="${LATENT_FACTOR_MIX_WITH_VANILLA:-0.0}"

export EXTRA_LATENT_FACTOR_OVERRIDES=1

"$SCRIPT_DIR/reflect_demo_debug.sh" "$@"
