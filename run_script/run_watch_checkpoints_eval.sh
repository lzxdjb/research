#!/usr/bin/env bash
# set -euo pipefail

cleanup_vllm() {
  pkill -TERM -f 'agent-o3/watch_checkpoints_eval.py' || true
  pkill -TERM -f 'agent-o3/main_contrast_out.py' || true
  pkill -TERM -f 'vllm.entrypoints.openai.api_server' || true
  pkill -TERM -f 'vllm' || true
  pkill -TERM -f 'VLLM' || true
  sleep 5
  pkill -KILL -f 'agent-o3/watch_checkpoints_eval.py' || true
  pkill -KILL -f 'agent-o3/main_contrast_out.py' || true
  pkill -KILL -f 'vllm.entrypoints.openai.api_server' || true
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
}

trap cleanup_vllm EXIT INT TERM HUP

# rm  ./data/30/data/dpo_260224/vllm_server.log

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CHECKPOINT_ROOT="${CHECKPOINT_ROOT:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/stock_grpo}"
OUTPUT_ROOT="${OUTPUT_ROOT:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/eval}"
INPUT_JSONL="${INPUT_JSONL:-./data/30/data/eval.jsonl}"
INPUT_IMAGES_DIR="${INPUT_IMAGES_DIR:-./data/30/images}"
WANDB_PROJECT="${WANDB_PROJECT:-stock-rl-reflect-eval}"
WANDB_RUN_NAME="${WANDB_RUN_NAME:-wiki_reflect_5000_inject_only}"
WATCH_LOG="${WATCH_LOG:-$OUTPUT_ROOT/watch_checkpoints_eval.log}"
BEGIN_STEP="${BEGIN_STEP:-}"
END_STEP="${END_STEP:-}"
FORCE_REEVAL="${FORCE_REEVAL:-0}"
ONCE="${ONCE:-0}"

WATCH_RANGE_ARGS=()
if [[ -n "$BEGIN_STEP" ]]; then
  WATCH_RANGE_ARGS+=(--begin-step "$BEGIN_STEP")
fi
if [[ -n "$END_STEP" ]]; then
  WATCH_RANGE_ARGS+=(--end-step "$END_STEP")
fi
if [[ "$FORCE_REEVAL" == "1" || "$FORCE_REEVAL" == "true" || "$FORCE_REEVAL" == "TRUE" ]]; then
  WATCH_RANGE_ARGS+=(--force-reeval)
fi
if [[ "$ONCE" == "1" || "$ONCE" == "true" || "$ONCE" == "TRUE" ]]; then
  WATCH_RANGE_ARGS+=(--once)
fi

mkdir -p "$OUTPUT_ROOT"
cleanup_vllm

CMD=(
  python agent-o3/watch_checkpoints_eval.py
  --checkpoint-root "$CHECKPOINT_ROOT"
  --output-root "$OUTPUT_ROOT"
  --wandb-project "$WANDB_PROJECT"
  --wandb-run-name "$WANDB_RUN_NAME"
  --ready-wait-seconds 300
  --checkpoint-subdir actor/huggingface
  --poll-seconds 30
  --failure-retry-seconds 900
)

CMD+=("${WATCH_RANGE_ARGS[@]}")

CMD+=(
  --input "$INPUT_JSONL"
  --input_images_dir "$INPUT_IMAGES_DIR"
  --vllm_port 8100
  --served_model_name local-model
  --vllm_tensor_parallel_size 4
  --vllm_enable_expert_parallel
  --vllm_expert_placement_strategy round_robin
  --vllm_gpu_memory_utilization 0.80
  --vllm_max_model_len 32000
  --vllm_dtype bfloat16
)

printf 'Running command:'
printf ' %q' "${CMD[@]}"
printf '\n'

"${CMD[@]}" 2>&1 | tee -a "$WATCH_LOG"
