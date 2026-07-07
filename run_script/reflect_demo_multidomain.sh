#!/usr/bin/env bash
set -euo pipefail
set -x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

ENGINE=${1:-${ENGINE:-vllm}}
PYTHON=${PYTHON:-python3}

if [[ "${CLEANUP_BEFORE_RUN:-1}" == "1" || "${CLEANUP_BEFORE_RUN:-1}" == "true" ]]; then
    ray stop || true
    pkill -KILL -f 'vllm' || true
    pkill -KILL -f 'VLLM' || true
fi

export CUDA_DEVICE_MAX_CONNECTIONS=${CUDA_DEVICE_MAX_CONNECTIONS:-1}
export VLLM_PROMPT_MAX_IMAGE_PIXELS=${VLLM_PROMPT_MAX_IMAGE_PIXELS:-602112}
export VLLM_ALLREDUCE_USE_SYMM_MEM=${VLLM_ALLREDUCE_USE_SYMM_MEM:-0}
export VLLM_RPC_TIMEOUT=${VLLM_RPC_TIMEOUT:-3600}
export NCCL_TIMEOUT=${NCCL_TIMEOUT:-7200}

JOB_NAME=${JOB_NAME:-multidomain_grpo_baseline}
ADV_ESTIMATOR=${ADV_ESTIMATOR:-grpo}
POLICY_LOSS_MODE=${POLICY_LOSS_MODE:-vanilla}
CALCULATE_SUM_PI_SQUARED=${CALCULATE_SUM_PI_SQUARED:-False}
CALCULATE_UPDATE_SKETCH=${CALCULATE_UPDATE_SKETCH:-False}
UPDATE_SKETCH_DIM=${UPDATE_SKETCH_DIM:-64}
UPDATE_SKETCH_SEED=${UPDATE_SKETCH_SEED:-17}
THSCC_TRAIN_CREATOR=${THSCC_TRAIN_CREATOR:-multidomain_rl}

export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME

WANDB_STORE_DIR=${WANDB_STORE_DIR:-"$PROJECT_DIR/wandb"}
mkdir -p "$WANDB_STORE_DIR"
export WANDB_DIR="${WANDB_DIR:-$(dirname "$WANDB_STORE_DIR")}"

ENABLE_WANDB=${ENABLE_WANDB:-0}
if [[ "$ENABLE_WANDB" == "1" || "$ENABLE_WANDB" == "true" ]]; then
    if [[ -n "${WANDB_API_KEY:-}" ]]; then
        wandb login "$WANDB_API_KEY"
    fi
    TRAINER_LOGGER='["console","wandb"]'
else
    export WANDB_MODE=disabled
    TRAINER_LOGGER='["console"]'
fi

RAW_DATA_DIR=${RAW_DATA_DIR:-"$PROJECT_DIR/data/multihop_raw"}
MULTIHOP_DIR=${MULTIHOP_DIR:-"$PROJECT_DIR/data/multihop_mix"}
MULTIDOMAIN_DIR=${MULTIDOMAIN_DIR:-"$PROJECT_DIR/data/multidomain_mix"}

DOWNLOAD_MULTIHOP_DATASET=${DOWNLOAD_MULTIHOP_DATASET:-0}
PREPARE_MULTIHOP_DATASET=${PREPARE_MULTIHOP_DATASET:-1}
REBUILD_MULTIDOMAIN_DATASET=${REBUILD_MULTIDOMAIN_DATASET:-1}

if [[ "$DOWNLOAD_MULTIHOP_DATASET" == "1" || "$DOWNLOAD_MULTIHOP_DATASET" == "true" ]]; then
    "$PYTHON" "$PROJECT_DIR/download_multihop_datasets.py"
fi

if [[ "$PREPARE_MULTIHOP_DATASET" == "1" || "$PREPARE_MULTIHOP_DATASET" == "true" ]]; then
    "$PYTHON" "$PROJECT_DIR/dataset_make_multihop_mix.py" \
        --hotpot_train    "$RAW_DATA_DIR/hotpot_train.jsonl" \
        --wiki2_train     "$RAW_DATA_DIR/2wiki_train.jsonl" \
        --musique_train   "$RAW_DATA_DIR/musique_train.jsonl" \
        --hotpot_test     "$RAW_DATA_DIR/hotpot_test.jsonl" \
        --wiki2_test      "$RAW_DATA_DIR/2wiki_test.jsonl" \
        --musique_test    "$RAW_DATA_DIR/musique_test.jsonl" \
        --bamboogle_test  "$RAW_DATA_DIR/bamboogle.jsonl" \
        --output_dir      "$MULTIHOP_DIR" \
        --val_ratio       "${MULTIHOP_VAL_RATIO:-0.003}" \
        --max_per_source  "${MULTIHOP_MAX_PER_SOURCE:-5000}" \
        --max_test_per_source "${MULTIHOP_MAX_TEST_PER_SOURCE:-100}" \
        --seed            "${SEED:-42}"
fi

MATH_TRAIN=${MATH_TRAIN:-}
MATH_VAL=${MATH_VAL:-}
CODE_TRAIN=${CODE_TRAIN:-}
CODE_VAL=${CODE_VAL:-}

if [[ "$REBUILD_MULTIDOMAIN_DATASET" == "1" || "$REBUILD_MULTIDOMAIN_DATASET" == "true" ]]; then
    if [[ -z "$MATH_TRAIN" || -z "$MATH_VAL" || -z "$CODE_TRAIN" || -z "$CODE_VAL" ]]; then
        cat >&2 <<'EOF'
Missing Math/Code dataset paths.

Set these environment variables to existing Verl-format parquet/jsonl files:
  MATH_TRAIN=/path/to/math/train.parquet
  MATH_VAL=/path/to/math/test.parquet
  CODE_TRAIN=/path/to/code/train.parquet
  CODE_VAL=/path/to/code/test.parquet

For Math, this repo can build one dataset with:
  python -m examples.data_preprocess.math_dataset --local_save_dir ./data/math

For Code, provide a supported RL dataset whose data_source is one of:
  codecontests, apps, codeforces, taco
or set CODE_DATA_SOURCE to one of those names when merging.
EOF
        exit 1
    fi

    MERGE_ARGS=(
        --multihop_train "$MULTIHOP_DIR/train_mix.parquet"
        --multihop_val "$MULTIHOP_DIR/val_mix.parquet"
        --math_train "$MATH_TRAIN"
        --math_val "$MATH_VAL"
        --code_train "$CODE_TRAIN"
        --code_val "$CODE_VAL"
        --output_dir "$MULTIDOMAIN_DIR"
        --seed "${SEED:-42}"
    )
    if [[ -n "${MAX_MULTIHOP_TRAIN:-}" ]]; then
        MERGE_ARGS+=(--max_multihop_train "$MAX_MULTIHOP_TRAIN")
    fi
    if [[ -n "${MAX_MATH_TRAIN:-}" ]]; then
        MERGE_ARGS+=(--max_math_train "$MAX_MATH_TRAIN")
    fi
    if [[ -n "${MAX_CODE_TRAIN:-}" ]]; then
        MERGE_ARGS+=(--max_code_train "$MAX_CODE_TRAIN")
    fi
    if [[ -n "${MATH_DATA_SOURCE:-}" ]]; then
        MERGE_ARGS+=(--math_data_source "$MATH_DATA_SOURCE")
    fi
    if [[ -n "${CODE_DATA_SOURCE:-}" ]]; then
        MERGE_ARGS+=(--code_data_source "$CODE_DATA_SOURCE")
    fi
    if [[ -n "${MULTIHOP_AGENT:-}" ]]; then
        MERGE_ARGS+=(--multihop_agent "$MULTIHOP_AGENT")
    fi

    "$PYTHON" "$PROJECT_DIR/dataset_make_multidomain_mix.py" "${MERGE_ARGS[@]}"
fi

train_path=${TRAIN_PATH:-"$MULTIDOMAIN_DIR/train_mix.parquet"}
test_path=${TEST_PATH:-"$MULTIDOMAIN_DIR/val_mix.parquet"}
max_prompt_length=${MAX_PROMPT_LENGTH:-1100}
max_response_length=${MAX_RESPONSE_LENGTH:-12000}
max_token_len=$((max_prompt_length + max_response_length))
TRAIN_BATCH_SIZE=${TRAIN_BATCH_SIZE:-64}
PPO_MINI_BATCH_SIZE=${PPO_MINI_BATCH_SIZE:-32}
ROLLOUT_N=${ROLLOUT_N:-8}

SKIP_PIP_INSTALL=${SKIP_PIP_INSTALL:-1}
if [[ "$SKIP_PIP_INSTALL" != "1" && "$SKIP_PIP_INSTALL" != "true" ]]; then
    pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0
    pip install wikipedia rank-bm25
fi

TOOL_CONFIG_PATH=${TOOL_CONFIG_PATH:-"$PROJECT_DIR/examples/sglang_multiturn/config/tool_config/multihop_qa_tool_config.yaml"}
MODEL_PATH=${MODEL_PATH:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B}
CHECKPOINT_ROOT=${CHECKPOINT_ROOT:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints}
ROLLOUT_DATA_DIR=${ROLLOUT_DATA_DIR:-"$PROJECT_DIR/rollout_log/${JOB_NAME}"}
VAL_DATA_DIR=${VAL_DATA_DIR:-"$PROJECT_DIR/val_log/${JOB_NAME}"}

MD_BOS_GRPO_K=${MD_BOS_GRPO_K:-4}
MD_BOS_GRPO_DOMAIN_LAMBDA=${MD_BOS_GRPO_DOMAIN_LAMBDA:-1.0}
MD_BOS_GRPO_NOISE_LAMBDA=${MD_BOS_GRPO_NOISE_LAMBDA:-1.0}
MD_BOS_GRPO_WEIGHT_FLOOR=${MD_BOS_GRPO_WEIGHT_FLOOR:-0.1}
MD_BOS_GRPO_WEIGHT_POWER=${MD_BOS_GRPO_WEIGHT_POWER:-1.0}
MD_BOS_GRPO_MIX_WITH_VANILLA=${MD_BOS_GRPO_MIX_WITH_VANILLA:-0.0}
MD_BOS_GRPO_POSITIVE_EIGS_ONLY=${MD_BOS_GRPO_POSITIVE_EIGS_ONLY:-True}
MD_BOS_GRPO_FALLBACK_TO_VANILLA=${MD_BOS_GRPO_FALLBACK_TO_VANILLA:-True}
MD_BOS_GRPO_INCLUDE_FEATURE_STD=${MD_BOS_GRPO_INCLUDE_FEATURE_STD:-True}
MD_BOS_GRPO_NORMALIZE_FEATURES=${MD_BOS_GRPO_NORMALIZE_FEATURES:-True}
MD_BOS_GRPO_EPS=${MD_BOS_GRPO_EPS:-1e-6}
MD_BOS_GRPO_DOMAIN_KEY=${MD_BOS_GRPO_DOMAIN_KEY:-domain}
MD_BOS_GRPO_MIN_DOMAIN_COUNT=${MD_BOS_GRPO_MIN_DOMAIN_COUNT:-2}

SP_MD_BOS_GRPO_SHARED_K=${SP_MD_BOS_GRPO_SHARED_K:-4}
SP_MD_BOS_GRPO_PRIVATE_K=${SP_MD_BOS_GRPO_PRIVATE_K:-2}
SP_MD_BOS_GRPO_DOMAIN_LAMBDA=${SP_MD_BOS_GRPO_DOMAIN_LAMBDA:-1.0}
SP_MD_BOS_GRPO_SHARED_NOISE_LAMBDA=${SP_MD_BOS_GRPO_SHARED_NOISE_LAMBDA:-1.0}
SP_MD_BOS_GRPO_PRIVATE_NOISE_LAMBDA=${SP_MD_BOS_GRPO_PRIVATE_NOISE_LAMBDA:-1.0}
SP_MD_BOS_GRPO_WEIGHT_FLOOR=${SP_MD_BOS_GRPO_WEIGHT_FLOOR:-0.3}
SP_MD_BOS_GRPO_WEIGHT_POWER=${SP_MD_BOS_GRPO_WEIGHT_POWER:-1.0}
SP_MD_BOS_GRPO_MIX_WITH_VANILLA=${SP_MD_BOS_GRPO_MIX_WITH_VANILLA:-0.3}
SP_MD_BOS_GRPO_POSITIVE_EIGS_ONLY=${SP_MD_BOS_GRPO_POSITIVE_EIGS_ONLY:-True}
SP_MD_BOS_GRPO_FALLBACK_TO_VANILLA=${SP_MD_BOS_GRPO_FALLBACK_TO_VANILLA:-True}
SP_MD_BOS_GRPO_INCLUDE_FEATURE_STD=${SP_MD_BOS_GRPO_INCLUDE_FEATURE_STD:-True}
SP_MD_BOS_GRPO_NORMALIZE_FEATURES=${SP_MD_BOS_GRPO_NORMALIZE_FEATURES:-True}
SP_MD_BOS_GRPO_EPS=${SP_MD_BOS_GRPO_EPS:-1e-6}
SP_MD_BOS_GRPO_DOMAIN_KEY=${SP_MD_BOS_GRPO_DOMAIN_KEY:-domain}
SP_MD_BOS_GRPO_MIN_DOMAIN_COUNT=${SP_MD_BOS_GRPO_MIN_DOMAIN_COUNT:-2}

SNR_MD_GRPO_INCLUDE_FEATURE_STD=${SNR_MD_GRPO_INCLUDE_FEATURE_STD:-False}
SNR_MD_GRPO_NORMALIZE_FEATURES=${SNR_MD_GRPO_NORMALIZE_FEATURES:-True}
SNR_MD_GRPO_EPS=${SNR_MD_GRPO_EPS:-1e-6}
SNR_MD_GRPO_DOMAIN_KEY=${SNR_MD_GRPO_DOMAIN_KEY:-domain}
SNR_MD_GRPO_MIN_DOMAIN_COUNT=${SNR_MD_GRPO_MIN_DOMAIN_COUNT:-2}

EXTRA_OVERRIDES=()
if [[ -n "${TOTAL_TRAINING_STEPS:-}" ]]; then
    EXTRA_OVERRIDES+=(trainer.total_training_steps="$TOTAL_TRAINING_STEPS")
fi
if [[ -n "${VAL_MAX_SAMPLES:-}" ]]; then
    EXTRA_OVERRIDES+=(data.val_max_samples="$VAL_MAX_SAMPLES")
fi
if [[ -n "${TRAIN_MAX_SAMPLES:-}" ]]; then
    EXTRA_OVERRIDES+=(data.train_max_samples="$TRAIN_MAX_SAMPLES")
fi
if [[ -n "${SANDBOX_FUSION_URL:-}" ]]; then
    EXTRA_OVERRIDES+=(reward.sandbox_fusion.url="$SANDBOX_FUSION_URL")
fi
if [[ -n "${SANDBOX_FUSION_MAX_CONCURRENT:-}" ]]; then
    EXTRA_OVERRIDES+=(reward.sandbox_fusion.max_concurrent="$SANDBOX_FUSION_MAX_CONCURRENT")
fi
if [[ -n "${SANDBOX_FUSION_MEMORY_LIMIT_MB:-}" ]]; then
    EXTRA_OVERRIDES+=(reward.sandbox_fusion.memory_limit_mb="$SANDBOX_FUSION_MEMORY_LIMIT_MB")
fi
if [[ "$ADV_ESTIMATOR" == "multi_domain_bos_grpo" || "$ADV_ESTIMATOR" == "md_bos_grpo" || "${EXTRA_MD_BOS_GRPO_OVERRIDES:-0}" == "1" ]]; then
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_k="$MD_BOS_GRPO_K")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_domain_lambda="$MD_BOS_GRPO_DOMAIN_LAMBDA")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_noise_lambda="$MD_BOS_GRPO_NOISE_LAMBDA")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_weight_floor="$MD_BOS_GRPO_WEIGHT_FLOOR")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_weight_power="$MD_BOS_GRPO_WEIGHT_POWER")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_mix_with_vanilla="$MD_BOS_GRPO_MIX_WITH_VANILLA")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_positive_eigs_only="$MD_BOS_GRPO_POSITIVE_EIGS_ONLY")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_fallback_to_vanilla="$MD_BOS_GRPO_FALLBACK_TO_VANILLA")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_include_feature_std="$MD_BOS_GRPO_INCLUDE_FEATURE_STD")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_normalize_features="$MD_BOS_GRPO_NORMALIZE_FEATURES")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_eps="$MD_BOS_GRPO_EPS")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_domain_key="$MD_BOS_GRPO_DOMAIN_KEY")
    EXTRA_OVERRIDES+=(algorithm.md_bos_grpo_min_domain_count="$MD_BOS_GRPO_MIN_DOMAIN_COUNT")
fi
if [[ "$ADV_ESTIMATOR" == "shared_private_multi_domain_bos_grpo" || "$ADV_ESTIMATOR" == "shared_private_md_bos_grpo" || "$ADV_ESTIMATOR" == "sp_md_bos_grpo" || "${EXTRA_SP_MD_BOS_GRPO_OVERRIDES:-0}" == "1" ]]; then
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_shared_k="$SP_MD_BOS_GRPO_SHARED_K")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_private_k="$SP_MD_BOS_GRPO_PRIVATE_K")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_domain_lambda="$SP_MD_BOS_GRPO_DOMAIN_LAMBDA")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_shared_noise_lambda="$SP_MD_BOS_GRPO_SHARED_NOISE_LAMBDA")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_private_noise_lambda="$SP_MD_BOS_GRPO_PRIVATE_NOISE_LAMBDA")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_weight_floor="$SP_MD_BOS_GRPO_WEIGHT_FLOOR")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_weight_power="$SP_MD_BOS_GRPO_WEIGHT_POWER")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_mix_with_vanilla="$SP_MD_BOS_GRPO_MIX_WITH_VANILLA")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_positive_eigs_only="$SP_MD_BOS_GRPO_POSITIVE_EIGS_ONLY")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_fallback_to_vanilla="$SP_MD_BOS_GRPO_FALLBACK_TO_VANILLA")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_include_feature_std="$SP_MD_BOS_GRPO_INCLUDE_FEATURE_STD")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_normalize_features="$SP_MD_BOS_GRPO_NORMALIZE_FEATURES")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_eps="$SP_MD_BOS_GRPO_EPS")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_domain_key="$SP_MD_BOS_GRPO_DOMAIN_KEY")
    EXTRA_OVERRIDES+=(algorithm.sp_md_bos_grpo_min_domain_count="$SP_MD_BOS_GRPO_MIN_DOMAIN_COUNT")
fi
if [[ "$ADV_ESTIMATOR" == "snr_multi_domain_grpo" || "$ADV_ESTIMATOR" == "snr_md_grpo" || "${EXTRA_SNR_MD_GRPO_OVERRIDES:-0}" == "1" ]]; then
    EXTRA_OVERRIDES+=(algorithm.snr_md_grpo_include_feature_std="$SNR_MD_GRPO_INCLUDE_FEATURE_STD")
    EXTRA_OVERRIDES+=(algorithm.snr_md_grpo_normalize_features="$SNR_MD_GRPO_NORMALIZE_FEATURES")
    EXTRA_OVERRIDES+=(algorithm.snr_md_grpo_eps="$SNR_MD_GRPO_EPS")
    EXTRA_OVERRIDES+=(algorithm.snr_md_grpo_domain_key="$SNR_MD_GRPO_DOMAIN_KEY")
    EXTRA_OVERRIDES+=(algorithm.snr_md_grpo_min_domain_count="$SNR_MD_GRPO_MIN_DOMAIN_COUNT")
fi

"$PYTHON" -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml' \
    algorithm.adv_estimator="$ADV_ESTIMATOR" \
    data.dataloader_num_workers=0 \
    data.seed="${SEED:-42}" \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    +data.multi_val_files.multihop="$MULTIDOMAIN_DIR/val_multihop.parquet" \
    +data.multi_val_files.math="$MULTIDOMAIN_DIR/val_math.parquet" \
    +data.multi_val_files.code="$MULTIDOMAIN_DIR/val_code.parquet" \
    data.train_batch_size=$TRAIN_BATCH_SIZE \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr=${ACTOR_LR:-1e-6} \
    actor_rollout_ref.actor.ppo_mini_batch_size=$PPO_MINI_BATCH_SIZE \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=${PPO_MICRO_BATCH_SIZE_PER_GPU:-1} \
    actor_rollout_ref.actor.policy_loss.loss_mode="$POLICY_LOSS_MODE" \
    actor_rollout_ref.actor.entropy_coeff=${ENTROPY_COEFF:-0} \
    actor_rollout_ref.actor.calculate_sum_pi_squared="$CALCULATE_SUM_PI_SQUARED" \
    actor_rollout_ref.actor.calculate_update_sketch="$CALCULATE_UPDATE_SKETCH" \
    actor_rollout_ref.actor.update_sketch_dim="$UPDATE_SKETCH_DIM" \
    actor_rollout_ref.actor.update_sketch_seed="$UPDATE_SKETCH_SEED" \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=${ROLLOUT_LOG_PROB_MICRO_BATCH_SIZE_PER_GPU:-1} \
    actor_rollout_ref.rollout.tensor_model_parallel_size=${ROLLOUT_TP_SIZE:-4} \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=${GPU_MEMORY_UTILIZATION:-0.6} \
    actor_rollout_ref.rollout.n=$ROLLOUT_N \
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=${ACTOR_TP_SIZE:-4} \
    actor_rollout_ref.actor.megatron.pipeline_model_parallel_size=${ACTOR_PP_SIZE:-1} \
    actor_rollout_ref.actor.megatron.context_parallel_size=${ACTOR_CP_SIZE:-1} \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=${REF_LOG_PROB_MICRO_BATCH_SIZE_PER_GPU:-2} \
    actor_rollout_ref.actor.megatron.use_mbridge=True \
    actor_rollout_ref.actor.megatron.param_offload=${ACTOR_PARAM_OFFLOAD:-True} \
    actor_rollout_ref.actor.megatron.optimizer_offload=${ACTOR_OPTIMIZER_OFFLOAD:-True} \
    actor_rollout_ref.actor.megatron.grad_offload=${ACTOR_GRAD_OFFLOAD:-True} \
    actor_rollout_ref.ref.megatron.param_offload=${REF_PARAM_OFFLOAD:-True} \
    actor_rollout_ref.actor.megatron.dist_ckpt_optim_fully_reshardable=False \
    trainer.use_legacy_worker_impl=disable \
    +actor_rollout_ref.rollout.multi_turn.use_seeupo=True \
    +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_offload_fraction=1 \
    +actor_rollout_ref.actor.optim.override_optimizer_config.overlap_cpu_optimizer_d2h_h2d=True \
    +actor_rollout_ref.actor.optim.override_optimizer_config.use_precision_aware_optimizer=True \
    +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_cpu_offload=True \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_method=uniform \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_granularity=full \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_num_layers=1 \
    +actor_rollout_ref.actor.megatron.override_transformer_config.gradient_accumulation_fusion=True \
    algorithm.use_kl_in_reward=False \
    trainer.critic_warmup=0 \
    trainer.logger="$TRAINER_LOGGER" \
    trainer.project_name=$SWANLAB_PROJECT \
    trainer.experiment_name=$SWANLAB_EXP_NAME \
    trainer.n_gpus_per_node=${N_GPUS_PER_NODE:-8} \
    trainer.rollout_data_dir="$ROLLOUT_DATA_DIR" \
    +trainer.validation_data_dir="$VAL_DATA_DIR" \
    trainer.nnodes=${NNODES:-1} \
    trainer.save_freq=${SAVE_FREQ:-10} \
    trainer.test_freq=${TEST_FREQ:-5} \
    trainer.total_epochs=${TOTAL_EPOCHS:-15} \
    trainer.default_local_dir="$CHECKPOINT_ROOT/${JOB_NAME}" \
    actor_rollout_ref.rollout.multi_turn.format=${MULTI_TURN_FORMAT:-qwen3_coder} \
    actor_rollout_ref.rollout.agent.default_agent_loop=single_turn_agent \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="$TOOL_CONFIG_PATH" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=${MAX_TOOL_RESPONSE_LENGTH:-2048} \
    actor_rollout_ref.rollout.multi_turn.max_user_turns=${MAX_USER_TURNS:-8} \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns=${MAX_ASSISTANT_TURNS:-8} \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    reward.custom_reward_function.path=null \
    reward.custom_reward_function.name=compute_score \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton \
    "${EXTRA_OVERRIDES[@]}"
