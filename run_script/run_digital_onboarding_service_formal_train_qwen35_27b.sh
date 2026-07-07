#!/usr/bin/env bash
set -xeuo pipefail

# Use the shared OpenAI-compatible Qwen3.5 27B vLLM service as both the
# customer simulator and reward judge for digital onboarding training.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

QWEN35_27B_BASE_URL="${QWEN35_27B_BASE_URL:-${CLIENT_REWARD_BASE_URL:-http://interactive-h8uvtimmgmw3:18886/v1}}"
QWEN35_27B_MODEL_NAME="${QWEN35_27B_MODEL_NAME:-${CLIENT_REWARD_MODEL_NAME:-/mnt/model/qwen_3_6_27B}}"

export CLIENT_REWARD_BASE_URL="$QWEN35_27B_BASE_URL"
export CLIENT_REWARD_MODEL_NAME="$QWEN35_27B_MODEL_NAME"

exec bash "$SCRIPT_DIR/run_digital_onboarding_service_formal_train.sh" "$@"
