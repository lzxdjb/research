#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic_turn_level_wi}"
export POLICY_LOSS_MODE=turn_level_ppo
# wi is used as the critic/baseline MSE scaler, not as an actor loss multiplier.
export CRITIC_VALUE_LOSS_WEIGHT_MODE=turn_score_norm
export CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE=true

exec "$SCRIPT_DIR/ppo.sh" "$@"
