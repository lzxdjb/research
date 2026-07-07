#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export JOB_NAME="${JOB_NAME:-wiki_user_sim_reflect_entropy_reward_first}"
export POLICY_LOSS_MODE=entropy_reward_first

exec "$SCRIPT_DIR/reflect_demo.sh" "$@"
