#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic_entropy_safe_token}"
export POLICY_LOSS_MODE=entropy_safe_token
export TURN_LEVEL_VALUE=False
export CRITIC_VALUE_LOSS_WEIGHT_MODE="${CRITIC_VALUE_LOSS_WEIGHT_MODE:-none}"
export CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE="${CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE:-true}"

exec "$SCRIPT_DIR/ppo.sh" "$@"
