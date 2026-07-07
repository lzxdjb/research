#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Turn-level PPO with length-normalized (geometric-mean) turn importance ratios.
# See `turn_level_ppo_geo_mean` in `verl/trainer/ppo/core_algos.py`.
export JOB_NAME="${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic_turn_level_geo_mean}"
export POLICY_LOSS_MODE=turn_level_ppo_geo_mean
export CRITIC_VALUE_LOSS_WEIGHT_MODE="${CRITIC_VALUE_LOSS_WEIGHT_MODE:-none}"
export CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE="${CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE:-true}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN:-null}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX:-null}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE:-true}"
export CRITIC_VALUE_LOSS_WEIGHT_RHO="${CRITIC_VALUE_LOSS_WEIGHT_RHO:-1.0}"
export CRITIC_VALUE_LOSS_WEIGHT_ALPHA="${CRITIC_VALUE_LOSS_WEIGHT_ALPHA:-1.0}"

exec "$SCRIPT_DIR/ppo.sh" "$@"
