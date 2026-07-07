set -x
cd /mnt/code/stock-rl-reflect
export WANDB_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/wandb_real

PROJECT_DIR="$(pwd)"
mkdir -p "$PROJECT_DIR/output"

ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0


THSCC_TRAIN_CREATOR=wiki
JOB_NAME=8_lantent_newest

wandb login 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
export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME

train_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/train_mix.jsonl
test_path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/multihop_mix/val_mix.jsonl
max_prompt_length=1100
max_response_length=6000
max_token_len=$((max_prompt_length + max_response_length))

pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0
pip install wikipedia
pip install sentence-transformers fastapi uvicorn

export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"

export VLLM_RPC_TIMEOUT=3600
export NCCL_TIMEOUT=7200

MEMORY_INJECT_TOP_K=${MEMORY_INJECT_TOP_K:-0}
MEMORY_INJECT_MAX_CHARS=${MEMORY_INJECT_MAX_CHARS:-700}
MEMORY_VOTE_INJECTED=${MEMORY_VOTE_INJECTED:-True}
MEMORY_DISABLE_SEARCH_MEMORY=${MEMORY_DISABLE_SEARCH_MEMORY:-False}
VAL_DATA_DIR=${VAL_DATA_DIR:-val_log/reflect-4B}

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
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
    data.train_batch_size=32 \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=32 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=2 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=2 \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.8 \
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
    trainer.n_gpus_per_node=8 \
    +trainer.val_data_dir=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/val_log/8_lantent_newest \
    trainer.nnodes=1 \
    trainer.save_freq=5 \
    trainer.test_freq=5 \
    trainer.total_epochs=15 \
    trainer.default_local_dir=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/8_lantent_newest \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/examples/sglang_multiturn/config/tool_config/hot_stock_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=2048 \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton \
    algorithm.adv_estimator=latent_factor_grpo \
    algorithm.latent_factor_hidden_dim=32 \
    algorithm.latent_factor_lr=1e-2 \
    algorithm.latent_factor_tau=1 \
    algorithm.latent_factor_balance=False \
    algorithm.latent_factor_balance_iters=4 \
    algorithm.latent_factor_preserve_scalar_mean=True \
    algorithm.latent_factor_residual_correction=True \
    algorithm.latent_factor_mix_with_vanilla=0.0 \
    actor_rollout_ref.actor.calculate_update_sketch=True \
    actor_rollout_ref.actor.update_sketch_dim=64 \
    actor_rollout_ref.actor.update_sketch_seed=17



    
    