set -x
cd /mnt/code/stock-rl-reflect
rm -rf /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/output/wiki_reflect_search_with_mask
PROJECT_DIR="$(pwd)"
mkdir -p "$PROJECT_DIR/output"
ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1 # For megatron communication/computation overlapping
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0 # for vllm0.11.0 with TP
THSCC_TRAIN_CREATOR=wiki
JOB_NAME=wiki_reflect_search_with_mask

wandb login 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
export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME
export CUDA_VISIBLE_DEVICES=4,5,6,7
train_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/train_mix.jsonl
test_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/val_mix.jsonl
max_prompt_length=1100
max_response_length=6000
max_token_len=$((max_prompt_length + max_response_length))
pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0
pip install wikipedia
pip install sentence-transformers fastapi uvicorn

# ── Embedding server ──────────────────────────────────────────────────────────
EMBED_PORT=8780
python -m verl.tools.embedding_server --model BAAI/bge-large-en-v1.5 --port $EMBED_PORT \
    > "$PROJECT_DIR/output/embedding_server.log" 2>&1 &
EMBED_PID=$!
echo "[embed] server PID=$EMBED_PID, waiting for startup..."

# Wait until the health endpoint responds (up to 60 s)
EMBED_HOST=$(hostname -I | awk '{print $1}')
EMBED_URL="http://${EMBED_HOST}:${EMBED_PORT}"
embed_deadline=$((SECONDS + 100))
while (( SECONDS < embed_deadline )); do
    if curl -sf "${EMBED_URL}/health" > /dev/null 2>&1; then
        echo "[embed] server ready at ${EMBED_URL}"
        break
    fi
    sleep 5
done
echo "[embed] embedding_server=${EMBED_URL}  (set this in your memory config if reusing)"
# ─────────────────────────────────────────────────────────────────────────────

export VLLM_RPC_TIMEOUT=3600  # seconds, default is usually 60
export NCCL_TIMEOUT=7200

MEMORY_INJECT_TOP_K=${MEMORY_INJECT_TOP_K:-0}
MEMORY_INJECT_MAX_CHARS=${MEMORY_INJECT_MAX_CHARS:-700}
MEMORY_VOTE_INJECTED=${MEMORY_VOTE_INJECTED:-True}
MEMORY_DISABLE_SEARCH_MEMORY=${MEMORY_DISABLE_SEARCH_MEMORY:-False}
VAL_DATA_DIR=${VAL_DATA_DIR:-val_log/reflect-4B}


python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
    algorithm.adv_estimator=grpo \
    data.dataloader_num_workers=0 \
    data.seed=42 \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    +data.multi_val_files.wiki2="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_2wiki.parquet" \
    +data.multi_val_files.bamboogle="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_bamboogle.parquet" \
    +data.multi_val_files.hotpotqa="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_hotpotqa.parquet" \
    +data.multi_val_files.musique="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix_reflect/test_musique.parquet" \
    data.train_batch_size=64 \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B \
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
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=4 \
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
    +trainer.val_data_dir=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/val_log/wiki_reflect_search_with_mask \
    trainer.nnodes=1 \
    trainer.save_freq=5 \
    trainer.test_freq=5 \
    trainer.total_epochs=15 \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    trainer.default_local_dir=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/wiki_reflect_search_with_mask \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/examples/sglang_multiturn/config/tool_config/hot_reflect_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/examples/sglang_multiturn/config/tool_config/hot_reflect_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=2048 \
     +actor_rollout_ref.rollout.multi_turn.memory.save_dir="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/output/wiki_reflect_search_with_mask" \
    +actor_rollout_ref.rollout.multi_turn.memory.max_entries=100 \
    +actor_rollout_ref.rollout.multi_turn.memory.prune_threshold=-1 \
    +actor_rollout_ref.rollout.multi_turn.memory.dedup_threshold=0.92 \
    +actor_rollout_ref.rollout.multi_turn.memory.embedding_server="$EMBED_URL" \
    +actor_rollout_ref.rollout.multi_turn.memory.inject_top_k=0 \
    +actor_rollout_ref.rollout.multi_turn.memory.inject_min_score=100 \
    +actor_rollout_ref.rollout.multi_turn.memory.inject_max_chars=1024 \
    +actor_rollout_ref.rollout.multi_turn.memory.vote_injected="$MEMORY_VOTE_INJECTED" \
    +actor_rollout_ref.rollout.multi_turn.memory.disable_search_memory=False \
    +actor_rollout_ref.rollout.multi_turn.memory.paired_rollout=True \
    +actor_rollout_ref.rollout.multi_turn.memory.paired_force_search_memory=False \
    +actor_rollout_ref.rollout.multi_turn.memory.paired_nonpositive_vote_delta=0 \
    +actor_rollout_ref.rollout.multi_turn.memory.mask_reflection=True \
    +actor_rollout_ref.rollout.multi_turn.memory.reflect_prompt_path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/reflect_prompt.md" \
    +actor_rollout_ref.rollout.multi_turn.memory.reflect_temperature=0.7 \
    +actor_rollout_ref.rollout.multi_turn.memory.reflect_max_tokens=512 \


    
