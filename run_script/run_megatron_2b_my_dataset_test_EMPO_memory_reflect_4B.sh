# set -x
pkill -9 -f VLLM
ray stop
pkill -f uvicorn
pkill -f "uvicorn alfworld_server:app"
pkill -f "verl.tools.embedding_server" 2>/dev/null || true
fuser -k 8766/tcp 2>/dev/null || true

PROJECT_DIR="$(pwd)"
mkdir -p "$PROJECT_DIR/output"
ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1 # For megatron communication/computation overlapping
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0 # for vllm0.11.0 with TP
SWANLAB_PROJECT=wiki
SWANLAB_EXP_NAME=test
wandb login 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


export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"  
# pip install swanlab
# swanlab login --host http://10.244.209.251:8000 -k CRlhKr9zstqX9RLGOLV0V
pip install transformers==5.3.0 flash-linear-attention
pip install wikipedia
pip install sentence-transformers fastapi uvicorn

# ── Embedding server ──────────────────────────────────────────────────────────
EMBED_PORT=8766
python -m verl.tools.embedding_server --model BAAI/bge-large-en-v1.5 --port $EMBED_PORT \
    > "$PROJECT_DIR/output/embedding_server.log" 2>&1 &
EMBED_PID=$!
echo "[embed] server PID=$EMBED_PID, waiting for startup..."

# Use loopback by default so local embedding requests do not go through http_proxy.
# Set EMBED_HOST=<node-ip> before running this script if remote workers need it.
EMBED_HOST=${EMBED_HOST:-127.0.0.1}
EMBED_URL="http://${EMBED_HOST}:${EMBED_PORT}"
export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1,${EMBED_HOST}"
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}localhost,127.0.0.1,${EMBED_HOST}"

# Wait until the health endpoint responds (up to 60 s)
embed_deadline=$((SECONDS + 60))
embed_ready=0
while (( SECONDS < embed_deadline )); do
    if curl --noproxy "*" -sf "${EMBED_URL}/health" > /dev/null 2>&1; then
        echo "[embed] server ready at ${EMBED_URL}"
        embed_ready=1
        break
    fi
    if ! kill -0 "$EMBED_PID" > /dev/null 2>&1; then
        echo "[embed] server exited before it became ready. Last log lines:"
        tail -n 80 "$PROJECT_DIR/output/embedding_server.log"
        exit 1
    fi
    sleep 1
done
if (( embed_ready != 1 )); then
    echo "[embed] server did not become ready at ${EMBED_URL} within 60s. Last log lines:"
    tail -n 80 "$PROJECT_DIR/output/embedding_server.log"
    exit 1
fi
echo "[embed] embedding_server=${EMBED_URL}  (set this in your memory config if reusing)"
# ─────────────────────────────────────────────────────────────────────────────
train_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/val_mix.parquet
test_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/val_mix.parquet
max_prompt_length=2000
max_response_length=8000
max_token_len=$((max_prompt_length + max_response_length))
TP=${TP:-4}
PP=${PP:-1}
CP=${CP:-1}
EP=${EP:-8}
ETP=${ETP:-1}
GEN_TP=${GEN_TP:-4}
MEMORY_INJECT_TOP_K=${MEMORY_INJECT_TOP_K:-0}
MEMORY_INJECT_MIN_SCORE=${MEMORY_INJECT_MIN_SCORE:-2}
MEMORY_INJECT_MAX_CHARS=${MEMORY_INJECT_MAX_CHARS:-700}
MEMORY_VOTE_INJECTED=${MEMORY_VOTE_INJECTED:-True}
MEMORY_DISABLE_SEARCH_MEMORY=${MEMORY_DISABLE_SEARCH_MEMORY:-False}
MEMORY_PAIRED_ROLLOUT=${MEMORY_PAIRED_ROLLOUT:-False}
MEMORY_PAIRED_FORCE_SEARCH_MEMORY=${MEMORY_PAIRED_FORCE_SEARCH_MEMORY:-True}
MEMORY_PAIRED_NONPOSITIVE_VOTE_DELTA=${MEMORY_PAIRED_NONPOSITIVE_VOTE_DELTA:-0}
MEMORY_PAIRED_DELTA_REWARD_COEF=${MEMORY_PAIRED_DELTA_REWARD_COEF:-0.0}
MEMORY_PRUNE_MIN_USES=${MEMORY_PRUNE_MIN_USES:-4}
MEMORY_PRUNE_MEAN_DELTA_THRESHOLD=${MEMORY_PRUNE_MEAN_DELTA_THRESHOLD:-0.0}
MEMORY_DELTA_EMA_ALPHA=${MEMORY_DELTA_EMA_ALPHA:-0.2}
MEMORY_SEARCH_TOP_K=${MEMORY_SEARCH_TOP_K:-0}
MEMORY_SEARCH_MIN_SIMILARITY=${MEMORY_SEARCH_MIN_SIMILARITY:-0.0}
MEMORY_SEARCH_CANDIDATE_MULTIPLIER=${MEMORY_SEARCH_CANDIDATE_MULTIPLIER:-4}
MEMORY_MASK_REFLECTION=${MEMORY_MASK_REFLECTION:-True}
VAL_DATA_DIR=${VAL_DATA_DIR:-val_log/reflect-4B}

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
    algorithm.adv_estimator=grpo \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    +data.multi_val_files.wiki2="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_2wiki.parquet" \
    +data.multi_val_files.bamboogle="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_bamboogle.parquet" \
    +data.multi_val_files.hotpotqa="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_hotpotqa.parquet" \
    +data.multi_val_files.musique="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_musique.parquet" \
    data.train_batch_size=8 \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3.5-0.8B" \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=4 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.01 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=4 \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.8 \
    actor_rollout_ref.rollout.n=1 \
    trainer.use_legacy_worker_impl=disable \
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=4 \
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
    +trainer.val_data_dir="$VAL_DATA_DIR" \
    +trainer.validate_before_train=True \
    +trainer.val_only=True \
    trainer.logger='["console","wandb"]' \
    trainer.project_name=$SWANLAB_PROJECT \
    trainer.experiment_name=$SWANLAB_EXP_NAME \
    trainer.n_gpus_per_node=4 \
    trainer.nnodes=1 \
    trainer.save_freq=20 \
    trainer.test_freq=5 \
    trainer.total_epochs=15 \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="$PROJECT_DIR/examples/sglang_multiturn/config/tool_config/hot_reflect_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=2048 \
    +actor_rollout_ref.rollout.multi_turn.memory.save_dir="$PROJECT_DIR/output/hotpot_memory" \
    +actor_rollout_ref.rollout.multi_turn.memory.max_entries=100 \
    +actor_rollout_ref.rollout.multi_turn.memory.prune_threshold=-1 \
    +actor_rollout_ref.rollout.multi_turn.memory.dedup_threshold=0.92 \
    +actor_rollout_ref.rollout.multi_turn.memory.prune_min_uses="$MEMORY_PRUNE_MIN_USES" \
    +actor_rollout_ref.rollout.multi_turn.memory.prune_mean_delta_threshold="$MEMORY_PRUNE_MEAN_DELTA_THRESHOLD" \
    +actor_rollout_ref.rollout.multi_turn.memory.delta_ema_alpha="$MEMORY_DELTA_EMA_ALPHA" \
    +actor_rollout_ref.rollout.multi_turn.memory.search_top_k="$MEMORY_SEARCH_TOP_K" \
    +actor_rollout_ref.rollout.multi_turn.memory.search_min_similarity="$MEMORY_SEARCH_MIN_SIMILARITY" \
    +actor_rollout_ref.rollout.multi_turn.memory.search_candidate_multiplier="$MEMORY_SEARCH_CANDIDATE_MULTIPLIER" \
    +actor_rollout_ref.rollout.multi_turn.memory.embedding_server="$EMBED_URL" \
    +actor_rollout_ref.rollout.multi_turn.memory.inject_top_k=0 \
    +actor_rollout_ref.rollout.multi_turn.memory.inject_min_score=100 \
    +actor_rollout_ref.rollout.multi_turn.memory.inject_max_chars=1024 \
    +actor_rollout_ref.rollout.multi_turn.memory.vote_injected="$MEMORY_VOTE_INJECTED" \
    +actor_rollout_ref.rollout.multi_turn.memory.disable_search_memory=False \
    +actor_rollout_ref.rollout.multi_turn.memory.paired_rollout=True \
    +actor_rollout_ref.rollout.multi_turn.memory.paired_force_search_memory="$MEMORY_PAIRED_FORCE_SEARCH_MEMORY" \
    +actor_rollout_ref.rollout.multi_turn.memory.paired_nonpositive_vote_delta="$MEMORY_PAIRED_NONPOSITIVE_VOTE_DELTA" \
    +actor_rollout_ref.rollout.multi_turn.memory.paired_delta_reward_coef="$MEMORY_PAIRED_DELTA_REWARD_COEF" \
    +actor_rollout_ref.rollout.multi_turn.memory.mask_reflection=False \
    +actor_rollout_ref.rollout.multi_turn.memory.reflect_prompt_path="$PROJECT_DIR/reflect_prompt.md" \
    +actor_rollout_ref.rollout.multi_turn.memory.reflect_temperature=0.7 \
    +actor_rollout_ref.rollout.multi_turn.memory.reflect_max_tokens=512 \
    data.shuffle=False \
    2>&1 | tee text_30.txt &



# MEMORY_PAIRED_ROLLOUT=True \
# MEMORY_INJECT_TOP_K=0 \
# MEMORY_DISABLE_SEARCH_MEMORY=False \
# MEMORY_PAIRED_FORCE_SEARCH_MEMORY=True \
# MEMORY_PAIRED_NONPOSITIVE_VOTE_DELTA=0 \
# MEMORY_MASK_REFLECTION=True \
# VAL_DATA_DIR=val_log/paired-memory-advantage \
# bash run_megatron_2b_my_dataset_test_EMPO_memory_reflect_4B.sh
