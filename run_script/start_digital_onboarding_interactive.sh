#!/usr/bin/env bash
set -euo pipefail

# Launch an interactive browser UI for a trained digital-onboarding checkpoint.
#
# Default behavior:
#   1. Serve the Hugging Face actor checkpoint with vLLM.
#   2. Start a FastAPI web UI that talks to that local vLLM endpoint.
#
# To use an already-running OpenAI-compatible model endpoint, set:
#   START_MODEL_SERVER=0
#   DIGITAL_ONBOARDING_SERVICE_ENDPOINT=http://host:port/v1/chat/completions
#   DIGITAL_ONBOARDING_SERVICE_MODEL=<served-model-name>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
PROJECT_ROOT="$(pwd)"

count_visible_gpus() {
  if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    python3 - "$CUDA_VISIBLE_DEVICES" <<'PY'
import sys
items = [item for item in sys.argv[1].split(",") if item.strip()]
print(len(items))
PY
  elif command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi -L | wc -l
  else
    echo 1
  fi
}

read_model_max_length() {
  local model_dir="$1"
  python3 - "$model_dir" <<'PY'
import json
import sys
from pathlib import Path

model_dir = Path(sys.argv[1])
config = model_dir / "config.json"
if config.is_file():
    try:
        data = json.loads(config.read_text())
        candidates = []
        for path in [
            ("max_position_embeddings",),
            ("text_config", "max_position_embeddings"),
            ("thinker_config", "max_position_embeddings"),
            ("thinker_config", "text_config", "max_position_embeddings"),
            ("talker_config", "text_config", "max_position_embeddings"),
        ]:
            obj = data
            for key in path:
                obj = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(obj, (int, float)) and obj > 0:
                candidates.append(int(obj))
        if candidates:
            print(max(candidates))
            raise SystemExit(0)
    except Exception:
        pass

tokenizer_config = model_dir / "tokenizer_config.json"
if tokenizer_config.is_file():
    try:
        value = json.loads(tokenizer_config.read_text()).get("model_max_length")
        if isinstance(value, int) and value > 0:
            print(value)
            raise SystemExit(0)
    except Exception:
        pass
print(131072)
PY
}

if [[ -n "${SERVER_GPUS:-}" ]]; then
  export CUDA_VISIBLE_DEVICES="$SERVER_GPUS"
fi

CHECKPOINT_DIR="${CHECKPOINT_DIR:-$PROJECT_ROOT/checkpoints/formal_train/global_step_25}"
if [[ -z "${MODEL_PATH:-}" ]]; then
  if [[ -d "$CHECKPOINT_DIR/actor/huggingface" ]]; then
    MODEL_PATH="$CHECKPOINT_DIR/actor/huggingface"
  else
    MODEL_PATH="$CHECKPOINT_DIR"
  fi
fi
MODEL_NAME="${MODEL_NAME:-$MODEL_PATH}"

START_MODEL_SERVER="${START_MODEL_SERVER:-1}"
MODEL_BIND_HOST="${MODEL_BIND_HOST:-127.0.0.1}"
MODEL_PORT="${MODEL_PORT:-8010}"
WEB_HOST="${WEB_HOST:-0.0.0.0}"
WEB_PORT="${WEB_PORT:-7860}"
DEBUGPY_HOST="${DEBUGPY_HOST:-127.0.0.1}"
DEBUGPY_PORT="${DEBUGPY_PORT:-5678}"
PUBLIC_HOST="${PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"

VISIBLE_GPU_COUNT="$(count_visible_gpus)"
SERVER_TP="${SERVER_TP:-$VISIBLE_GPU_COUNT}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-$(read_model_max_length "$MODEL_PATH")}"
DTYPE="${DTYPE:-bfloat16}"
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"

export VLLM_ALLREDUCE_USE_SYMM_MEM="${VLLM_ALLREDUCE_USE_SYMM_MEM:-0}"
export VLLM_RPC_TIMEOUT="${VLLM_RPC_TIMEOUT:-3600}"
export DIGITAL_ONBOARDING_TOOL_BACKEND="${DIGITAL_ONBOARDING_TOOL_BACKEND:-real_bank}"
export DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES="${DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES:-1}"
export DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT="${DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT:-1}"
export DIGITAL_ONBOARDING_REAL_BANK_FAKE_VERIFICATION_WRAPPER="${DIGITAL_ONBOARDING_REAL_BANK_FAKE_VERIFICATION_WRAPPER:-1}"
export DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER="${DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER:-1}"
export DIGITAL_ONBOARDING_REQUIRE_UPLOADED_IMAGE="${DIGITAL_ONBOARDING_REQUIRE_UPLOADED_IMAGE:-1}"
export ENABLE_THINKING="${ENABLE_THINKING:-True}"
export INTERACTIVE_MAX_TOKENS="${INTERACTIVE_MAX_TOKENS:-4096}"
export INTERACTIVE_TEMPERATURE="${INTERACTIVE_TEMPERATURE:-0.2}"
export INTERACTIVE_SEND_OPENAI_TOOLS="${INTERACTIVE_SEND_OPENAI_TOOLS:-0}"
export INTERACTIVE_CONTEXT_WINDOW="${INTERACTIVE_CONTEXT_WINDOW:-$MAX_MODEL_LEN}"

VLLM_PID=""
cleanup() {
  if [[ -n "$VLLM_PID" ]] && kill -0 "$VLLM_PID" >/dev/null 2>&1; then
    kill "$VLLM_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup INT TERM EXIT

if [[ "${STOP_EXISTING_SERVERS:-0}" == "1" ]]; then
  ray stop || true
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
fi

if [[ "$START_MODEL_SERVER" == "1" ]]; then
  echo "Starting service model server"
  echo "Model path: $MODEL_PATH"
  echo "Served model name: $MODEL_NAME"
  echo "CUDA_VISIBLE_DEVICES: ${CUDA_VISIBLE_DEVICES:-ALL_VISIBLE_GPUS}"
  echo "Tensor parallel size: $SERVER_TP"
  echo "Model endpoint: http://$MODEL_BIND_HOST:$MODEL_PORT/v1/chat/completions"

  VLLM_CMD=(
    vllm serve "$MODEL_PATH"
    --served-model-name "$MODEL_NAME"
    --host "$MODEL_BIND_HOST"
    --port "$MODEL_PORT"
    --tensor-parallel-size "$SERVER_TP"
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
    --max-model-len "$MAX_MODEL_LEN"
    --dtype "$DTYPE"
  )
  if [[ "$TRUST_REMOTE_CODE" == "1" || "$TRUST_REMOTE_CODE" == "true" || "$TRUST_REMOTE_CODE" == "True" ]]; then
    VLLM_CMD+=(--trust-remote-code)
  fi

  "${VLLM_CMD[@]}" "$@" &
  VLLM_PID=$!

  python3 - "$MODEL_BIND_HOST" "$MODEL_PORT" <<'PY'
import json
import sys
import time
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
health_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
models_url = f"http://{health_host}:{port}/v1/models"
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
    raise SystemExit(f"model server not ready at {models_url}: {last_error}")
PY

  export DIGITAL_ONBOARDING_SERVICE_ENDPOINT="http://$MODEL_BIND_HOST:$MODEL_PORT/v1/chat/completions"
  export DIGITAL_ONBOARDING_SERVICE_MODEL="$MODEL_NAME"
else
  : "${DIGITAL_ONBOARDING_SERVICE_ENDPOINT:?Set DIGITAL_ONBOARDING_SERVICE_ENDPOINT when START_MODEL_SERVER=0}"
  : "${DIGITAL_ONBOARDING_SERVICE_MODEL:?Set DIGITAL_ONBOARDING_SERVICE_MODEL when START_MODEL_SERVER=0}"
fi

cat <<EOF

============================================================
Digital onboarding interactive UI is starting.

Web UI:
  http://$PUBLIC_HOST:$WEB_PORT

For voice input from a Mac browser over SSH, use port forwarding and open:
  ssh -L $WEB_PORT:127.0.0.1:$WEB_PORT <user>@<server>
  http://localhost:$WEB_PORT

Debug:
  Set INTERACTIVE_DEBUGPY=1 before launching to wait for VS Code on $DEBUGPY_HOST:$DEBUGPY_PORT.

Model:
  $DIGITAL_ONBOARDING_SERVICE_MODEL
  $DIGITAL_ONBOARDING_SERVICE_ENDPOINT

Tool backend:
  $DIGITAL_ONBOARDING_TOOL_BACKEND
============================================================

EOF

if [[ "${INTERACTIVE_DEBUGPY:-0}" == "1" ]]; then
  echo "Waiting for VS Code debugger on $DEBUGPY_HOST:$DEBUGPY_PORT"
  python3 -m debugpy --listen "$DEBUGPY_HOST:$DEBUGPY_PORT" --wait-for-client \
    -m uvicorn recipe.digital_onboarding.interactive_web:app --host "$WEB_HOST" --port "$WEB_PORT"
else
  python3 -m uvicorn recipe.digital_onboarding.interactive_web:app --host "$WEB_HOST" --port "$WEB_PORT"
fi
