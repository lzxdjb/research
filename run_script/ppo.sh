set -x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

pkill -TERM -f 'agent-o3/watch_checkpoints_eval.py' || true
  pkill -TERM -f 'agent-o3/main_contrast_out.py' || true
  pkill -TERM -f 'vllm.entrypoints.openai.api_server' || true
  pkill -TERM -f 'vllm' || true
  pkill -TERM -f 'VLLM' || true
  sleep 5
  pkill -KILL -f 'agent-o3/watch_checkpoints_eval.py' || true
  pkill -KILL -f 'agent-o3/main_contrast_out.py' || true
  pkill -KILL -f 'vllm.entrypoints.openai.api_server' || true
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
PROJECT_DIR="$(pwd)"
mkdir -p "$PROJECT_DIR/output"

ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0

export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"

THSCC_TRAIN_CREATOR=wiki
JOB_NAME=${JOB_NAME:-wiki_mlp_qwen3_8b_megatron_critic}
POLICY_LOSS_MODE=${POLICY_LOSS_MODE:-vanilla}
CRITIC_VALUE_LOSS_WEIGHT_MODE=${CRITIC_VALUE_LOSS_WEIGHT_MODE:-none}
CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE=${CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE:-true}
CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN=${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN:-null}
CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX=${CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX:-null}
CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE=${CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE:-true}
CRITIC_VALUE_LOSS_WEIGHT_RHO=${CRITIC_VALUE_LOSS_WEIGHT_RHO:-1.0}
CRITIC_VALUE_LOSS_WEIGHT_ALPHA=${CRITIC_VALUE_LOSS_WEIGHT_ALPHA:-1.0}
TURN_LEVEL_VALUE=${TURN_LEVEL_VALUE:-False}
TURN_LEVEL_VALUE_ANCHOR=${TURN_LEVEL_VALUE_ANCHOR:-first}

wandb login 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
export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME

train_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/val_mix.parquet
test_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/val_mix.parquet
max_prompt_length=2000
max_response_length=512
max_token_len=$((max_prompt_length + max_response_length))
MODEL_PATH=./data/Qwen3-8B

pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0
pip install wikipedia
pip install sentence-transformers fastapi uvicorn

# ── Embedding server ──────────────────────────────────────────────────────────
EMBED_PORT=8780
fuser -k ${EMBED_PORT}/tcp 2>/dev/null || true

python -m verl.tools.embedding_server --model BAAI/bge-large-en-v1.5 --port $EMBED_PORT \
    > "$PROJECT_DIR/output/embedding_server.log" 2>&1 &
EMBED_PID=$!
echo "[embed] server PID=$EMBED_PID, waiting for startup..."

EMBED_HOST=${EMBED_HOST:-127.0.0.1}
EMBED_URL="http://${EMBED_HOST}:${EMBED_PORT}"

export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1,${EMBED_HOST}"
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}localhost,127.0.0.1,${EMBED_HOST}"

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

done

if (( embed_ready != 1 )); then
    echo "[embed] server did not become ready at ${EMBED_URL} within 60s. Last log lines:"
    tail -n 80 "$PROJECT_DIR/output/embedding_server.log"
    exit 1
fi

echo "[embed] embedding_server=${EMBED_URL}"
# ─────────────────────────────────────────────────────────────────────────────

export VLLM_RPC_TIMEOUT=3600
export NCCL_TIMEOUT=7200

MEMORY_INJECT_TOP_K=${MEMORY_INJECT_TOP_K:-0}
MEMORY_INJECT_MAX_CHARS=${MEMORY_INJECT_MAX_CHARS:-700}
MEMORY_VOTE_INJECTED=${MEMORY_VOTE_INJECTED:-True}
MEMORY_DISABLE_SEARCH_MEMORY=${MEMORY_DISABLE_SEARCH_MEMORY:-False}
VAL_DATA_DIR=${VAL_DATA_DIR:-val_log/reflect-4B}

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
    algorithm.adv_estimator=gae \
    algorithm.gamma=1.0 \
    algorithm.lam=1.0 \
    algorithm.turn_level_value=$TURN_LEVEL_VALUE \
    algorithm.turn_level_value_anchor=$TURN_LEVEL_VALUE_ANCHOR \
    data.dataloader_num_workers=0 \
    data.seed=42 \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    +data.multi_val_files.wiki2="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/test_2wiki.parquet" \
    +data.multi_val_files.bamboogle="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/test_bamboogle.parquet" \
    +data.multi_val_files.hotpotqa="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/test_hotpotqa.parquet" \
    +data.multi_val_files.musique="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/test_musique.parquet" \
    data.train_batch_size=32 \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path="$MODEL_PATH" \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=32 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.001 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.policy_loss.loss_mode=$POLICY_LOSS_MODE \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=4 \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6 \
    actor_rollout_ref.rollout.n=1 \
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
    critic.enable=True \
    critic.strategy=megatron \
    critic.model.path="$MODEL_PATH" \
    critic.megatron.tensor_model_parallel_size=4 \
    critic.megatron.pipeline_model_parallel_size=1 \
    critic.megatron.context_parallel_size=1 \
    critic.megatron.use_mbridge=True \
    critic.megatron.param_offload=True \
    critic.megatron.optimizer_offload=True \
    critic.megatron.grad_offload=True \
    critic.megatron.dist_ckpt_optim_fully_reshardable=False \
    critic.optim.lr=1e-5 \
    critic.ppo_micro_batch_size_per_gpu=1 \
    critic.value_loss_weight_mode=$CRITIC_VALUE_LOSS_WEIGHT_MODE \
    critic.value_loss_weight_normalize=$CRITIC_VALUE_LOSS_WEIGHT_NORMALIZE \
    critic.value_loss_weight_clip_min=$CRITIC_VALUE_LOSS_WEIGHT_CLIP_MIN \
    critic.value_loss_weight_clip_max=$CRITIC_VALUE_LOSS_WEIGHT_CLIP_MAX \
    critic.value_loss_weight_clip_renormalize=$CRITIC_VALUE_LOSS_WEIGHT_CLIP_RENORMALIZE \
    critic.value_loss_weight_rho=$CRITIC_VALUE_LOSS_WEIGHT_RHO \
    critic.value_loss_weight_alpha=$CRITIC_VALUE_LOSS_WEIGHT_ALPHA \
    +actor_rollout_ref.rollout.multi_turn.use_seeupo=True \
    +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_offload_fraction=1 \
    +actor_rollout_ref.actor.optim.override_optimizer_config.overlap_cpu_optimizer_d2h_h2d=True \
    +actor_rollout_ref.actor.optim.override_optimizer_config.use_precision_aware_optimizer=True \
    +actor_rollout_ref.actor.optim.override_optimizer_config.optimizer_cpu_offload=True \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_method=uniform \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_granularity=full \
    +actor_rollout_ref.actor.megatron.override_transformer_config.recompute_num_layers=1 \
    +actor_rollout_ref.actor.megatron.override_transformer_config.gradient_accumulation_fusion=True \
    +critic.megatron.override_transformer_config.recompute_method=uniform \
    +critic.megatron.override_transformer_config.recompute_granularity=full \
    +critic.megatron.override_transformer_config.recompute_num_layers=1 \
    +critic.megatron.override_transformer_config.gradient_accumulation_fusion=True \
    algorithm.use_kl_in_reward=False \
    trainer.critic_warmup=0 \
    trainer.logger='["console"]' \
    trainer.project_name=$SWANLAB_PROJECT \
    trainer.experiment_name=$SWANLAB_EXP_NAME \
    trainer.n_gpus_per_node=4 \
    +trainer.val_data_dir="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/val_log/${JOB_NAME}" \
    trainer.nnodes=1 \
    trainer.save_freq=5 \
    trainer.test_freq=5 \
    trainer.total_epochs=15 \
    trainer.default_local_dir="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/${JOB_NAME}" \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/examples/sglang_multiturn/config/tool_config/hot_stock_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=2048 \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton \
    +algorithm.mi_reward_coef=0.0 \
    +algorithm.mi_reward_num_negatives=31 \
    +algorithm.mi_reward_zscore_clip=2.0 \
    +algorithm.mi_reward_clip=0.02 \
    +algorithm.mi_reward_warmup_steps=50 \
    +algorithm.mi_reward_target_zscore=0.8 \
    +algorithm.mi_reward_success_threshold=0.0
