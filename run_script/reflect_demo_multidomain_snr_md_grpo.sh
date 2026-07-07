#!/usr/bin/env bash
set -euo pipefail

export JOB_NAME="${JOB_NAME:-multidomain_snr_md_grpo}"
export ADV_ESTIMATOR=snr_multi_domain_grpo
export POLICY_LOSS_MODE="${POLICY_LOSS_MODE:-vanilla}"

# Required for the sketch proxy. This returns a low-dimensional CountSketch of
# the categorical score vector e_y - pi from the Megatron log-prob forward pass.
export CALCULATE_UPDATE_SKETCH="${CALCULATE_UPDATE_SKETCH:-True}"
export UPDATE_SKETCH_DIM="${UPDATE_SKETCH_DIM:-64}"
export UPDATE_SKETCH_SEED="${UPDATE_SKETCH_SEED:-17}"

export SNR_MD_GRPO_INCLUDE_FEATURE_STD="${SNR_MD_GRPO_INCLUDE_FEATURE_STD:-False}"
export SNR_MD_GRPO_NORMALIZE_FEATURES="${SNR_MD_GRPO_NORMALIZE_FEATURES:-True}"
export SNR_MD_GRPO_EPS="${SNR_MD_GRPO_EPS:-1e-6}"
export SNR_MD_GRPO_DOMAIN_KEY="${SNR_MD_GRPO_DOMAIN_KEY:-domain}"
export SNR_MD_GRPO_MIN_DOMAIN_COUNT="${SNR_MD_GRPO_MIN_DOMAIN_COUNT:-2}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/reflect_demo_multidomain.sh" "$@"
