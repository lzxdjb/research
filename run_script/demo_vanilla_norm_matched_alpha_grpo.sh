#!/usr/bin/env bash
set -euo pipefail
set -x

PROJECT_DIR=${PROJECT_DIR:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/research}
cd "$PROJECT_DIR"

export WANDB_DIR=${WANDB_DIR:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/wandb_real}
mkdir -p "$PROJECT_DIR/output"

ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0

export https_proxy="${https_proxy:-http://hexin:hx300033@10.217.180.65:30100}"
export http_proxy="${http_proxy:-http://hexin:hx300033@10.217.180.65:30100}"

THSCC_TRAIN_CREATOR=${THSCC_TRAIN_CREATOR:-wiki}
JOB_NAME=${JOB_NAME:-8_vanilla_norm_matched_alpha_grpo}

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
fi

export VLLM_RPC_TIMEOUT=3600
export NCCL_TIMEOUT=7200

CALCULATE_SUM_PI_SQUARED=${CALCULATE_SUM_PI_SQUARED:-True}
SUM_PI_SQUARED_CHECKPOINTING=${SUM_PI_SQUARED_CHECKPOINTING:-False}
INTENTIONAL_ETA=${INTENTIONAL_ETA:-1}
INTENTIONAL_SCORE_NORM_EPS=${INTENTIONAL_SCORE_NORM_EPS:-1e-8}
INTENTIONAL_CLIP_TARGET=${INTENTIONAL_CLIP_TARGET:-True}
INTENTIONAL_REQUIRE_SUM_PI_SQUARED=${INTENTIONAL_REQUIRE_SUM_PI_SQUARED:-True}
INTENTIONAL_ALPHA_MIN=${INTENTIONAL_ALPHA_MIN:-0.0}
INTENTIONAL_ALPHA_MAX=${INTENTIONAL_ALPHA_MAX:-null}

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

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml' \
    data.dataloader_num_workers=0 \
    data.seed=42 \
    data.train_files="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/train_mix.parquet" \
    data.val_files="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_mix.parquet" \
    +data.multi_val_files.wiki2="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_2wiki.parquet" \
    +data.multi_val_files.bamboogle="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_bamboogle.parquet" \
    +data.multi_val_files.hotpotqa="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_hotpotqa.parquet" \
    +data.multi_val_files.musique="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_musique.parquet" \
    +data.multi_val_files.math="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_math.parquet" \
    +data.multi_val_files.numina_math="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_numina_math.parquet" \
    +data.multi_val_files.aimo_amc="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_aimo_amc.parquet" \
    +data.multi_val_files.code_apps="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/challenging_multidomain_benchmark/val_code_apps.parquet" \
    data.train_batch_size=${TRAIN_BATCH_SIZE:-32} \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B \
    actor_rollout_ref.actor.optim.lr=${ACTOR_LR:-1e-6} \
    actor_rollout_ref.actor.ppo_mini_batch_size=${PPO_MINI_BATCH_SIZE:-32} \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=${PPO_MICRO_BATCH_SIZE_PER_GPU:-2} \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.policy_loss.loss_mode=vanilla_norm_matched_alpha_grpo \
    actor_rollout_ref.actor.policy_loss.intentional_eta=$INTENTIONAL_ETA \
    actor_rollout_ref.actor.policy_loss.intentional_score_norm_eps=$INTENTIONAL_SCORE_NORM_EPS \
    actor_rollout_ref.actor.policy_loss.intentional_clip_target=$INTENTIONAL_CLIP_TARGET \
    actor_rollout_ref.actor.policy_loss.intentional_require_sum_pi_squared=$INTENTIONAL_REQUIRE_SUM_PI_SQUARED \
    actor_rollout_ref.actor.policy_loss.intentional_alpha_min=$INTENTIONAL_ALPHA_MIN \
    actor_rollout_ref.actor.policy_loss.intentional_alpha_max=$INTENTIONAL_ALPHA_MAX \
    actor_rollout_ref.actor.calculate_sum_pi_squared=$CALCULATE_SUM_PI_SQUARED \
    actor_rollout_ref.actor.sum_pi_squared_checkpointing=$SUM_PI_SQUARED_CHECKPOINTING \
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
    +trainer.val_data_dir=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/val_log/${JOB_NAME} \
    trainer.nnodes=1 \
    trainer.save_freq=${SAVE_FREQ:-5} \
    trainer.test_freq=${TEST_FREQ:-5} \
    trainer.total_epochs=${TOTAL_EPOCHS:-15} \
    trainer.default_local_dir=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/${JOB_NAME} \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/examples/sglang_multiturn/config/tool_config/hot_stock_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=${MAX_TOOL_RESPONSE_LENGTH:-2048} \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton
