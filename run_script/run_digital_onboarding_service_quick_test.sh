#!/usr/bin/env bash
set -euo pipefail
if [[ "${DIGITAL_ONBOARDING_SHELL_TRACE:-0}" == "1" ]]; then
  set -x
fi
wandb login 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
# Formal service-model RL run.
#
# This trains only the service model. Customer simulation and reward judging
# are served by one or two already-running OpenAI-compatible servers.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
CODE_DIR="$(pwd)"
STORAGE_ROOT="${DIGITAL_ONBOARDING_STORAGE_ROOT:-${ARTIFACT_ROOT:-$CODE_DIR}}"

count_visible_gpus() {
  if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    python3 - "$CUDA_VISIBLE_DEVICES" <<'PY'
import sys
items = [x for x in sys.argv[1].split(",") if x.strip()]
print(len(items))
PY
  else
    echo "${N_GPUS:-${DEFAULT_GPU_COUNT:-8}}"
  fi
}

if [[ -n "${TRAIN_GPUS:-}" ]]; then
  export CUDA_VISIBLE_DEVICES="$TRAIN_GPUS"
fi
VISIBLE_GPU_COUNT="$(count_visible_gpus)"

normalize_openai_chat_endpoint() {
  local endpoint="$1"
  endpoint="${endpoint%/}"
  case "$endpoint" in
    */v1/chat/completions|*/chat/completions)
      printf '%s\n' "$endpoint"
      ;;
    */v1)
      printf '%s/chat/completions\n' "$endpoint"
      ;;
    *)
      printf '%s/v1/chat/completions\n' "$endpoint"
      ;;
  esac
}

CLIENT_REWARD_BASE_URL="${CLIENT_REWARD_BASE_URL:-${QWEN35_27B_BASE_URL:-}}"
CLIENT_BASE_URL="${CLIENT_BASE_URL:-}"
REWARD_BASE_URL="${REWARD_BASE_URL:-}"

if [[ -z "${CLIENT_REWARD_ENDPOINT:-}" && -n "$CLIENT_REWARD_BASE_URL" ]]; then
  CLIENT_REWARD_ENDPOINT="$(normalize_openai_chat_endpoint "$CLIENT_REWARD_BASE_URL")"
fi
CLIENT_REWARD_ENDPOINT="${CLIENT_REWARD_ENDPOINT:?Set CLIENT_REWARD_ENDPOINT=http://<server-ip>:<port>/v1/chat/completions or CLIENT_REWARD_BASE_URL=http://<server-ip>:<port>/v1}"
CLIENT_REWARD_ENDPOINT="$(normalize_openai_chat_endpoint "$CLIENT_REWARD_ENDPOINT")"

if [[ -z "${CLIENT_ENDPOINT:-}" && -n "$CLIENT_BASE_URL" ]]; then
  CLIENT_ENDPOINT="$(normalize_openai_chat_endpoint "$CLIENT_BASE_URL")"
fi
if [[ -z "${REWARD_ENDPOINT:-}" && -n "$REWARD_BASE_URL" ]]; then
  REWARD_ENDPOINT="$(normalize_openai_chat_endpoint "$REWARD_BASE_URL")"
fi
CLIENT_ENDPOINT="$(normalize_openai_chat_endpoint "${CLIENT_ENDPOINT:-$CLIENT_REWARD_ENDPOINT}")"
REWARD_ENDPOINT="$(normalize_openai_chat_endpoint "${REWARD_ENDPOINT:-$CLIENT_REWARD_ENDPOINT}")"
CLIENT_REWARD_MODEL_NAME="${CLIENT_REWARD_MODEL_NAME:-${TEACHER_MODEL_NAME:-${QWEN35_27B_MODEL_NAME:-}}}"
if [[ -z "$CLIENT_REWARD_MODEL_NAME" && -n "$CLIENT_REWARD_BASE_URL" ]]; then
  CLIENT_REWARD_MODEL_NAME="${QWEN35_27B_MODEL_NAME:-/mnt/model/qwen_3_6_27B}"
fi
CLIENT_REWARD_MODEL_NAME="${CLIENT_REWARD_MODEL_NAME:?Set CLIENT_REWARD_MODEL_NAME to the served model name from the server}"
CLIENT_MODEL="${CLIENT_MODEL:-$CLIENT_REWARD_MODEL_NAME}"
REWARD_MODEL="${REWARD_MODEL:-$CLIENT_REWARD_MODEL_NAME}"

DATA_DIR="${DATA_DIR:-$STORAGE_ROOT/data/digital_onboarding/service_formal}"
TRAIN_SIZE="${TRAIN_SIZE:-512}"
VAL_SIZE="${VAL_SIZE:-64}"
DATA_SEED="${DATA_SEED:-17}"
CUSTOM_TRAIN_FILE="${TRAIN_FILE:-}"
CUSTOM_VAL_FILE="${VAL_FILE:-}"
DEFAULT_TRAIN_FILE="$DATA_DIR/train.parquet"
DEFAULT_VAL_FILE="$DATA_DIR/val.parquet"
CHAT_TEMPLATE_ENABLE_THINKING="${CHAT_TEMPLATE_ENABLE_THINKING:-${ENABLE_THINKING:-False}}"
case "${CHAT_TEMPLATE_ENABLE_THINKING,,}" in
  1|true|yes|y|on)
    CHAT_TEMPLATE_ENABLE_THINKING=True
    ;;
  0|false|no|n|off)
    CHAT_TEMPLATE_ENABLE_THINKING=False
    ;;
  *)
    echo "Set ENABLE_THINKING/CHAT_TEMPLATE_ENABLE_THINKING to true or false, got: $CHAT_TEMPLATE_ENABLE_THINKING" >&2
    exit 1
    ;;
esac

if [[ ( -n "$CUSTOM_TRAIN_FILE" && -z "$CUSTOM_VAL_FILE" ) || ( -z "$CUSTOM_TRAIN_FILE" && -n "$CUSTOM_VAL_FILE" ) ]]; then
  echo "Set both TRAIN_FILE and VAL_FILE, or leave both empty to use existing dataset files." >&2
  exit 1
fi

SCENARIO_PHASE="${SCENARIO_PHASE:-${DIGITAL_ONBOARDING_SCENARIO_PHASE:-all}}"
if [[ -z "${BEHAVIOR_MODE:-}" ]]; then
  case "${SCENARIO_PHASE,,}" in
    all|mixed|"")
      BEHAVIOR_MODE="mixed"
      ;;
    phase1|phase_1)
      BEHAVIOR_MODE="phase1"
      ;;
    finishable|can_finish|can-finish)
      BEHAVIOR_MODE="finishable"
      ;;
    phase2|phase_2|unfinishable|cannot_finish|cannot-finish|cant_finish|cant-finish|impossible)
      BEHAVIOR_MODE="unfinishable"
      ;;
    *)
      BEHAVIOR_MODE="$SCENARIO_PHASE"
      ;;
  esac
fi

IS_PHASE1=0
case "${SCENARIO_PHASE,,}" in
  phase1|phase_1)
    IS_PHASE1=1
    ;;
esac
case "${BEHAVIOR_MODE,,}" in
  phase1|cooperative)
    IS_PHASE1=1
    ;;
esac
if [[ -z "${SKIP_SERVER_READY_CHECK:-}" ]]; then
  if [[ "$IS_PHASE1" == "1" ]]; then
    SKIP_SERVER_READY_CHECK=1
  else
    SKIP_SERVER_READY_CHECK=0
  fi
fi

mkdir -p "$STORAGE_ROOT/output" "$DATA_DIR"

if [[ "$SKIP_SERVER_READY_CHECK" == "1" ]]; then
  echo "Skipping client/reward server readiness check for phase 1 bank-rule reward."
else
  python3 - "$CLIENT_REWARD_ENDPOINT" <<'PY'
import json
import sys
import time
import urllib.request

endpoint = sys.argv[1]
models_url = endpoint.rsplit("/v1/chat/completions", 1)[0] + "/v1/models"
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
        time.sleep(10)
else:
    raise SystemExit(f"client/reward server not ready at {models_url}: {last_error}")
PY
fi

if [[ -n "$CUSTOM_TRAIN_FILE" ]]; then
  echo "Using custom TRAIN_FILE=$CUSTOM_TRAIN_FILE"
  echo "Using custom VAL_FILE=$CUSTOM_VAL_FILE"
elif [[ -f "$DEFAULT_TRAIN_FILE" && -f "$DEFAULT_VAL_FILE" ]]; then
  echo "Using existing TRAIN_FILE=$DEFAULT_TRAIN_FILE"
  echo "Using existing VAL_FILE=$DEFAULT_VAL_FILE"
else
  echo "Dataset files not found: $DEFAULT_TRAIN_FILE and $DEFAULT_VAL_FILE" >&2
  echo "Build them separately with recipe.digital_onboarding.scripts.build_data, or set TRAIN_FILE and VAL_FILE." >&2
  exit 1
fi

echo "Digital onboarding scenario phase: $SCENARIO_PHASE"
echo "Digital onboarding behavior mode: $BEHAVIOR_MODE"
echo "Training CUDA_VISIBLE_DEVICES: ${CUDA_VISIBLE_DEVICES:-ALL_VISIBLE_GPUS}"
echo "Training GPU count: $VISIBLE_GPU_COUNT"

PROJECT_DIR="$CODE_DIR"
ENGINE="${ROLLOUT_BACKEND:-vllm}"

export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS="${VLLM_PROMPT_MAX_IMAGE_PIXELS:-602112}"
export VLLM_ALLREDUCE_USE_SYMM_MEM="${VLLM_ALLREDUCE_USE_SYMM_MEM:-0}"
export VLLM_RPC_TIMEOUT="${VLLM_RPC_TIMEOUT:-3600}"
export NCCL_TIMEOUT="${NCCL_TIMEOUT:-7200}"

export CUSTOMER_ENDPOINT="$CLIENT_ENDPOINT"
export CUSTOMER_MODEL="$CLIENT_MODEL"
export REWARD_ENDPOINT="$REWARD_ENDPOINT"
export REWARD_MODEL="$REWARD_MODEL"
export REWARD_FALLBACK_TO_RULE="${REWARD_FALLBACK_TO_RULE:-False}"
export DIGITAL_ONBOARDING_REWARD_ENDPOINT="$REWARD_ENDPOINT"
export DIGITAL_ONBOARDING_REWARD_MODEL="$REWARD_MODEL"
export DIGITAL_ONBOARDING_REWARD_FALLBACK_TO_RULE="$REWARD_FALLBACK_TO_RULE"
export DIGITAL_ONBOARDING_REWARD_MAX_TOKENS="${REWARD_MAX_TOKENS:-1024}"
export DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT="${DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT:-1}"
export DIGITAL_ONBOARDING_REAL_BANK_FAKE_VERIFICATION_WRAPPER="${DIGITAL_ONBOARDING_REAL_BANK_FAKE_VERIFICATION_WRAPPER:-1}"
export DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER="${DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER:-1}"
export DIGITAL_ONBOARDING_REQUIRE_UPLOADED_IMAGE="${DIGITAL_ONBOARDING_REQUIRE_UPLOADED_IMAGE:-1}"
export DIGITAL_ONBOARDING_PROVENANCE_REWARD_ENABLED="${DIGITAL_ONBOARDING_PROVENANCE_REWARD_ENABLED:-0}"
export DIGITAL_ONBOARDING_PROVENANCE_REWARD_WEIGHT="${DIGITAL_ONBOARDING_PROVENANCE_REWARD_WEIGHT:-0.7}"
export DIGITAL_ONBOARDING_PROVENANCE_MAX_PENALTY="${DIGITAL_ONBOARDING_PROVENANCE_MAX_PENALTY:-0.8}"
export DIGITAL_ONBOARDING_PROVENANCE_UNGROUNDED_SUBMIT_MAX_SCORE="${DIGITAL_ONBOARDING_PROVENANCE_UNGROUNDED_SUBMIT_MAX_SCORE:-0.35}"

SERVICE_MODEL_PATH="${SERVICE_MODEL_PATH:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3-Omini-30A3B}"
if [[ -z "${USE_REMOVE_PADDING:-}" ]]; then
  case "${SERVICE_MODEL_PATH,,}" in
    *qwen3-omini*|*qwen3-omni*|*qwen3_omni*)
      # Qwen3-Omni should follow the working reference recipe with remove-padding enabled.
      # The engine now detects Omni vision positions via thinker_config as well, so the
      # standard THD path is the correct one here.
      USE_REMOVE_PADDING=True
      ;;
    *)
      USE_REMOVE_PADDING=True
      ;;
  esac
fi
TRAIN_FILE="${CUSTOM_TRAIN_FILE:-$DEFAULT_TRAIN_FILE}"
VAL_FILE="${CUSTOM_VAL_FILE:-$DEFAULT_VAL_FILE}"
JOB_NAME="${JOB_NAME:-digital_onboarding_service_formal_train}"
PROJECT_NAME="${PROJECT_NAME:-digital_onboarding_tool_rl}"
ROLLOUT_LOG_DIR="${ROLLOUT_LOG_DIR:-${ROLLOUT_DATA_DIR:-$STORAGE_ROOT/rollout_log/$JOB_NAME}}"
export WANDB_DIR="${WANDB_DIR:-$STORAGE_ROOT/wandb/$JOB_NAME}"
mkdir -p "$ROLLOUT_LOG_DIR" "$WANDB_DIR"
export CUSTOMER_ROLLOUT_LOG="${CUSTOMER_ROLLOUT_LOG:-$ROLLOUT_LOG_DIR/customer_turns.jsonl}"
export DIGITAL_ONBOARDING_REWARD_LOG="${DIGITAL_ONBOARDING_REWARD_LOG:-$ROLLOUT_LOG_DIR/reward_judge.jsonl}"
DEBUG_TRACE="${DEBUG_TRACE:-1}"
export DIGITAL_ONBOARDING_DEBUG_ENABLED="${DIGITAL_ONBOARDING_DEBUG_ENABLED:-$DEBUG_TRACE}"
export DIGITAL_ONBOARDING_DEBUG_CSV="${DIGITAL_ONBOARDING_DEBUG_CSV:-$ROLLOUT_LOG_DIR/debug_trace}"
export DIGITAL_ONBOARDING_DEBUG_GROUP_BY="${DIGITAL_ONBOARDING_DEBUG_GROUP_BY:-scenario}"
export DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES="${DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES:-1}"
export VERL_AGENT_LOOP_DEBUG_STDOUT="${VERL_AGENT_LOOP_DEBUG_STDOUT:-0}"
export DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_ENABLED="${DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_ENABLED:-0}"
export DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MIN_TURNS="${DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MIN_TURNS:-3}"
export DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MAX_TURNS="${DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MAX_TURNS:-5}"
export DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_SEED="${DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_SEED:-17}"

MAX_PROMPT_LENGTH="${MAX_PROMPT_LENGTH:-4352}"
MAX_RESPONSE_LENGTH="${MAX_RESPONSE_LENGTH:-12000}"
MAX_TOKEN_LEN="$((MAX_PROMPT_LENGTH + MAX_RESPONSE_LENGTH))"
export CUSTOMER_MAX_TOKENS="${CUSTOMER_MAX_TOKENS:-512}"

N_GPUS="${N_GPUS:-$VISIBLE_GPU_COUNT}"
NNODES="${NNODES:-1}"
case "${SCENARIO_PHASE,,}" in
  phase1|phase_1|finishable|can_finish|can-finish)
    TRAIN_BATCH_SIZE="${TRAIN_BATCH_SIZE:-64}"
    PPO_MINI_BATCH_SIZE="${PPO_MINI_BATCH_SIZE:-32}"
    ROLLOUT_N="${ROLLOUT_N:-1}"
    TOTAL_EPOCHS="${TOTAL_EPOCHS:-1}"
    ;;
  *)
    TRAIN_BATCH_SIZE="${TRAIN_BATCH_SIZE:-64}"
    PPO_MINI_BATCH_SIZE="${PPO_MINI_BATCH_SIZE:-32}"
    ROLLOUT_N="${ROLLOUT_N:-2}"
    TOTAL_EPOCHS="${TOTAL_EPOCHS:-3}"
    ;;
esac
VAL_ROLLOUT_N="${VAL_ROLLOUT_N:-1}"
MAX_ASSISTANT_TURNS="${MAX_ASSISTANT_TURNS:-40}"
MAX_USER_TURNS="${MAX_USER_TURNS:-40}"
export CUSTOMER_MAX_USER_TURNS="${CUSTOMER_MAX_USER_TURNS:-$MAX_USER_TURNS}"

ROLLOUT_TP="${ROLLOUT_TP:-4}"
TRAIN_TP="${TRAIN_TP:-4}"
TRAIN_PP="${TRAIN_PP:-1}"
TRAIN_CP="${TRAIN_CP:-1}"
EP=${EP:-8}
ETP=${ETP:-1}

LR="${LR:-1e-6}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.45}"
LOGGER="${LOGGER:-['console','wandb']}"
POLICY_LOSS_MODE="${POLICY_LOSS_MODE:-grpo_per_turn_soft_adaptive_normalized}"
SAVE_FREQ="${SAVE_FREQ:-20}"
TEST_FREQ="${TEST_FREQ:-10}"

mkdir -p "$STORAGE_ROOT/output" "$STORAGE_ROOT/checkpoints" "$STORAGE_ROOT/val_log" "$STORAGE_ROOT/wandb"

echo "Training service model S_pi from: $SERVICE_MODEL_PATH"
echo "Code root: $PROJECT_DIR"
echo "Storage root: $STORAGE_ROOT"
echo "Customer endpoint: $CUSTOMER_ENDPOINT"
echo "Customer model: $CUSTOMER_MODEL"
echo "Reward endpoint: $DIGITAL_ONBOARDING_REWARD_ENDPOINT"
echo "Reward model: $DIGITAL_ONBOARDING_REWARD_MODEL"
echo "Saving S_pi checkpoints to: $STORAGE_ROOT/checkpoints/$JOB_NAME"
echo "Customer simulator log: $CUSTOMER_ROLLOUT_LOG"
echo "Reward judge log: $DIGITAL_ONBOARDING_REWARD_LOG"
echo "W&B local dir: $WANDB_DIR"
echo "Per-trajectory debug CSV dir: $DIGITAL_ONBOARDING_DEBUG_CSV"
echo "Debug trace enabled: $DIGITAL_ONBOARDING_DEBUG_ENABLED"
echo "Debug trace group by: $DIGITAL_ONBOARDING_DEBUG_GROUP_BY"
echo "Debug CSV escape newlines: $DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES"
echo "Agent-loop stdout turn debug: $VERL_AGENT_LOOP_DEBUG_STDOUT"
echo "Customer interruption injection: $DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_ENABLED"
echo "Customer interruption interval: ${DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MIN_TURNS}-${DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MAX_TURNS} turns"
echo "Provenance reward enabled: $DIGITAL_ONBOARDING_PROVENANCE_REWARD_ENABLED"
echo "Provenance reward weight: $DIGITAL_ONBOARDING_PROVENANCE_REWARD_WEIGHT"
echo "Provenance max penalty: $DIGITAL_ONBOARDING_PROVENANCE_MAX_PENALTY"
echo "Provenance ungrounded submit max score: $DIGITAL_ONBOARDING_PROVENANCE_UNGROUNDED_SUBMIT_MAX_SCORE"
echo "Customer simulator max tokens: $CUSTOMER_MAX_TOKENS"
echo "Reward judge max tokens: $DIGITAL_ONBOARDING_REWARD_MAX_TOKENS"
echo "Megatron use_remove_padding: $USE_REMOVE_PADDING"
echo "Max assistant turns: $MAX_ASSISTANT_TURNS"
echo "Max user turns: $MAX_USER_TURNS"
echo "Chat template enable_thinking: $CHAT_TEMPLATE_ENABLE_THINKING"

if [[ "${KILL_EXISTING_VLLM:-0}" == "1" ]]; then
  pkill -TERM -f 'vllm' || true
  pkill -TERM -f 'VLLM' || true
  sleep 3
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
fi

if [[ "${STOP_RAY_BEFORE_TRAIN:-0}" == "1" ]]; then
  ray stop || true
fi

python3 -m verl.trainer.main_ppo \
  --config-path=config \
  --config-name=ppo_megatron_trainer.yaml \
  algorithm.adv_estimator=grpo \
  algorithm.use_kl_in_reward=False \
  +data.apply_chat_template_kwargs.enable_thinking="$CHAT_TEMPLATE_ENABLE_THINKING" \
  data.train_files="$TRAIN_FILE" \
  data.val_files="$VAL_FILE" \
  data.return_raw_chat=True \
  data.dataloader_num_workers=0 \
  data.seed="${DATA_SEED:-42}" \
  data.train_batch_size="$TRAIN_BATCH_SIZE" \
  data.max_prompt_length="$MAX_PROMPT_LENGTH" \
  data.max_response_length="$MAX_RESPONSE_LENGTH" \
  data.filter_overlong_prompts="${FILTER_OVERLONG_PROMPTS:-False}" \
  data.truncation=error \
  data.tool_config_path="$PROJECT_DIR/recipe/digital_onboarding/config/tool_config.yaml" \
  actor_rollout_ref.model.path="$SERVICE_MODEL_PATH" \
  actor_rollout_ref.model.use_remove_padding="$USE_REMOVE_PADDING" \
  actor_rollout_ref.model.enable_gradient_checkpointing=True \
  actor_rollout_ref.actor.optim.lr="$LR" \
  actor_rollout_ref.actor.ppo_mini_batch_size="$PPO_MINI_BATCH_SIZE" \
  actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu="${PPO_MICRO_BATCH_SIZE_PER_GPU:-1}" \
  actor_rollout_ref.actor.ppo_max_token_len_per_gpu="$MAX_TOKEN_LEN" \
  actor_rollout_ref.actor.use_kl_loss="${USE_KL_LOSS:-False}" \
  actor_rollout_ref.actor.kl_loss_coef="${KL_COEF:-0.001}" \
  actor_rollout_ref.actor.entropy_coeff=0 \
  actor_rollout_ref.actor.policy_loss.loss_mode="$POLICY_LOSS_MODE" \
  actor_rollout_ref.rollout.name="$ENGINE" \
  actor_rollout_ref.rollout.mode=async \
  actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu="${ROLLOUT_LOGPROB_MICRO_BSZ:-1}" \
  actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu="$MAX_TOKEN_LEN" \
  actor_rollout_ref.rollout.tensor_model_parallel_size="$ROLLOUT_TP" \
  actor_rollout_ref.rollout.gpu_memory_utilization="$GPU_MEMORY_UTILIZATION" \
  actor_rollout_ref.rollout.n="$ROLLOUT_N" \
  actor_rollout_ref.rollout.val_kwargs.n="$VAL_ROLLOUT_N" \
  actor_rollout_ref.rollout.val_kwargs.temperature=0.0 \
  actor_rollout_ref.rollout.multi_turn.enable=True \
  actor_rollout_ref.rollout.multi_turn.max_parallel_calls=1 \
  actor_rollout_ref.rollout.multi_turn.max_assistant_turns="$MAX_ASSISTANT_TURNS" \
  actor_rollout_ref.rollout.multi_turn.max_user_turns="$MAX_USER_TURNS" \
  actor_rollout_ref.rollout.multi_turn.max_tool_response_length="${MAX_TOOL_RESPONSE_LENGTH:-2048}" \
  actor_rollout_ref.rollout.multi_turn.tool_response_truncate_side=middle \
  actor_rollout_ref.rollout.multi_turn.format="${TOOL_FORMAT:-hermes}" \
  actor_rollout_ref.rollout.multi_turn.tool_config_path="$PROJECT_DIR/recipe/digital_onboarding/config/tool_config.yaml" \
  actor_rollout_ref.rollout.multi_turn.interaction_config_path="$PROJECT_DIR/recipe/digital_onboarding/config/interaction_config.yaml" \
  actor_rollout_ref.rollout.multi_turn.tokenization_sanity_check_mode=ignore_strippable \
  actor_rollout_ref.rollout.agent.default_agent_loop=tool_agent \
  actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu="${REF_LOGPROB_MICRO_BSZ:-1}" \
  actor_rollout_ref.ref.log_prob_max_token_len_per_gpu="$MAX_TOKEN_LEN" \
  actor_rollout_ref.actor.megatron.tensor_model_parallel_size="$TRAIN_TP" \
  actor_rollout_ref.actor.megatron.pipeline_model_parallel_size="$TRAIN_PP" \
  actor_rollout_ref.actor.megatron.context_parallel_size="$TRAIN_CP" \
  actor_rollout_ref.actor.megatron.use_remove_padding="$USE_REMOVE_PADDING" \
  actor_rollout_ref.actor.megatron.use_mbridge=True \
  actor_rollout_ref.actor.megatron.param_offload=True \
  actor_rollout_ref.actor.megatron.optimizer_offload=True \
  actor_rollout_ref.actor.megatron.grad_offload=True \
  actor_rollout_ref.ref.megatron.param_offload=True \
  actor_rollout_ref.ref.megatron.use_remove_padding="$USE_REMOVE_PADDING" \
  +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_offload_fraction=1 \
  +actor_rollout_ref.actor.optim.override_optimizer_config.overlap_cpu_optimizer_d2h_h2d=True \
  +actor_rollout_ref.actor.optim.override_optimizer_config.use_precision_aware_optimizer=True \
  +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_cpu_offload=True \
  +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_method=uniform \
  +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_granularity=full \
  +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_num_layers=1 \
  +actor_rollout_ref.actor.megatron.override_transformer_config.gradient_accumulation_fusion=True \
  reward.reward_manager.name=dapo \
  reward.custom_reward_function.path="$PROJECT_DIR/recipe/digital_onboarding/model_reward_function.py" \
  actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
  reward.custom_reward_function.name=compute_score \
  trainer.critic_warmup=0 \
  trainer.logger="$LOGGER" \
  trainer.project_name="$PROJECT_NAME" \
  trainer.experiment_name="$JOB_NAME" \
  trainer.n_gpus_per_node="$N_GPUS" \
  trainer.nnodes="$NNODES" \
  trainer.default_local_dir="$STORAGE_ROOT/checkpoints/$JOB_NAME" \
  +trainer.val_data_dir="$STORAGE_ROOT/val_log/$JOB_NAME" \
  trainer.rollout_data_dir="$ROLLOUT_LOG_DIR" \
  trainer.save_freq="$SAVE_FREQ" \
  trainer.test_freq="$TEST_FREQ" \
  trainer.total_epochs="$TOTAL_EPOCHS" \
  trainer.val_before_train="${VAL_BEFORE_TRAIN:-False}" \
  actor_rollout_ref.actor.megatron.dist_ckpt_optim_fully_reshardable=False \
  trainer.use_legacy_worker_impl=disable \
  "$@"
