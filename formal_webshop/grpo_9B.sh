set -x
WEBSHOP_CLUSTER_0="10.248.100.156:8800:1"

# Add more: WEBSHOP_CLUSTER_3="host:base_port:num_servers"

# ── Build cluster list from WEBSHOP_CLUSTER_* env vars ──────────────────────
CLUSTERS=()
for var in $(compgen -v | grep '^WEBSHOP_CLUSTER_' | sort); do
    CLUSTERS+=("${!var}")
done

if [ ${#CLUSTERS[@]} -eq 0 ]; then
    echo "ERROR: No clusters defined. Set WEBSHOP_CLUSTER_0, WEBSHOP_CLUSTER_1, ..."
    exit 1
fi

echo "Checking ${#CLUSTERS[@]} cluster(s)..."

TOTAL=0
READY=0
FAILED_ANY=0

for cluster in "${CLUSTERS[@]}"; do
    IFS=':' read -r HOST BASE_PORT NUM_SERVERS <<< "$cluster"
    echo ""
    echo "  Cluster $HOST:$BASE_PORT (${NUM_SERVERS} servers)"
    cluster_ready=0
    for i in $(seq 0 $((NUM_SERVERS - 1))); do
        PORT=$((BASE_PORT + i))
        if curl -sf http://${HOST}:${PORT}/health > /dev/null 2>&1; then
            cluster_ready=$((cluster_ready + 1))
        fi
        curl -s -X POST http://${HOST}:${PORT}/recollect > /dev/null

    done
    echo "  → $cluster_ready / $NUM_SERVERS ready"
    READY=$((READY + cluster_ready))
    TOTAL=$((TOTAL + NUM_SERVERS))
    if [ $cluster_ready -lt $NUM_SERVERS ]; then
        FAILED_ANY=1
    fi
done

echo ""
echo "Total: $READY / $TOTAL servers reachable across all clusters."

if [ $FAILED_ANY -ne 0 ]; then
    echo "ERROR: Some servers unreachable. Aborting."
    exit 1
fi

# ── Export WEBSHOP_CLUSTERS JSON for the Python client ──────────────────────
CLUSTERS_JSON="["
SEP=""
for cluster in "${CLUSTERS[@]}"; do
    IFS=':' read -r HOST BASE_PORT NUM_SERVERS <<< "$cluster"
    CLUSTERS_JSON+="${SEP}{\"host\":\"${HOST}\",\"base_port\":${BASE_PORT},\"num_servers\":${NUM_SERVERS}}"
    SEP=","
done
CLUSTERS_JSON+="]"

export WEBSHOP_CLUSTERS="$CLUSTERS_JSON"
echo "WEBSHOP_CLUSTERS=$WEBSHOP_CLUSTERS"
echo "All servers reachable. Ready to train."


PROJECT_DIR="$(pwd)"
ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1 # For megatron communication/computation overlapping
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0 # for vllm0.11.0 with TP
THSCC_TRAIN_CREATOR=webshop
JOB_NAME=webshop_grpo
pip install wikipedia

wandb login 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
export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME

train_path=/cpfs01/nlp/leizhengxing/stock-rl/data/webshop/webshop_train.parquet
test_path=/cpfs01/nlp/leizhengxing/stock-rl/data/webshop/webshop_test.parquet
max_prompt_length=1000
max_response_length=8000
max_token_len=$((max_prompt_length + max_response_length))
pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0

export VLLM_RPC_TIMEOUT=3600  # seconds, default is usually 60
export NCCL_TIMEOUT=7200
cd /cpfs01/nlp/leizhengxing/code/stock-rl
python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
    algorithm.adv_estimator=grpo \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    data.train_batch_size=32 \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path=/cpfs01/nlp/leizhengxing/stock-rl/data/Qwen3.5-9B \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=32 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=4 \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6 \
    actor_rollout_ref.rollout.n=8 \
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=2 \
    actor_rollout_ref.actor.megatron.pipeline_model_parallel_size=1 \
    actor_rollout_ref.actor.megatron.context_parallel_size=1 \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.actor.megatron.use_mbridge=True \
    actor_rollout_ref.actor.megatron.param_offload=True \
    actor_rollout_ref.actor.megatron.optimizer_offload=True \
    actor_rollout_ref.actor.megatron.grad_offload=True \
    actor_rollout_ref.ref.megatron.param_offload=True \
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
    trainer.logger='["console","wandb"]' \
    trainer.project_name=$SWANLAB_PROJECT \
    trainer.experiment_name=$SWANLAB_EXP_NAME \
    trainer.n_gpus_per_node=4 \
    +trainer.val_data_dir=/cpfs01/nlp/leizhengxing/stock-rl/val_log/webshop_grpo \
    trainer.nnodes=1 \
    trainer.save_freq=5 \
    trainer.test_freq=5 \
    trainer.total_epochs=15 \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    trainer.default_local_dir=/cpfs01/nlp/leizhengxing/stock-rl/checkpoint/webshop_grpo \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="/cpfs01/nlp/leizhengxing/code/stock-rl/examples/sglang_multiturn/config/tool_config/webshop_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=2048 \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton


    


