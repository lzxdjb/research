#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic_token_is_turn_value_wi}"
export POLICY_LOSS_MODE=vanilla
export TURN_LEVEL_VALUE=True
export TURN_LEVEL_VALUE_ANCHOR="${TURN_LEVEL_VALUE_ANCHOR:-first}"
export CRITIC_VALUE_LOSS_WEIGHT_MODE=turn_score_norm
export CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE=true

exec "$SCRIPT_DIR/ppo.sh" "$@"
