#!/usr/bin/env bash
set -euo pipefail
set -x

PROJECT_DIR=${PROJECT_DIR:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/research}
cd "$PROJECT_DIR"

ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0

export https_proxy="${https_proxy:-http://hexin:hx300033@10.217.180.65:30100}"
export http_proxy="${http_proxy:-http://hexin:hx300033@10.217.180.65:30100}"
NO_PROXY_DEFAULT="localhost,127.0.0.1,0.0.0.0,10.0.0.0/8,10.244.0.0/16,10.248.0.0/16"
export no_proxy="${no_proxy:-${NO_PROXY:-$NO_PROXY_DEFAULT}}"
export NO_PROXY="${NO_PROXY:-$no_proxy}"

THSCC_TRAIN_CREATOR=${THSCC_TRAIN_CREATOR:-mixed_agent_domains}
JOB_NAME=${JOB_NAME:-mixed_webshop_alfworld_multihop_state_agreement_grpo_avg_pass_val}

export WANDB_DIR=${WANDB_DIR:-$PROJECT_DIR/output/wandb}
mkdir -p "$WANDB_DIR" "$PROJECT_DIR/output"

WANDB_API_KEY=${WANDB_API_KEY:-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}
if [[ -n "$WANDB_API_KEY" ]]; then
    wandb login "$WANDB_API_KEY"
fi

export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME

MULTIHOP_TRAIN_FILE=${MULTIHOP_TRAIN_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/multihop_mix/train_mix.parquet}
ALFWORLD_TRAIN_FILE=${ALFWORLD_TRAIN_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/alfworld/alfworld_train.parquet}
WEBSHOP_TRAIN_FILE=${WEBSHOP_TRAIN_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/webshop/webshop_train.parquet}
MIXED_TRAIN_FILE=${MIXED_TRAIN_FILE:-$PROJECT_DIR/data/mixed_webshop_alfworld_multihop/train_mix.parquet}

MULTIHOP_2WIKI_VAL_FILE=${MULTIHOP_2WIKI_VAL_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/multihop_mix/test_2wiki.parquet}
MULTIHOP_BAMBOOGLE_VAL_FILE=${MULTIHOP_BAMBOOGLE_VAL_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/multihop_mix/test_bamboogle.parquet}
MULTIHOP_HOTPOTQA_VAL_FILE=${MULTIHOP_HOTPOTQA_VAL_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/multihop_mix/test_hotpotqa.parquet}
MULTIHOP_MUSIQUE_VAL_FILE=${MULTIHOP_MUSIQUE_VAL_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/multihop_mix/test_musique.parquet}
ALFWORLD_VAL_FILE=${ALFWORLD_VAL_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/alfworld/alfworld_test.parquet}
WEBSHOP_VAL_FILE=${WEBSHOP_VAL_FILE:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/webshop/webshop_test.parquet}

MIX_MULTIHOP_SAMPLES=${MIX_MULTIHOP_SAMPLES:--1}
MIX_ALFWORLD_SAMPLES=${MIX_ALFWORLD_SAMPLES:--1}
MIX_WEBSHOP_SAMPLES=${MIX_WEBSHOP_SAMPLES:--1}
MIX_SAMPLE_MODE=${MIX_SAMPLE_MODE:-random}
MIX_SEED=${MIX_SEED:-42}
REBUILD_MIXED_DATASET=${REBUILD_MIXED_DATASET:-0}

if [[ "$REBUILD_MIXED_DATASET" == "1" || "$REBUILD_MIXED_DATASET" == "true" || ! -f "$MIXED_TRAIN_FILE" ]]; then
    MIXER_EXTRA_ARGS=()
    if [[ "${MIX_ALLOW_REPEAT:-0}" == "1" || "${MIX_ALLOW_REPEAT:-0}" == "true" ]]; then
        MIXER_EXTRA_ARGS+=(--allow-repeat)
    fi
    if [[ "${MIX_NO_SHUFFLE:-0}" == "1" || "${MIX_NO_SHUFFLE:-0}" == "true" ]]; then
        MIXER_EXTRA_ARGS+=(--no-shuffle)
    fi
    python3 "$PROJECT_DIR/dataset_make_webshop_alfworld_multihop_mix.py" \
        --multihop-train "$MULTIHOP_TRAIN_FILE" \
        --alfworld-train "$ALFWORLD_TRAIN_FILE" \
        --webshop-train "$WEBSHOP_TRAIN_FILE" \
        --multihop-samples "$MIX_MULTIHOP_SAMPLES" \
        --alfworld-samples "$MIX_ALFWORLD_SAMPLES" \
        --webshop-samples "$MIX_WEBSHOP_SAMPLES" \
        --sample-mode "$MIX_SAMPLE_MODE" \
        --seed "$MIX_SEED" \
        --output "$MIXED_TRAIN_FILE" \
        "${MIXER_EXTRA_ARGS[@]}"
fi

: "${ALFWORLD_CLUSTER_0:=${ALFWORLD_SERVER_HOST:-10.248.100.118}:${ALFWORLD_BASE_PORT:-8700}:${ALFWORLD_NUM_SERVERS:-1}}"
: "${WEBSHOP_CLUSTER_0:=${WEBSHOP_SERVER_HOST:-10.244.138.102}:${WEBSHOP_BASE_PORT:-8800}:${WEBSHOP_NUM_SERVERS:-1}}"

check_and_export_clusters() {
    local prefix=$1
    local env_name=$2
    local cluster_vars=()
    local clusters=()
    mapfile -t cluster_vars < <(compgen -v | grep "^${prefix}_CLUSTER_" | sort)

    if [[ ${#cluster_vars[@]} -eq 0 ]]; then
        echo "ERROR: No ${prefix}_CLUSTER_* variables defined."
        exit 1
    fi

    local total=0
    local ready=0
    local failed_any=0
    local cluster host base_port num_servers port cluster_ready

    for var in "${cluster_vars[@]}"; do
        cluster=${!var}
        clusters+=("$cluster")
        IFS=':' read -r host base_port num_servers <<< "$cluster"
        echo "Checking ${prefix} cluster ${host}:${base_port} (${num_servers} servers)"
        cluster_ready=0
        for i in $(seq 0 $((num_servers - 1))); do
            port=$((base_port + i))
            if curl --noproxy '*' -sf "http://${host}:${port}/health" > /dev/null 2>&1; then
                cluster_ready=$((cluster_ready + 1))
            fi
            curl --noproxy '*' -s -X POST "http://${host}:${port}/recollect" > /dev/null 2>&1 || true
        done
        echo "  ${cluster_ready} / ${num_servers} ready"
        ready=$((ready + cluster_ready))
        total=$((total + num_servers))
        if [[ $cluster_ready -lt $num_servers ]]; then
            failed_any=1
        fi
    done

    echo "${prefix} total: ${ready} / ${total} servers reachable."
    if [[ $failed_any -ne 0 ]]; then
        echo "ERROR: Some ${prefix} servers are unreachable. Aborting."
        exit 1
    fi

    local clusters_json="["
    local sep=""
    for cluster in "${clusters[@]}"; do
        IFS=':' read -r host base_port num_servers <<< "$cluster"
        clusters_json+="${sep}{\"host\":\"${host}\",\"base_port\":${base_port},\"num_servers\":${num_servers}}"
        sep=","
    done
    clusters_json+="]"
    printf -v "$env_name" '%s' "$clusters_json"
    export "$env_name"
    echo "${env_name}=${clusters_json}"
}

check_and_export_clusters "ALFWORLD" "ALFWORLD_CLUSTERS"
check_and_export_clusters "WEBSHOP" "WEBSHOP_CLUSTERS"

max_prompt_length=${MAX_PROMPT_LENGTH:-2000}
max_response_length=${MAX_RESPONSE_LENGTH:-8000}
max_token_len=$((max_prompt_length + max_response_length))

SKIP_PIP_INSTALL=${SKIP_PIP_INSTALL:-0}
if [[ "$SKIP_PIP_INSTALL" != "1" && "$SKIP_PIP_INSTALL" != "true" ]]; then
    pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0
    pip install wikipedia
    pip install sentence-transformers fastapi uvicorn
fi

export VLLM_RPC_TIMEOUT=${VLLM_RPC_TIMEOUT:-3600}
export NCCL_TIMEOUT=${NCCL_TIMEOUT:-7200}

STATE_PREDICTIVE_MAX_SEGMENT_LEN=${STATE_PREDICTIVE_MAX_SEGMENT_LEN:-512}
STATE_PREDICTIVE_MIN_SEGMENT_LEN=${STATE_PREDICTIVE_MIN_SEGMENT_LEN:-2}
STATE_PREDICTIVE_LOSS_TYPE=${STATE_PREDICTIVE_LOSS_TYPE:-state_level}
STATE_PREDICTIVE_RATIO_MODE=${STATE_PREDICTIVE_RATIO_MODE:-geo_mean}
STATE_PREDICTIVE_OBJECTIVE=${STATE_PREDICTIVE_OBJECTIVE:-agreement}
STATE_PREDICTIVE_AGREEMENT_SCORE_MODE=${STATE_PREDICTIVE_AGREEMENT_SCORE_MODE:-raw}
STATE_PREDICTIVE_SEGMENT_BACKEND=${STATE_PREDICTIVE_SEGMENT_BACKEND:-torch}
STATE_PREDICTIVE_PRECOMPUTE_STATE_INDEX=${STATE_PREDICTIVE_PRECOMPUTE_STATE_INDEX:-True}
STATE_PREDICTIVE_USE_UPDATE_SKETCH=${STATE_PREDICTIVE_USE_UPDATE_SKETCH:-True}
STATE_PREDICTIVE_NORMALIZE_FEATURES=${STATE_PREDICTIVE_NORMALIZE_FEATURES:-True}
CALCULATE_UPDATE_SKETCH=${CALCULATE_UPDATE_SKETCH:-$STATE_PREDICTIVE_USE_UPDATE_SKETCH}
UPDATE_SKETCH_DIM=${UPDATE_SKETCH_DIM:-64}
UPDATE_SKETCH_SEED=${UPDATE_SKETCH_SEED:-17}
POLICY_LOSS_MODE=${POLICY_LOSS_MODE:-vanilla}

VAL_ROLLOUT_N=${VAL_ROLLOUT_N:-4}
VAL_DO_SAMPLE=${VAL_DO_SAMPLE:-True}
VAL_TEMPERATURE=${VAL_TEMPERATURE:-1.0}
VAL_TOP_P=${VAL_TOP_P:-0.7}
VAL_TOP_K=${VAL_TOP_K:--1}
VAL_BATCH_SIZE=${VAL_BATCH_SIZE:-}
VAL_BATCH_SIZE_ARGS=()
if [[ -n "$VAL_BATCH_SIZE" ]]; then
    VAL_BATCH_SIZE_ARGS+=(data.val_batch_size=$VAL_BATCH_SIZE)
fi

MODEL_PATH=${MODEL_PATH:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B}
TRAIN_BATCH_SIZE=${TRAIN_BATCH_SIZE:-32}
PPO_MINI_BATCH_SIZE=${PPO_MINI_BATCH_SIZE:-32}
PPO_MICRO_BATCH_SIZE_PER_GPU=${PPO_MICRO_BATCH_SIZE_PER_GPU:-8}
LOG_PROB_MICRO_BATCH_SIZE_PER_GPU=${LOG_PROB_MICRO_BATCH_SIZE_PER_GPU:-8}
REF_LOG_PROB_MICRO_BATCH_SIZE_PER_GPU=${REF_LOG_PROB_MICRO_BATCH_SIZE_PER_GPU:-8}
ROLLOUT_N=${ROLLOUT_N:-8}
ROLLOUT_TP=${ROLLOUT_TP:-2}
N_GPUS_PER_NODE=${N_GPUS_PER_NODE:-8}
TP=${TP:-2}
PP=${PP:-1}
CP=${CP:-1}
EP=${EP:-8}
ETP=${ETP:-1}

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

HOTPOT_TOOL_CONFIG=${HOTPOT_TOOL_CONFIG:-$PROJECT_DIR/examples/sglang_multiturn/config/tool_config/hot_stock_tool_config.yaml}
ALFWORLD_TOOL_CONFIG=${ALFWORLD_TOOL_CONFIG:-$PROJECT_DIR/examples/sglang_multiturn/config/tool_config/alfworld_tool_config.yaml}
WEBSHOP_TOOL_CONFIG=${WEBSHOP_TOOL_CONFIG:-$PROJECT_DIR/examples/sglang_multiturn/config/tool_config/webshop_tool_config.yaml}

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
    data.dataloader_num_workers=0 \
    data.seed=$MIX_SEED \
    data.train_files="$MIXED_TRAIN_FILE" \
    data.val_files="$WEBSHOP_VAL_FILE" \
    +data.multi_val_files.multihop_2wiki="$MULTIHOP_2WIKI_VAL_FILE" \
    +data.multi_val_files.multihop_bamboogle="$MULTIHOP_BAMBOOGLE_VAL_FILE" \
    +data.multi_val_files.multihop_hotpotqa="$MULTIHOP_HOTPOTQA_VAL_FILE" \
    +data.multi_val_files.multihop_musique="$MULTIHOP_MUSIQUE_VAL_FILE" \
    +data.multi_val_files.alfworld="$ALFWORLD_VAL_FILE" \
    +data.multi_val_files.webshop="$WEBSHOP_VAL_FILE" \
    data.train_batch_size=$TRAIN_BATCH_SIZE \
    "${VAL_BATCH_SIZE_ARGS[@]}" \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.actor.optim.lr=${ACTOR_LR:-1e-6} \
    actor_rollout_ref.actor.ppo_mini_batch_size=$PPO_MINI_BATCH_SIZE \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=$PPO_MICRO_BATCH_SIZE_PER_GPU \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.policy_loss.loss_mode=$POLICY_LOSS_MODE \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=$LOG_PROB_MICRO_BATCH_SIZE_PER_GPU \
    actor_rollout_ref.rollout.tensor_model_parallel_size=$ROLLOUT_TP \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=${GPU_MEMORY_UTILIZATION:-0.8} \
    actor_rollout_ref.rollout.n=$ROLLOUT_N \
    actor_rollout_ref.rollout.val_kwargs.n=$VAL_ROLLOUT_N \
    actor_rollout_ref.rollout.val_kwargs.do_sample=$VAL_DO_SAMPLE \
    actor_rollout_ref.rollout.val_kwargs.temperature=$VAL_TEMPERATURE \
    actor_rollout_ref.rollout.val_kwargs.top_p=$VAL_TOP_P \
    actor_rollout_ref.rollout.val_kwargs.top_k=$VAL_TOP_K \
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=$TP \
    actor_rollout_ref.actor.megatron.pipeline_model_parallel_size=$PP \
    actor_rollout_ref.actor.megatron.context_parallel_size=$CP \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=$REF_LOG_PROB_MICRO_BATCH_SIZE_PER_GPU \
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
    trainer.n_gpus_per_node=$N_GPUS_PER_NODE \
    +trainer.val_data_dir=${VAL_DATA_DIR:-$PROJECT_DIR/output/val_log/${JOB_NAME}} \
    trainer.nnodes=${NNODES:-1} \
    trainer.save_freq=${SAVE_FREQ:-5} \
    trainer.test_freq=${TEST_FREQ:-5} \
    trainer.total_epochs=${TOTAL_EPOCHS:-15} \
    trainer.default_local_dir=${CHECKPOINT_DIR:-$PROJECT_DIR/output/checkpoints/${JOB_NAME}} \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="$HOTPOT_TOOL_CONFIG" \
    +actor_rollout_ref.rollout.multi_turn.tool_config_paths.hotpot_qa_agent="$HOTPOT_TOOL_CONFIG" \
    +actor_rollout_ref.rollout.multi_turn.tool_config_paths.alfworld_agent="$ALFWORLD_TOOL_CONFIG" \
    +actor_rollout_ref.rollout.multi_turn.tool_config_paths.webshop_agent="$WEBSHOP_TOOL_CONFIG" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=${MAX_TOOL_RESPONSE_LENGTH:-2048} \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton
