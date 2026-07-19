#!/usr/bin/env bash
set -euo pipefail
set -x

PROJECT_DIR=${PROJECT_DIR:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/research}
cd "$PROJECT_DIR"

STOCK_RL_ROOT=${STOCK_RL_ROOT:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect}
DATA_ROOT=${DATA_ROOT:-$STOCK_RL_ROOT/data/challenging_multidomain_benchmark}
MODEL_PATH=${MODEL_PATH:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B}
TOOL_CONFIG_PATH=${TOOL_CONFIG_PATH:-$STOCK_RL_ROOT/examples/sglang_multiturn/config/tool_config/multihop_qa_tool_config.yaml}

export WANDB_DIR=${WANDB_DIR:-$STOCK_RL_ROOT/wandb_real}
mkdir -p "$PROJECT_DIR/output"

ENGINE=${1:-vllm}
if (( $# > 0 )); then
    shift
fi

export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0
export https_proxy="${https_proxy:-http://hexin:hx300033@10.217.180.65:30100}"
export http_proxy="${http_proxy:-http://hexin:hx300033@10.217.180.65:30100}"

THSCC_TRAIN_CREATOR=${THSCC_TRAIN_CREATOR:-wiki_9B}
JOB_NAME=${JOB_NAME:-gspo_9B}

if [[ -n "${WANDB_API_KEY:-}" ]]; then
    wandb login "$WANDB_API_KEY"
fi

export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME

max_prompt_length=${MAX_PROMPT_LENGTH:-1100}
max_response_length=${MAX_RESPONSE_LENGTH:-6000}
max_token_len=$((max_prompt_length + max_response_length))

SKIP_PIP_INSTALL=${SKIP_PIP_INSTALL:-0}
if [[ "$SKIP_PIP_INSTALL" != "1" && "$SKIP_PIP_INSTALL" != "true" ]]; then
    pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0
    pip install wikipedia
    pip install sentence-transformers fastapi uvicorn
fi

export VLLM_RPC_TIMEOUT=3600
export NCCL_TIMEOUT=7200

GSPO_CLIP_RATIO_LOW=${GSPO_CLIP_RATIO_LOW:-0.0003}
GSPO_CLIP_RATIO_HIGH=${GSPO_CLIP_RATIO_HIGH:-0.0004}
MEGATRON_PARAM_OFFLOAD=${MEGATRON_PARAM_OFFLOAD:-True}
MEGATRON_OPTIMIZER_OFFLOAD=${MEGATRON_OPTIMIZER_OFFLOAD:-True}
MEGATRON_GRAD_OFFLOAD=${MEGATRON_GRAD_OFFLOAD:-True}
REF_MEGATRON_PARAM_OFFLOAD=${REF_MEGATRON_PARAM_OFFLOAD:-True}
OPTIMIZER_OFFLOAD_FRACTION=${OPTIMIZER_OFFLOAD_FRACTION:-1}
OVERLAP_CPU_OPTIMIZER_D2H_H2D=${OVERLAP_CPU_OPTIMIZER_D2H_H2D:-True}
USE_PRECISION_AWARE_OPTIMIZER=${USE_PRECISION_AWARE_OPTIMIZER:-True}
OPTIMIZER_CPU_OFFLOAD=${OPTIMIZER_CPU_OFFLOAD:-True}
RECOMPUTE_METHOD=${RECOMPUTE_METHOD:-uniform}
RECOMPUTE_GRANULARITY=${RECOMPUTE_GRANULARITY:-full}
RECOMPUTE_NUM_LAYERS=${RECOMPUTE_NUM_LAYERS:-1}
GRADIENT_ACCUMULATION_FUSION=${GRADIENT_ACCUMULATION_FUSION:-True}

VAL_ROLLOUT_N=${VAL_ROLLOUT_N:-4}
VAL_DO_SAMPLE=${VAL_DO_SAMPLE:-True}
VAL_TEMPERATURE=${VAL_TEMPERATURE:-1.0}
VAL_TOP_P=${VAL_TOP_P:-0.7}
VAL_TOP_K=${VAL_TOP_K:--1}

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml' \
    data.dataloader_num_workers=0 \
    data.seed=42 \
    data.train_files="$DATA_ROOT/train_mix.parquet" \
    data.val_files="$DATA_ROOT/val_mix.parquet" \
    +data.multi_val_files.wiki2="$DATA_ROOT/val_2wiki.parquet" \
    +data.multi_val_files.bamboogle="$DATA_ROOT/val_bamboogle.parquet" \
    +data.multi_val_files.hotpotqa="$DATA_ROOT/val_hotpotqa.parquet" \
    +data.multi_val_files.musique="$DATA_ROOT/val_musique.parquet" \
    +data.multi_val_files.math="$DATA_ROOT/val_math.parquet" \
    +data.multi_val_files.numina_math="$DATA_ROOT/val_numina_math.parquet" \
    +data.multi_val_files.aimo_amc="$DATA_ROOT/val_aimo_amc.parquet" \
    +data.multi_val_files.code_apps="$DATA_ROOT/val_code_apps.parquet" \
    data.train_batch_size=${TRAIN_BATCH_SIZE:-32} \
    data.max_prompt_length=$max_prompt_length \
    data.max_response_length=$max_response_length \
    data.filter_overlong_prompts=True \
    data.truncation=left \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr=${ACTOR_LR:-3e-6} \
    algorithm.kl_ctrl.kl_coef=0.0 \
    actor_rollout_ref.actor.ppo_mini_batch_size=${PPO_MINI_BATCH_SIZE:-32} \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=${PPO_MICRO_BATCH_SIZE_PER_GPU:-2} \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.policy_loss.loss_mode=gspo \
    actor_rollout_ref.actor.loss_agg_mode=seq-mean-token-mean \
    actor_rollout_ref.actor.clip_ratio_low=$GSPO_CLIP_RATIO_LOW \
    actor_rollout_ref.actor.clip_ratio_high=$GSPO_CLIP_RATIO_HIGH \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=2 \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.8 \
    actor_rollout_ref.rollout.n=${ROLLOUT_N:-8} \
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=2 \
    actor_rollout_ref.actor.megatron.pipeline_model_parallel_size=1 \
    actor_rollout_ref.actor.megatron.context_parallel_size=1 \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.actor.megatron.use_mbridge=True \
    actor_rollout_ref.actor.megatron.param_offload=$MEGATRON_PARAM_OFFLOAD \
    actor_rollout_ref.actor.megatron.optimizer_offload=$MEGATRON_OPTIMIZER_OFFLOAD \
    actor_rollout_ref.actor.megatron.grad_offload=$MEGATRON_GRAD_OFFLOAD \
    actor_rollout_ref.ref.megatron.param_offload=$REF_MEGATRON_PARAM_OFFLOAD \
    actor_rollout_ref.actor.megatron.dist_ckpt_optim_fully_reshardable=False \
    trainer.use_legacy_worker_impl=disable \
    +actor_rollout_ref.rollout.multi_turn.use_seeupo=True \
    +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_offload_fraction=$OPTIMIZER_OFFLOAD_FRACTION \
    +actor_rollout_ref.actor.optim.override_optimizer_config.overlap_cpu_optimizer_d2h_h2d=$OVERLAP_CPU_OPTIMIZER_D2H_H2D \
    +actor_rollout_ref.actor.optim.override_optimizer_config.use_precision_aware_optimizer=$USE_PRECISION_AWARE_OPTIMIZER \
    +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_cpu_offload=$OPTIMIZER_CPU_OFFLOAD \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_method=$RECOMPUTE_METHOD \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_granularity=$RECOMPUTE_GRANULARITY \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_num_layers=$RECOMPUTE_NUM_LAYERS \
    +actor_rollout_ref.actor.megatron.override_transformer_config.gradient_accumulation_fusion=$GRADIENT_ACCUMULATION_FUSION \
    algorithm.adv_estimator=grpo \
    algorithm.use_kl_in_reward=False \
    trainer.critic_warmup=0 \
    trainer.logger='["console","wandb"]' \
    trainer.project_name=$SWANLAB_PROJECT \
    trainer.experiment_name=$SWANLAB_EXP_NAME \
    trainer.n_gpus_per_node=8 \
    +trainer.val_data_dir="$STOCK_RL_ROOT/val_log/$JOB_NAME" \
    trainer.nnodes=1 \
    trainer.save_freq=${SAVE_FREQ:-5} \
    trainer.test_freq=${TEST_FREQ:-5} \
    trainer.total_epochs=${TOTAL_EPOCHS:-3} \
    trainer.default_local_dir="$STOCK_RL_ROOT/checkpoints/$JOB_NAME" \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="$TOOL_CONFIG_PATH" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=${MAX_TOOL_RESPONSE_LENGTH:-2048} \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton \
    actor_rollout_ref.rollout.val_kwargs.n=$VAL_ROLLOUT_N \
    actor_rollout_ref.rollout.val_kwargs.do_sample=$VAL_DO_SAMPLE \
    actor_rollout_ref.rollout.val_kwargs.temperature=$VAL_TEMPERATURE \
    actor_rollout_ref.rollout.val_kwargs.top_p=$VAL_TOP_P \
    actor_rollout_ref.rollout.val_kwargs.top_k=$VAL_TOP_K \
    "$@"
