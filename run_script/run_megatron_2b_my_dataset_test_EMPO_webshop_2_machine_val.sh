# set -x
pkill -9 -f VLLM
ray stop
pkill -f uvicorn
pkill -f "uvicorn alfworld_server:app"
# source /cpfs01/nlp/leizhengxing/stock-rl/miniconda3/bin/activate
# conda activate alfworld
# export ALFWORLD_DATA=/cpfs01/nlp/leizhengxing/stock-rl/data/alfworld_env_data/alfworld

# BASE_PORT=8800
# NUM_SERVERS=16

# echo "Starting $NUM_SERVERS ALFWorld servers from port $BASE_PORT..."

# for i in $(seq 0 $((NUM_SERVERS - 1))); do
#     PORT=$((BASE_PORT + i))
#     PORT=$PORT uvicorn alfworld_server:app \
#         --host 0.0.0.0 \
#         --port $PORT \
#         --workers 1 &
# done

# echo "All $NUM_SERVERS servers launching in background."
# echo "Waiting for them to be ready..."

# # Wait until all are healthy
# READY=0
# while [ $READY -lt $NUM_SERVERS ]; do
#     READY=0
#     for i in $(seq 0 $((NUM_SERVERS - 1))); do
#         PORT=$((BASE_PORT + i))
#         if curl -sf http://localhost:$PORT/health > /dev/null 2>&1; then
#             READY=$((READY + 1))
#         fi
#     done
#     echo "$READY / $NUM_SERVERS ready..."
#     sleep 5
# done

# echo "All servers ready. Starting training..."
# conda deactivate
# conda deactivate



# Set these to point to machine B
export WEBSHOP_SERVER_HOST="10.248.100.212"   # e.g. 192.168.1.42
export WEBSHOP_BASE_PORT=8900
export WEBSHOP_NUM_SERVERS=64

# Verify connectivity before training
echo "Checking connectivity to remote servers..."
READY=0
for i in $(seq 0 $((WEBSHOP_NUM_SERVERS - 1))); do
    PORT=$((WEBSHOP_BASE_PORT + i))
    if curl -sf http://${WEBSHOP_SERVER_HOST}:${PORT}/health > /dev/null 2>&1; then
        READY=$((READY + 1))
    fi
done
echo "$READY / $WEBSHOP_NUM_SERVERS servers reachable."

if [ $READY -lt $WEBSHOP_NUM_SERVERS ]; then
    echo "ERROR: Not all servers reachable. Aborting."
    exit 1
fi

PROJECT_DIR="$(pwd)"
ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1 # For megatron communication/computation overlapping
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0 # for vllm0.11.0 with TP
THSCC_TRAIN_CREATOR=lzx
JOB_NAME=test

# pip install swanlab
# swanlab login --host http://10.244.209.251:8000 -k CRlhKr9zstqX9RLGOLV0V
pip install transformers==5.3.0 flash-linear-attention
pip install wikipedia
train_path=/cpfs01/nlp/leizhengxing/stock-rl/data/webshop/webshop_test.parquet
test_path=/cpfs01/nlp/leizhengxing/stock-rl/data/webshop/webshop_test.parquet
max_prompt_length=1000
max_response_length=4000
max_token_len=$((max_prompt_length + max_response_length))
export CUDA_VISIBLE_DEVICES=0,1

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
    algorithm.adv_estimator=grpo \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    data.train_batch_size=8 \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path=/cpfs01/nlp/leizhengxing/stock-rl/data/Qwen3.5-4B \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=8 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.actor.use_kl_loss=False \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=2 \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6 \
    actor_rollout_ref.rollout.n=1 \
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=2 \
    actor_rollout_ref.actor.megatron.pipeline_model_parallel_size=1 \
    actor_rollout_ref.actor.megatron.context_parallel_size=1 \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.actor.megatron.use_mbridge=True \
    actor_rollout_ref.actor.megatron.param_offload=True \
    actor_rollout_ref.actor.megatron.optimizer_offload=True \
    actor_rollout_ref.actor.megatron.grad_offload=True \
    actor_rollout_ref.ref.megatron.param_offload=True \
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
    trainer.logger='["console"]' \
    trainer.project_name=$SWANLAB_PROJECT \
    trainer.experiment_name=$SWANLAB_EXP_NAME \
    trainer.n_gpus_per_node=2 \
    trainer.nnodes=1 \
    trainer.save_freq=-1 \
    trainer.test_freq=-1 \
    +trainer.val_data_dir=val_log/webshop-4B-test \
    +trainer.validate_before_train=True \
    +trainer.val_only=True \
    trainer.total_epochs=15 \
    trainer.use_legacy_worker_impl=disable \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    +actor_rollout_ref.rollout.multi_turn.use_seeupo=True \
    actor_rollout_ref.actor.policy_loss.loss_mode=empo_new \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="$PROJECT_DIR/examples/sglang_multiturn/config/tool_config/webshop_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=2048 \
    2>&1 | tee text.txt &

