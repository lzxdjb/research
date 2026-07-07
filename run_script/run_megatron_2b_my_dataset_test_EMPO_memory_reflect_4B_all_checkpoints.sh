#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ENGINE=${1:-vllm}
CHECKPOINT_ROOT=${CHECKPOINT_ROOT:-"$PROJECT_DIR/wiki_reflect_search_no_mask"}
CHECKPOINT_SUBDIR=${CHECKPOINT_SUBDIR:-actor/huggingface}
LOG_ROOT=${LOG_ROOT:-"$PROJECT_DIR/output/checkpoint_eval_logs/wiki_reflect_search_no_mask"}
VAL_DATA_DIR_ROOT=${VAL_DATA_DIR_ROOT:-val_log/wiki_reflect_search_no_mask}
MEMORY_SAVE_ROOT=${MEMORY_SAVE_ROOT:-"$PROJECT_DIR/output/hotpot_memory/wiki_reflect_search_no_mask"}
MEMORY_PAIRED_FORCE_SEARCH_MEMORY=${MEMORY_PAIRED_FORCE_SEARCH_MEMORY:-True}
MEMORY_PAIRED_NONPOSITIVE_VOTE_DELTA=${MEMORY_PAIRED_NONPOSITIVE_VOTE_DELTA:-0}
MEMORY_PAIRED_DELTA_REWARD_COEF=${MEMORY_PAIRED_DELTA_REWARD_COEF:-0.0}
MEMORY_PRUNE_MIN_USES=${MEMORY_PRUNE_MIN_USES:-4}
MEMORY_PRUNE_MEAN_DELTA_THRESHOLD=${MEMORY_PRUNE_MEAN_DELTA_THRESHOLD:-0.0}
MEMORY_DELTA_EMA_ALPHA=${MEMORY_DELTA_EMA_ALPHA:-0.2}
MEMORY_SEARCH_TOP_K=${MEMORY_SEARCH_TOP_K:-0}
MEMORY_SEARCH_MIN_SIMILARITY=${MEMORY_SEARCH_MIN_SIMILARITY:-0.0}
MEMORY_SEARCH_CANDIDATE_MULTIPLIER=${MEMORY_SEARCH_CANDIDATE_MULTIPLIER:-4}

WANDB_PROJECT=${WANDB_PROJECT:-wiki}
WANDB_EXP_PREFIX=${WANDB_EXP_PREFIX:-test-wiki_reflect_search_no_mask}
WANDB_SUMMARY_RUN_NAME=${WANDB_SUMMARY_RUN_NAME:-"${WANDB_EXP_PREFIX}-all-checkpoints"}
WANDB_SUMMARY_RUN_ID=${WANDB_SUMMARY_RUN_ID:-"${WANDB_SUMMARY_RUN_NAME//[^a-zA-Z0-9_-]/_}"}
TRAINER_LOGGER=${TRAINER_LOGGER:-'["console"]'}
export WANDB_RUN_GROUP=${WANDB_RUN_GROUP:-"$WANDB_EXP_PREFIX"}
export WANDB_JOB_TYPE=${WANDB_JOB_TYPE:-checkpoint-eval-summary}

mkdir -p "$PROJECT_DIR/output" "$LOG_ROOT" "$MEMORY_SAVE_ROOT"
SUMMARY_TSV="$LOG_ROOT/summary.tsv"
printf "step\tmodel_path\tstatus\tlog_file\n" > "$SUMMARY_TSV"
export CHECKPOINT_ROOT CHECKPOINT_SUBDIR

if [[ ! -d "$CHECKPOINT_ROOT" ]]; then
    echo "[error] checkpoint root does not exist: $CHECKPOINT_ROOT"
    exit 1
fi

mapfile -t STEP_DIRS < <(find "$CHECKPOINT_ROOT" -maxdepth 1 -mindepth 1 -type d -name "global_step_*" | sort -V)
if (( ${#STEP_DIRS[@]} == 0 )); then
    echo "[error] no global_step_* directories found under $CHECKPOINT_ROOT"
    exit 1
fi

export CUDA_DEVICE_MAX_CONNECTIONS=1
export VLLM_PROMPT_MAX_IMAGE_PIXELS=602112
export VLLM_ALLREDUCE_USE_SYMM_MEM=0
export http_proxy=${http_proxy:-"http://hexin:hx300033@10.217.180.65:30100"}
export https_proxy=${https_proxy:-"http://hexin:hx300033@10.217.180.65:30100"}
export HTTP_PROXY=${HTTP_PROXY:-"$http_proxy"}
export HTTPS_PROXY=${HTTPS_PROXY:-"$https_proxy"}

if [[ -n "${WANDB_API_KEY:-}" ]]; then
    wandb login "$WANDB_API_KEY"
fi

if [[ "${INSTALL_DEPS:-1}" == "1" ]]; then
    pip install transformers==5.3.0 flash-linear-attention
    pip install wikipedia
    pip install sentence-transformers fastapi uvicorn
fi

cleanup_eval_workers() {
    pkill -9 -f VLLM >/dev/null 2>&1 || true
    ray stop >/dev/null 2>&1 || true
}

cleanup_all() {
    cleanup_eval_workers
    if [[ -n "${EMBED_PID:-}" ]] && kill -0 "$EMBED_PID" >/dev/null 2>&1; then
        kill "$EMBED_PID" >/dev/null 2>&1 || true
        wait "$EMBED_PID" >/dev/null 2>&1 || true
    fi
}
trap cleanup_all EXIT INT TERM

log_metrics_to_wandb() {
    local log_file=$1
    local step_num=$2
    local step_name=$3
    local model_path=$4

    python3 - "$log_file" "$step_num" "$step_name" "$model_path" "$WANDB_PROJECT" "$WANDB_SUMMARY_RUN_NAME" "$WANDB_SUMMARY_RUN_ID" <<'PY'
import os
import re
import sys
from pathlib import Path

log_file, step_num, step_name, model_path, project, run_name, run_id = sys.argv[1:8]
step = int(step_num)
text = Path(log_file).read_text(encoding="utf-8", errors="replace")

metric_re = re.compile(
    r"(?P<key>[A-Za-z0-9_./@-]+):(?P<val>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
)


def parse_metric_line(line: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for match in metric_re.finditer(line):
        key = match.group("key")
        if "/" not in key:
            continue
        metrics[key] = float(match.group("val"))
    return metrics


metrics = {}
for line in reversed(text.splitlines()):
    if "val-core/" in line or "val-aux/" in line:
        metrics = parse_metric_line(line)
        if metrics:
            break

if not metrics:
    raise RuntimeError(f"could not parse validation metrics from {log_file}")

import wandb

run = wandb.init(
    project=project,
    name=run_name,
    id=run_id,
    resume="allow",
    job_type=os.environ.get("WANDB_JOB_TYPE", "checkpoint-eval-summary"),
    group=os.environ.get("WANDB_RUN_GROUP"),
    config={
        "checkpoint_root": os.environ.get("CHECKPOINT_ROOT"),
        "checkpoint_subdir": os.environ.get("CHECKPOINT_SUBDIR"),
        "source": "run_megatron_2b_my_dataset_test_EMPO_memory_reflect_4B_all_checkpoints.sh",
    },
)
run.config.update(
    {
        "last_checkpoint_name": step_name,
        "last_checkpoint_path": model_path,
    },
    allow_val_change=True,
)
wandb.define_metric("eval/global_step")
wandb.define_metric("*", step_metric="eval/global_step")
metrics["eval/global_step"] = step
metrics["eval/checkpoint_step"] = step
metrics["eval/success"] = 1
wandb.log(metrics)
run.finish()
print(f"[wandb] logged {len(metrics)} metrics for {step_name} to {project}/{run_name}")
PY
}

pkill -f "uvicorn alfworld_server:app" >/dev/null 2>&1 || true
pkill -f "verl.tools.embedding_server" >/dev/null 2>&1 || true
cleanup_eval_workers

# Embedding server ------------------------------------------------------------
EMBED_PORT=${EMBED_PORT:-8766}
fuser -k "${EMBED_PORT}/tcp" >/dev/null 2>&1 || true

python -m verl.tools.embedding_server --model BAAI/bge-large-en-v1.5 --port "$EMBED_PORT" \
    > "$PROJECT_DIR/output/embedding_server.log" 2>&1 &
EMBED_PID=$!
echo "[embed] server PID=$EMBED_PID, waiting for startup..."

EMBED_HOST=${EMBED_HOST:-127.0.0.1}
EMBED_URL="http://${EMBED_HOST}:${EMBED_PORT}"
export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1,${EMBED_HOST}"
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}localhost,127.0.0.1,${EMBED_HOST}"

embed_deadline=$((SECONDS + 600))
embed_ready=0
while (( SECONDS < embed_deadline )); do
    if curl --noproxy "*" -sf "${EMBED_URL}/health" >/dev/null 2>&1; then
        echo "[embed] server ready at ${EMBED_URL}"
        embed_ready=1
        break
    fi
    if ! kill -0 "$EMBED_PID" >/dev/null 2>&1; then
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

# Shared eval config ----------------------------------------------------------
train_path=${TRAIN_PATH:-"$PROJECT_DIR/data/multihop_mix_reflect/val_mix.parquet"}
test_path=${TEST_PATH:-"$PROJECT_DIR/data/multihop_mix_reflect/val_mix.parquet"}
max_prompt_length=${MAX_PROMPT_LENGTH:-2000}
max_response_length=${MAX_RESPONSE_LENGTH:-8000}
max_token_len=$((max_prompt_length + max_response_length))

TP=${TP:-4}
PP=${PP:-1}
CP=${CP:-1}
EP=${EP:-8}
ETP=${ETP:-1}
GEN_TP=${GEN_TP:-4}
MEMORY_VOTE_INJECTED=${MEMORY_VOTE_INJECTED:-True}

run_checkpoint() {
    local step_dir=$1
    local step_name step_num model_path exp_name log_file val_data_dir memory_save_dir

    step_name=$(basename "$step_dir")
    step_num=${step_name#global_step_}
    model_path="$step_dir/$CHECKPOINT_SUBDIR"
    exp_name="${WANDB_EXP_PREFIX}-${step_name}"
    log_file="$LOG_ROOT/${step_name}.log"
    val_data_dir="$VAL_DATA_DIR_ROOT/$step_name"
    memory_save_dir="$MEMORY_SAVE_ROOT/$step_name"

    mkdir -p "$val_data_dir" "$memory_save_dir"
    cleanup_eval_workers

    echo "[eval] $step_name"
    echo "[eval] model_path=$model_path"
    echo "[eval] trainer_logger=$TRAINER_LOGGER"
    echo "[eval] summary wandb project=$WANDB_PROJECT run=$WANDB_SUMMARY_RUN_NAME group=$WANDB_RUN_GROUP"

    python3 -m verl.trainer.main_ppo --config-path=config \
        --config-name='ppo_megatron_trainer.yaml' \
        algorithm.adv_estimator=grpo \
        data.train_files="$train_path" \
        data.val_files="$test_path" \
        +data.multi_val_files.wiki2="$PROJECT_DIR/data/multihop_mix_reflect/test_2wiki.parquet" \
        +data.multi_val_files.bamboogle="$PROJECT_DIR/data/multihop_mix_reflect/test_bamboogle.parquet" \
        +data.multi_val_files.hotpotqa="$PROJECT_DIR/data/multihop_mix_reflect/test_hotpotqa.parquet" \
        +data.multi_val_files.musique="$PROJECT_DIR/data/multihop_mix_reflect/test_musique.parquet" \
        data.train_batch_size=8 \
        data.max_prompt_length=${max_prompt_length} \
        data.max_response_length=${max_response_length} \
        data.filter_overlong_prompts=True \
        data.truncation='left' \
        actor_rollout_ref.model.path="$model_path" \
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
        +trainer.val_data_dir="$val_data_dir" \
        +trainer.validate_before_train=True \
        +trainer.val_only=True \
        trainer.logger="$TRAINER_LOGGER" \
        trainer.project_name="$WANDB_PROJECT" \
        trainer.experiment_name="$exp_name" \
        trainer.n_gpus_per_node=4 \
        trainer.nnodes=1 \
        trainer.save_freq=20 \
        trainer.test_freq=5 \
        trainer.total_epochs=15 \
        +trainer.eval_checkpoint_step="$step_num" \
        +trainer.eval_checkpoint_path="$model_path" \
        actor_rollout_ref.rollout.multi_turn.format=qwen3_coder \
        actor_rollout_ref.rollout.multi_turn.tool_config_path="$PROJECT_DIR/examples/sglang_multiturn/config/tool_config/hot_reflect_tool_config.yaml" \
        actor_rollout_ref.rollout.multi_turn.max_tool_response_length=2048 \
        +actor_rollout_ref.rollout.multi_turn.memory.save_dir="$memory_save_dir" \
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
        2>&1 | tee "$log_file"
}

failures=0
for step_dir in "${STEP_DIRS[@]}"; do
    step_name=$(basename "$step_dir")
    model_path="$step_dir/$CHECKPOINT_SUBDIR"
    log_file="$LOG_ROOT/${step_name}.log"

    if [[ ! -d "$model_path" ]]; then
        echo "[skip] $step_name missing $CHECKPOINT_SUBDIR"
        printf "%s\t%s\t%s\t%s\n" "$step_name" "$model_path" "missing" "$log_file" >> "$SUMMARY_TSV"
        continue
    fi

    if run_checkpoint "$step_dir"; then
        step_num=${step_name#global_step_}
        log_metrics_to_wandb "$log_file" "$step_num" "$step_name" "$model_path"
        printf "%s\t%s\t%s\t%s\n" "$step_name" "$model_path" "success" "$log_file" >> "$SUMMARY_TSV"
        echo "[done] $step_name"
    else
        status=$?
        failures=$((failures + 1))
        printf "%s\t%s\t%s\t%s\n" "$step_name" "$model_path" "failed:$status" "$log_file" >> "$SUMMARY_TSV"
        echo "[failed] $step_name status=$status"
    fi

    cleanup_eval_workers
done

echo "[summary] $SUMMARY_TSV"
if (( failures > 0 )); then
    echo "[summary] $failures checkpoint(s) failed"
    exit 1
fi
echo "[summary] all checkpoints evaluated successfully"
