set -euo pipefail
set -x
ray stop
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

WANDB_STORE_DIR=${WANDB_STORE_DIR:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/wandb}
mkdir -p "$WANDB_STORE_DIR"
export WANDB_DIR="${WANDB_DIR:-$(dirname "$WANDB_STORE_DIR")}"

rm -rf "$PROJECT_DIR/output/wiki_user_sim_reflect"
rm -rf "$PROJECT_DIR/rollout_log/wiki_user_sim_reflect"

mkdir -p "$PROJECT_DIR/output"

ENGINE=${1:-vllm}
export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0

export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"

THSCC_TRAIN_CREATOR=wiki
JOB_NAME=${JOB_NAME:-wiki_user_sim_reflect}
ADV_ESTIMATOR=${ADV_ESTIMATOR:-grpo}
CALCULATE_SUM_PI_SQUARED=${CALCULATE_SUM_PI_SQUARED:-False}
CALCULATE_UPDATE_SKETCH=${CALCULATE_UPDATE_SKETCH:-False}
UPDATE_SKETCH_DIM=${UPDATE_SKETCH_DIM:-64}
UPDATE_SKETCH_SEED=${UPDATE_SKETCH_SEED:-17}
POLICY_LOSS_MODE=${POLICY_LOSS_MODE:-vanilla}
INTENTIONAL_ETA=${INTENTIONAL_ETA:-1.0}
INTENTIONAL_SCORE_NORM_EPS=${INTENTIONAL_SCORE_NORM_EPS:-1e-8}
INTENTIONAL_CLIP_TARGET=${INTENTIONAL_CLIP_TARGET:-True}
INTENTIONAL_REQUIRE_SUM_PI_SQUARED=${INTENTIONAL_REQUIRE_SUM_PI_SQUARED:-True}
INTENTIONAL_ALPHA_MIN=${INTENTIONAL_ALPHA_MIN:-0.0}
INTENTIONAL_ALPHA_MAX=${INTENTIONAL_ALPHA_MAX:-}
LPO_PROJECTION=${LPO_PROJECTION:-forward}
LPO_TAU=${LPO_TAU:-1.0}
LPO_ADAPTIVE_GRID_SIZE=${LPO_ADAPTIVE_GRID_SIZE:-32}
LPO_ADAPTIVE_MAX_LOGIT_GAP=${LPO_ADAPTIVE_MAX_LOGIT_GAP:-20.0}
BOS_GRPO_K=${BOS_GRPO_K:-4}
BOS_GRPO_LAMBDA=${BOS_GRPO_LAMBDA:-1.0}
BOS_GRPO_WEIGHT_FLOOR=${BOS_GRPO_WEIGHT_FLOOR:-0.1}
BOS_GRPO_WEIGHT_POWER=${BOS_GRPO_WEIGHT_POWER:-1.0}
BOS_GRPO_MIX_WITH_VANILLA=${BOS_GRPO_MIX_WITH_VANILLA:-0.0}
BOS_GRPO_POSITIVE_EIGS_ONLY=${BOS_GRPO_POSITIVE_EIGS_ONLY:-True}
BOS_GRPO_FALLBACK_TO_VANILLA=${BOS_GRPO_FALLBACK_TO_VANILLA:-True}
BOS_GRPO_INCLUDE_FEATURE_STD=${BOS_GRPO_INCLUDE_FEATURE_STD:-True}
BOS_GRPO_NORMALIZE_FEATURES=${BOS_GRPO_NORMALIZE_FEATURES:-True}
BOS_GRPO_EPS=${BOS_GRPO_EPS:-1e-6}

export SWANLAB_PROJECT=$THSCC_TRAIN_CREATOR
export SWANLAB_EXP_NAME=$JOB_NAME
ENABLE_WANDB=${ENABLE_WANDB:-1}
if [[ "$ENABLE_WANDB" == "1" || "$ENABLE_WANDB" == "true" ]]; then
    wandb login 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
    TRAINER_LOGGER='["console","wandb"]'
else
    export WANDB_MODE=disabled
    TRAINER_LOGGER='["console"]'
fi

RAW_DATA_DIR=${RAW_DATA_DIR:-"$PROJECT_DIR/data/multihop_raw"}
DATASET_DIR=${DATASET_DIR:-"$PROJECT_DIR/data/multihop_mix_user_sim_reflect"}
REBUILD_DATASET=${REBUILD_DATASET:-1}

if [[ "$REBUILD_DATASET" == "1" || "$REBUILD_DATASET" == "true" ]]; then
    python dataset_make_multihop_mix_reflect.py \
        --hotpot_train    "$RAW_DATA_DIR/hotpot_train.jsonl" \
        --wiki2_train     "$RAW_DATA_DIR/2wiki_train.jsonl" \
        --musique_train   "$RAW_DATA_DIR/musique_train.jsonl" \
        --hotpot_test     "$RAW_DATA_DIR/hotpot_test.jsonl" \
        --wiki2_test      "$RAW_DATA_DIR/2wiki_test.jsonl" \
        --musique_test    "$RAW_DATA_DIR/musique_test.jsonl" \
        --bamboogle_test  "$RAW_DATA_DIR/bamboogle.jsonl" \
        --output_dir      "$DATASET_DIR" \
        --system_prompt_path "$PROJECT_DIR/system_prompt_hot_reflect.md" \
        --agent_name      wiki_user_sim_reflect_agent \
        --data_source_name wiki_user_sim_reflect \
        --val_ratio       0.02 \
        --max_per_source  5000 \
        --max_test_per_source 100 \
        --seed            42
fi

python - "$DATASET_DIR/train_mix.jsonl" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    row = json.loads(f.readline())

agent_name = row.get("agent_name")
data_source = row.get("data_source")
if agent_name != "wiki_user_sim_reflect_agent" or data_source != "wiki_user_sim_reflect":
    raise SystemExit(
        f"stale dataset at {path}: agent_name={agent_name!r}, data_source={data_source!r}"
    )
PY

train_path="$DATASET_DIR/train_mix.jsonl"
test_path="$DATASET_DIR/val_mix.jsonl"
max_prompt_length=${MAX_PROMPT_LENGTH:-1100}
max_response_length=${MAX_RESPONSE_LENGTH:-12000}
max_token_len=$((max_prompt_length + max_response_length))
TRAIN_BATCH_SIZE=${TRAIN_BATCH_SIZE:-64}
PPO_MINI_BATCH_SIZE=${PPO_MINI_BATCH_SIZE:-32}
ROLLOUT_N=${ROLLOUT_N:-8}

SKIP_PIP_INSTALL=${SKIP_PIP_INSTALL:-0}
if [[ "$SKIP_PIP_INSTALL" != "1" && "$SKIP_PIP_INSTALL" != "true" ]]; then
    pip install transformers==5.3.0 flash-linear-attention==0.4.2 triton==3.6.0
    pip install wikipedia
fi

export VLLM_RPC_TIMEOUT=3600
export NCCL_TIMEOUT=7200

VAL_DATA_DIR=${VAL_DATA_DIR:-"$PROJECT_DIR/val_log/${JOB_NAME}"}
ROLLOUT_DATA_DIR=${ROLLOUT_DATA_DIR:-"$PROJECT_DIR/rollout_log/${JOB_NAME}"}
VALIDATE_BEFORE_TRAIN=${VALIDATE_BEFORE_TRAIN:-True}

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
if [[ "$POLICY_LOSS_MODE" == "simple_intentional_grpo" || "$POLICY_LOSS_MODE" == "intentional_grpo" || "$POLICY_LOSS_MODE" == "vanilla_adaptive_alpha_grpo" || "$POLICY_LOSS_MODE" == "adaptive_alpha_grpo" ]]; then
    EXTRA_OVERRIDES+=(actor_rollout_ref.actor.policy_loss.intentional_eta="$INTENTIONAL_ETA")
    EXTRA_OVERRIDES+=(actor_rollout_ref.actor.policy_loss.intentional_score_norm_eps="$INTENTIONAL_SCORE_NORM_EPS")
    EXTRA_OVERRIDES+=(actor_rollout_ref.actor.policy_loss.intentional_clip_target="$INTENTIONAL_CLIP_TARGET")
    EXTRA_OVERRIDES+=(actor_rollout_ref.actor.policy_loss.intentional_require_sum_pi_squared="$INTENTIONAL_REQUIRE_SUM_PI_SQUARED")
    EXTRA_OVERRIDES+=(actor_rollout_ref.actor.policy_loss.intentional_alpha_min="$INTENTIONAL_ALPHA_MIN")
    if [[ -n "$INTENTIONAL_ALPHA_MAX" ]]; then
        EXTRA_OVERRIDES+=(actor_rollout_ref.actor.policy_loss.intentional_alpha_max="$INTENTIONAL_ALPHA_MAX")
    fi
fi
if [[ "$ADV_ESTIMATOR" == "lpo" || "$ADV_ESTIMATOR" == "lpo_forward" || "$ADV_ESTIMATOR" == "lpo_adaptive" || "$ADV_ESTIMATOR" == "lpo_adaptive_forward" ]]; then
    EXTRA_OVERRIDES+=(algorithm.lpo_projection="$LPO_PROJECTION")
    EXTRA_OVERRIDES+=(algorithm.lpo_tau="$LPO_TAU")
    EXTRA_OVERRIDES+=(algorithm.lpo_adaptive_grid_size="$LPO_ADAPTIVE_GRID_SIZE")
    EXTRA_OVERRIDES+=(algorithm.lpo_adaptive_max_logit_gap="$LPO_ADAPTIVE_MAX_LOGIT_GAP")
fi
if [[ "$ADV_ESTIMATOR" == "latent_factor_grpo" || "${EXTRA_LATENT_FACTOR_OVERRIDES:-0}" == "1" ]]; then
    EXTRA_OVERRIDES+=(algorithm.latent_factor_k="${LATENT_FACTOR_K:-8}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_hidden_dim="${LATENT_FACTOR_HIDDEN_DIM:-32}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_aux_steps="${LATENT_FACTOR_AUX_STEPS:-8}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_lr="${LATENT_FACTOR_LR:-1e-2}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_tau="${LATENT_FACTOR_TAU:-1.0}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_balance="${LATENT_FACTOR_BALANCE:-True}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_balance_iters="${LATENT_FACTOR_BALANCE_ITERS:-4}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_preserve_scalar_mean="${LATENT_FACTOR_PRESERVE_SCALAR_MEAN:-True}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_residual_correction="${LATENT_FACTOR_RESIDUAL_CORRECTION:-True}")
    EXTRA_OVERRIDES+=(algorithm.latent_factor_mix_with_vanilla="${LATENT_FACTOR_MIX_WITH_VANILLA:-0.0}")
fi
if [[ "$ADV_ESTIMATOR" == "batch_opt_subspace_grpo" || "$ADV_ESTIMATOR" == "bos_grpo" || "${EXTRA_BOS_GRPO_OVERRIDES:-0}" == "1" ]]; then
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_k="$BOS_GRPO_K")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_lambda="$BOS_GRPO_LAMBDA")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_weight_floor="$BOS_GRPO_WEIGHT_FLOOR")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_weight_power="$BOS_GRPO_WEIGHT_POWER")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_mix_with_vanilla="$BOS_GRPO_MIX_WITH_VANILLA")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_positive_eigs_only="$BOS_GRPO_POSITIVE_EIGS_ONLY")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_fallback_to_vanilla="$BOS_GRPO_FALLBACK_TO_VANILLA")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_include_feature_std="$BOS_GRPO_INCLUDE_FEATURE_STD")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_normalize_features="$BOS_GRPO_NORMALIZE_FEATURES")
    EXTRA_OVERRIDES+=(algorithm.bos_grpo_eps="$BOS_GRPO_EPS")
fi

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml'\
    algorithm.adv_estimator="$ADV_ESTIMATOR" \
    data.dataloader_num_workers=0 \
    data.seed=42 \
    data.train_files="$train_path" \
    data.val_files="$test_path" \
    +data.multi_val_files.wiki2="$DATASET_DIR/test_2wiki.parquet" \
    +data.multi_val_files.bamboogle="$DATASET_DIR/test_bamboogle.parquet" \
    +data.multi_val_files.hotpotqa="$DATASET_DIR/test_hotpotqa.parquet" \
    +data.multi_val_files.musique="$DATASET_DIR/test_musique.parquet" \
    data.train_batch_size=$TRAIN_BATCH_SIZE \
    data.max_prompt_length=${max_prompt_length} \
    data.max_response_length=${max_response_length} \
    data.filter_overlong_prompts=True \
    data.truncation='left' \
    actor_rollout_ref.model.path=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=$PPO_MINI_BATCH_SIZE \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.actor.policy_loss.loss_mode="$POLICY_LOSS_MODE" \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.calculate_sum_pi_squared="$CALCULATE_SUM_PI_SQUARED" \
    actor_rollout_ref.actor.calculate_update_sketch="$CALCULATE_UPDATE_SKETCH" \
    actor_rollout_ref.actor.update_sketch_dim="$UPDATE_SKETCH_DIM" \
    actor_rollout_ref.actor.update_sketch_seed="$UPDATE_SKETCH_SEED" \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=1 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=4 \
    actor_rollout_ref.actor.ppo_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.ref.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.log_prob_max_token_len_per_gpu=$max_token_len \
    actor_rollout_ref.rollout.name=$ENGINE \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6 \
    actor_rollout_ref.rollout.n=$ROLLOUT_N \
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
    trainer.logger="$TRAINER_LOGGER" \
    trainer.project_name=$SWANLAB_PROJECT \
    trainer.experiment_name=$SWANLAB_EXP_NAME \
    trainer.n_gpus_per_node=8 \
    trainer.rollout_data_dir="$ROLLOUT_DATA_DIR" \
    trainer.nnodes=1 \
    trainer.save_freq=${SAVE_FREQ:-10} \
    trainer.test_freq=${TEST_FREQ:-5} \
    trainer.total_epochs=15 \
    trainer.default_local_dir="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/${JOB_NAME}" \
    actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
    actor_rollout_ref.rollout.agent.default_agent_loop=wiki_user_sim_reflect_agent \
    actor_rollout_ref.rollout.multi_turn.tool_config_path="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/examples/sglang_multiturn/config/tool_config/wiki_user_sim_reflect_tool_config.yaml" \
    actor_rollout_ref.rollout.multi_turn.max_tool_response_length=${MAX_TOOL_RESPONSE_LENGTH:-2048} \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_max_rounds=${WIKI_USER_SIM_MAX_ROUNDS:-3} \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_system_prompt_path="$PROJECT_DIR/system_prompt_wiki_user_simulator.md" \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_user_prompt_path="$PROJECT_DIR/user_prompt_wiki_user_simulator.md" \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_temperature=${WIKI_USER_SIM_TEMPERATURE:-0.7} \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_top_p=${WIKI_USER_SIM_TOP_P:-0.95} \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_max_tokens=${WIKI_USER_SIM_MAX_TOKENS:-256} \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_solver_max_tokens_per_turn=${WIKI_USER_SIM_SOLVER_MAX_TOKENS_PER_TURN:-0} \
    actor_rollout_ref.rollout.multi_turn.wiki_user_sim_answer_max_chars=${WIKI_USER_SIM_ANSWER_MAX_CHARS:-4000} \
    reward.custom_reward_function.path="$PROJECT_DIR/verl/utils/reward_score/wiki_final.py" \
    reward.custom_reward_function.name=compute_score \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    +actor_rollout_ref.rollout.engine_kwargs.vllm.gdn_prefill_backend=triton \
    "${EXTRA_OVERRIDES[@]}"
