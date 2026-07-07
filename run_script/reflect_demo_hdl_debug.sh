#!/usr/bin/env bash
set -euo pipefail
set -x

ray stop || true
pkill -KILL -f 'vllm' || true
pkill -KILL -f 'VLLM' || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

ENGINE=${1:-vllm}
JOB_NAME=${JOB_NAME:-hdl_agent_smoke}
PROJECT_NAME=${PROJECT_NAME:-hdl_agent}

export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_ALLREDUCE_USE_SYMM_MEM=${VLLM_ALLREDUCE_USE_SYMM_MEM:-0}
export VLLM_RPC_TIMEOUT=${VLLM_RPC_TIMEOUT:-3600}
export NCCL_TIMEOUT=${NCCL_TIMEOUT:-7200}
export XDG_CACHE_HOME=${XDG_CACHE_HOME:-"$PROJECT_DIR/.cache"}
export FLASHINFER_WORKSPACE_BASE=${FLASHINFER_WORKSPACE_BASE:-"$PROJECT_DIR"}
export TRITON_CACHE_DIR=${TRITON_CACHE_DIR:-"$PROJECT_DIR/.cache/triton"}
mkdir -p "$XDG_CACHE_HOME" "$TRITON_CACHE_DIR"

HDL_ENV_SH=${HDL_ENV_SH:-"$PROJECT_DIR/hdl_env/env.sh"}
if [[ ! -f "$HDL_ENV_SH" ]]; then
    bash "$PROJECT_DIR/hdl_env/rebuild_hdl_env.sh"
fi
export HDL_ENV_SH
export HDL_AGENT_MAX_ROUNDS=${HDL_AGENT_MAX_ROUNDS:-2}
export HDL_AGENT_TIMEOUT=${HDL_AGENT_TIMEOUT:-30}
export HDL_AGENT_FEEDBACK_MAX_CHARS=${HDL_AGENT_FEEDBACK_MAX_CHARS:-5000}
export HDL_AGENT_KEEP_WORK=${HDL_AGENT_KEEP_WORK:-0}

VERIFY_HDL_ENV=${VERIFY_HDL_ENV:-1}
if [[ "$VERIFY_HDL_ENV" == "1" || "$VERIFY_HDL_ENV" == "true" ]]; then
    bash "$PROJECT_DIR/hdl_env/smoke_test_hdl_env.sh"
fi

DATASET_DIR=${DATASET_DIR:-"$PROJECT_DIR/data/hdl_agent_smoke"}
REBUILD_DATASET=${REBUILD_DATASET:-1}
if [[ "$REBUILD_DATASET" == "1" || "$REBUILD_DATASET" == "true" ]]; then
    python "$PROJECT_DIR/recipe/hdl_agent/build_smoke_dataset.py" \
        --output-dir "$DATASET_DIR" \
        --train-repeat "${HDL_SMOKE_TRAIN_REPEAT:-4}" \
        --seed "${SEED:-42}"
fi

python - "$DATASET_DIR/train.jsonl" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    row = json.loads(f.readline())
if row.get("agent_name") != "hdl_agent" or row.get("data_source") != "hdl_agent_smoke":
    raise SystemExit(f"stale HDL dataset at {sys.argv[1]}: {row.get('agent_name')=} {row.get('data_source')=}")
PY

train_path="$DATASET_DIR/train.jsonl"
test_path="$DATASET_DIR/val.jsonl"

max_prompt_length=${MAX_PROMPT_LENGTH:-2048}
max_response_length=${MAX_RESPONSE_LENGTH:-8192}
max_token_len=$((max_prompt_length + max_response_length))

MODEL_PATH=${MODEL_PATH:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-4B}
LR=${LR:-1e-6}
ADV_ESTIMATOR=${ADV_ESTIMATOR:-grpo}
POLICY_LOSS_MODE=${POLICY_LOSS_MODE:-vanilla}
TRAIN_BATCH_SIZE=${TRAIN_BATCH_SIZE:-8}
PPO_MINI_BATCH_SIZE=${PPO_MINI_BATCH_SIZE:-4}
ROLLOUT_N=${ROLLOUT_N:-4}
TOTAL_EPOCHS=${TOTAL_EPOCHS:-1}
N_GPUS_PER_NODE=${N_GPUS_PER_NODE:-8}
TP_SIZE=${TP_SIZE:-4}
GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.60}

ENABLE_WANDB=${ENABLE_WANDB:-0}
if [[ "$ENABLE_WANDB" == "1" || "$ENABLE_WANDB" == "true" ]]; then
    TRAINER_LOGGER='["console","wandb"]'
else
    export WANDB_MODE=disabled
    TRAINER_LOGGER='["console"]'
fi

VAL_DATA_DIR=${VAL_DATA_DIR:-"$PROJECT_DIR/val_log/${JOB_NAME}"}
ROLLOUT_DATA_DIR=${ROLLOUT_DATA_DIR:-"$PROJECT_DIR/rollout_log/${JOB_NAME}"}
mkdir -p "$PROJECT_DIR/output" "$VAL_DATA_DIR" "$ROLLOUT_DATA_DIR"

EXTRA_OVERRIDES=()
if [[ -n "${TOTAL_TRAINING_STEPS:-}" ]]; then
    EXTRA_OVERRIDES+=(trainer.total_training_steps="$TOTAL_TRAINING_STEPS")
fi
if [[ -n "${TRAIN_MAX_SAMPLES:-}" ]]; then
    EXTRA_OVERRIDES+=(data.train_max_samples="$TRAIN_MAX_SAMPLES")
fi
if [[ -n "${VAL_MAX_SAMPLES:-}" ]]; then
    EXTRA_OVERRIDES+=(data.val_max_samples="$VAL_MAX_SAMPLES")
fi

python3 -m verl.trainer.main_ppo --config-path="$PROJECT_DIR/verl/trainer/config" \
    --config-name='ppo_megatron_trainer.yaml' \
    algorithm.adv_estimator="$ADV_ESTIMATOR" \
    data.dataloader_num_workers=0 \
    data.seed="${SEED:-42}" \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    data.train_batch_size="$TRAIN_BATCH_SIZE" \
    data.max_prompt_length="$max_prompt_length" \
    data.max_response_length="$max_response_length" \
    data.return_raw_chat=True \
    data.filter_overlong_prompts=False \
    data.truncation='error' \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr="$LR" \
    actor_rollout_ref.actor.ppo_mini_batch_size="$PPO_MINI_BATCH_SIZE" \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.actor.policy_loss.loss_mode="$POLICY_LOSS_MODE" \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.rollout.name="$ENGINE" \
    actor_rollout_ref.rollout.prompt_length="$max_prompt_length" \
    actor_rollout_ref.rollout.response_length="$max_response_length" \
    actor_rollout_ref.rollout.max_model_len="$max_token_len" \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu="$max_token_len" \
    actor_rollout_ref.rollout.tensor_model_parallel_size="$TP_SIZE" \
    actor_rollout_ref.rollout.gpu_memory_utilization="$GPU_MEMORY_UTILIZATION" \
    actor_rollout_ref.rollout.n="$ROLLOUT_N" \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="$PROJECT_DIR/recipe/hdl_agent/config/tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns="$HDL_AGENT_MAX_ROUNDS" \
    actor_rollout_ref.rollout.multi_turn.max_user_turns="$HDL_AGENT_MAX_ROUNDS" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length="${MAX_TOOL_RESPONSE_LENGTH:-2048}" \
    actor_rollout_ref.rollout.multi_turn.hdl_max_rounds="$HDL_AGENT_MAX_ROUNDS" \
    actor_rollout_ref.rollout.multi_turn.hdl_judge_timeout="$HDL_AGENT_TIMEOUT" \
    actor_rollout_ref.rollout.multi_turn.hdl_feedback_max_chars="$HDL_AGENT_FEEDBACK_MAX_CHARS" \
    actor_rollout_ref.rollout.multi_turn.hdl_keep_judge_work="$HDL_AGENT_KEEP_WORK" \
    actor_rollout_ref.rollout.multi_turn.hdl_env_sh="$HDL_ENV_SH" \
    actor_rollout_ref.rollout.multi_turn.tokenization_sanity_check_mode=ignore_strippable \
    actor_rollout_ref.rollout.agent.default_agent_loop=hdl_agent \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu="$max_token_len" \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu="$max_token_len" \
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size="$TP_SIZE" \
    actor_rollout_ref.actor.megatron.pipeline_model_parallel_size=1 \
    actor_rollout_ref.actor.megatron.context_parallel_size=1 \
    actor_rollout_ref.actor.megatron.use_mbridge=True \
    actor_rollout_ref.actor.megatron.param_offload=True \
    actor_rollout_ref.actor.megatron.optimizer_offload=True \
    actor_rollout_ref.actor.megatron.grad_offload=True \
    actor_rollout_ref.ref.megatron.param_offload=True \
    actor_rollout_ref.actor.megatron.dist_ckpt_optim_fully_reshardable=False \
    trainer.use_legacy_worker_impl=disable \
    algorithm.use_kl_in_reward=False \
    trainer.critic_warmup=0 \
    trainer.logger="$TRAINER_LOGGER" \
    trainer.project_name="$PROJECT_NAME" \
    trainer.experiment_name="$JOB_NAME" \
    trainer.n_gpus_per_node="$N_GPUS_PER_NODE" \
    trainer.nnodes=1 \
    trainer.val_before_train="${VALIDATE_BEFORE_TRAIN:-True}" \
    trainer.save_freq="${SAVE_FREQ:--1}" \
    trainer.test_freq="${TEST_FREQ:-1}" \
    trainer.total_epochs="$TOTAL_EPOCHS" \
    trainer.default_local_dir="$PROJECT_DIR/checkpoints/${JOB_NAME}" \
    trainer.rollout_data_dir="$ROLLOUT_DATA_DIR" \
    reward.custom_reward_function.path="$PROJECT_DIR/recipe/hdl_agent/reward_function.py" \
    reward.custom_reward_function.name=compute_score \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton \
    +ray_kwargs.ray_init.runtime_env.env_vars.XDG_CACHE_HOME="$XDG_CACHE_HOME" \
    +ray_kwargs.ray_init.runtime_env.env_vars.FLASHINFER_WORKSPACE_BASE="$FLASHINFER_WORKSPACE_BASE" \
    +ray_kwargs.ray_init.runtime_env.env_vars.TRITON_CACHE_DIR="$TRITON_CACHE_DIR" \
    "${EXTRA_OVERRIDES[@]}"
