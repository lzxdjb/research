#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic_turn_level}"

# Choose actor turn-level ratio:
#   geo     -> length-normalized turn ratio (default)
#   product -> raw product turn ratio
TURN_RATIO_MODE="${TURN_RATIO_MODE:-geo}"
if [[ "$TURN_RATIO_MODE" == "product" ]]; then
  export POLICY_LOSS_MODE=turn_level_ppo_product
else
  export POLICY_LOSS_MODE=turn_level_ppo
fi

# Choose critic/value weighting:
#   none, token_score_norm, turn_score_norm, suffix_score_norm, turn_suffix_score_norm
export CRITIC_VALUE_LOSS_WEIGHT_MODE="${CRITIC_VALUE_LOSS_WEIGHT_MODE:-none}"
export CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE="${CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE:-true}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN:-null}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX:-null}"
export CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE="${CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE:-true}"
export CRITIC_VALUE_LOSS_WEIGHT_RHO="${CRITIC_VALUE_LOSS_WEIGHT_RHO:-1.0}"
export CRITIC_VALUE_LOSS_WEIGHT_ALPHA="${CRITIC_VALUE_LOSS_WEIGHT_ALPHA:-1.0}"

exec "$SCRIPT_DIR/ppo.sh" "$@"
