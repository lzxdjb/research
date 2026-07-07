#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Critic-only suffix-energy weighting:
#   q_t = (1 - exp(old_log_prob_t))^2
#   C_t = sum_{k=t}^{T} rho^(k-t) q_k
# The actor loss is unchanged.
export JOB_NAME="${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic_suffix_value_wi}"
export CRITIC_VALUE_LOSS_WEIGHT_MODE=suffix_score_norm
export CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE=true
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN:-0.25}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX:-4.0}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE:-true}"
export CRITIC_VALUE_LOSS_WEIGHT_RHO="${CRITIC_VALUE_LOSS_WEIGHT_RHO:-1.0}"
export CRITIC_VALUE_LOSS_WEIGHT_ALPHA="${CRITIC_VALUE_LOSS_WEIGHT_ALPHA:-1.0}"

exec "$SCRIPT_DIR/ppo.sh" "$@"

