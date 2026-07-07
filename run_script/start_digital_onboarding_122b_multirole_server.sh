#!/usr/bin/env bash
set -xeuo pipefail

# Formal server: start one OpenAI-compatible 122B server that can play frozen
# roles by request-level system prompt:
# - customer simulator C_theta bootstrap
# - reward judge R_phi bootstrap
# - outer teacher
#
# No model reload is needed when switching roles; the prompt changes per request.
#
# This is a standalone server script. It does not depend on the training repo
# at runtime; you can copy it to another machine and run it there as long as
# vLLM and the model path are available.

count_visible_gpus() {
  if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    python3 - "$CUDA_VISIBLE_DEVICES" <<'PY'
import sys
items = [x for x in sys.argv[1].split(",") if x.strip()]
print(len(items))
PY
  elif command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi -L | wc -l
  else
    echo 1
  fi
}

if [[ -n "${SERVER_GPUS:-}" ]]; then
  export CUDA_VISIBLE_DEVICES="$SERVER_GPUS"
fi

TEACHER_MODEL_PATH="${TEACHER_MODEL_PATH:-${TEACHER_MODEL:-/path/to/your/122B-A10B}}"
TEACHER_MODEL_NAME="${TEACHER_MODEL_NAME:-$TEACHER_MODEL_PATH}"
TEACHER_HOST="${TEACHER_HOST:-0.0.0.0}"
TEACHER_PORT="${TEACHER_PORT:-8002}"
PUBLIC_HOST="${PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"
VISIBLE_GPU_COUNT="$(count_visible_gpus)"
SERVER_TP="${SERVER_TP:-$VISIBLE_GPU_COUNT}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
DTYPE="${DTYPE:-bfloat16}"

export VLLM_ALLREDUCE_USE_SYMM_MEM="${VLLM_ALLREDUCE_USE_SYMM_MEM:-0}"
export VLLM_RPC_TIMEOUT="${VLLM_RPC_TIMEOUT:-3600}"

echo "Starting digital onboarding formal client/reward server"
echo "Model path: $TEACHER_MODEL_PATH"
echo "Served model name: $TEACHER_MODEL_NAME"
echo "CUDA_VISIBLE_DEVICES: ${CUDA_VISIBLE_DEVICES:-ALL_VISIBLE_GPUS}"
echo "Tensor parallel size: $SERVER_TP"
echo "Bind address: $TEACHER_HOST:$TEACHER_PORT"

vllm serve "$TEACHER_MODEL_PATH" \
  --served-model-name "$TEACHER_MODEL_NAME" \
  --host "$TEACHER_HOST" \
  --port "$TEACHER_PORT" \
  --tensor-parallel-size "$SERVER_TP" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --max-model-len "$MAX_MODEL_LEN" \
  --dtype "$DTYPE" \
  "$@" &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup INT TERM EXIT

HEALTH_HOST="$TEACHER_HOST"
if [[ "$HEALTH_HOST" == "0.0.0.0" || "$HEALTH_HOST" == "::" ]]; then
  HEALTH_HOST="127.0.0.1"
fi

python3 - "$HEALTH_HOST" "$TEACHER_PORT" <<'PY'
import json
import sys
import time
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
models_url = f"http://{host}:{port}/v1/models"
deadline = time.time() + 1800
last_error = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(models_url, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
        print(json.dumps({"server_ready": True, "models": data}, ensure_ascii=False)[:1000])
        break
    except Exception as exc:
        last_error = str(exc)
        time.sleep(5)
else:
    raise SystemExit(f"server not ready at {models_url}: {last_error}")
PY

cat <<EOF

============================================================
Digital onboarding formal server is ready.

Use this from the training machine:

CLIENT_REWARD_ENDPOINT=http://$PUBLIC_HOST:$TEACHER_PORT/v1/chat/completions
CLIENT_REWARD_MODEL_NAME=$TEACHER_MODEL_NAME

Example:

cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
CLIENT_REWARD_ENDPOINT=http://$PUBLIC_HOST:$TEACHER_PORT/v1/chat/completions \\
CLIENT_REWARD_MODEL_NAME=$TEACHER_MODEL_NAME \\
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \\
bash run_script/run_digital_onboarding_service_formal_train.sh
============================================================

EOF

wait "$SERVER_PID"
