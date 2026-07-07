#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic_token_level_wi}"
export POLICY_LOSS_MODE=vanilla
export TURN_LEVEL_VALUE=False
export CRITIC_VALUE_LOSS_WEIGHT_MODE=token_score_norm
export CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE=true
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN:-0.2}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX:-5.0}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE=false

exec "$SCRIPT_DIR/ppo.sh" "$@"
