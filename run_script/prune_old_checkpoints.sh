#!/usr/bin/env bash
set -euo pipefail

CHECKPOINT_ROOT="${CHECKPOINT_ROOT:-/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints}"
KEEP="${KEEP:-1}"
INTERVAL="${INTERVAL:-60}"
MODE="dry-run"
WATCH=0

usage() {
  cat <<'EOF'
Usage:
  run_script/prune_old_checkpoints.sh [--delete] [--dry-run] [--watch] [--interval SECONDS] [--root PATH] [--keep N]

Scans each immediate task folder under the checkpoint root and keeps only the
newest completed numeric global_step_<N> directories. By default this is a dry run.

If latest_checkpointed_iteration.txt exists and points to an existing global_step
directory, newer directories are treated as still in progress and are not removed
until the latest file catches up. If the latest file is missing or invalid, the
highest numeric global_step directory is kept.

Options:
  --delete            Actually remove old global_step_<N> directories.
  --dry-run           Print what would be removed without deleting anything.
  --watch             Run forever and scan repeatedly.
  --interval SECONDS  Watch scan interval. Default: 60.
  --root PATH         Checkpoint root. Defaults to CHECKPOINT_ROOT or repo checkpoints.
  --keep N            Number of newest completed global_step directories to keep per task. Default: 1.
EOF
}

log() {
  echo "[$(date '+%F %T')] $*"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete)
      MODE="delete"
      shift
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --watch)
      WATCH=1
      shift
      ;;
    --once)
      WATCH=0
      shift
      ;;
    --interval)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --interval requires a positive integer number of seconds" >&2
        exit 2
      fi
      INTERVAL="$2"
      shift 2
      ;;
    --root)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --root requires a path" >&2
        exit 2
      fi
      CHECKPOINT_ROOT="$2"
      shift 2
      ;;
    --keep)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --keep requires a positive integer" >&2
        exit 2
      fi
      KEEP="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --keep must be a positive integer, got: $KEEP" >&2
  exit 2
fi

if [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: --interval must be a positive integer, got: $INTERVAL" >&2
  exit 2
fi

if [[ ! -d "$CHECKPOINT_ROOT" ]]; then
  echo "ERROR: checkpoint root does not exist: $CHECKPOINT_ROOT" >&2
  exit 1
fi

LOCK_FILE="$CHECKPOINT_ROOT/.prune_old_checkpoints.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Another prune_old_checkpoints.sh instance is already running for $CHECKPOINT_ROOT" >&2
  exit 1
fi

prune_once() {
  local task_count=0
  local remove_count=0
  local skip_count=0
  local preserved_newer_count=0

  while IFS= read -r -d '' task_dir; do
    local task_name="${task_dir#"$CHECKPOINT_ROOT"/}"
    local -a step_entries=()
    local step_dir step_base

    while IFS= read -r -d '' step_dir; do
      step_base="${step_dir##*/}"
      if [[ "$step_base" =~ ^global_step_([0-9]+)$ ]]; then
        step_entries+=("${BASH_REMATCH[1]}:$step_dir")
      else
        log "skip non-numeric global_step directory: $step_dir"
        ((skip_count += 1))
      fi
    done < <(find "$task_dir" -mindepth 1 -maxdepth 1 -type d -name 'global_step_*' -print0)

    if (( ${#step_entries[@]} == 0 )); then
      continue
    fi

    ((task_count += 1))
    local -a sorted_steps=()
    mapfile -t sorted_steps < <(printf '%s\n' "${step_entries[@]}" | sort -t ':' -k1,1nr)

    local latest_file="$task_dir/latest_checkpointed_iteration.txt"
    local latest_value=""
    local latest_is_usable=0
    local entry step_num

    if [[ -f "$latest_file" ]]; then
      read -r latest_value < "$latest_file" || true
      latest_value="${latest_value//[[:space:]]/}"
      if [[ "$latest_value" =~ ^[0-9]+$ ]]; then
        for entry in "${sorted_steps[@]}"; do
          step_num="${entry%%:*}"
          if [[ "$step_num" == "$latest_value" ]]; then
            latest_is_usable=1
            break
          fi
        done
      fi
    fi

    local -a prune_candidates=()
    if (( latest_is_usable == 1 )); then
      for entry in "${sorted_steps[@]}"; do
        step_num="${entry%%:*}"
        if (( 10#$step_num <= 10#$latest_value )); then
          prune_candidates+=("$entry")
        else
          ((preserved_newer_count += 1))
        fi
      done
    else
      prune_candidates=("${sorted_steps[@]}")
    fi

    if (( ${#prune_candidates[@]} <= KEEP )); then
      continue
    fi

    local -a keep_paths=()
    local i
    for ((i = 0; i < KEEP; i++)); do
      keep_paths+=("${prune_candidates[$i]#*:}")
    done
    log "$task_name: keep ${keep_paths[*]}"

    local remove_path remove_base
    for ((i = KEEP; i < ${#prune_candidates[@]}; i++)); do
      remove_path="${prune_candidates[$i]#*:}"
      remove_base="${remove_path##*/}"

      if [[ ! "$remove_path" == "$task_dir"/global_step_* ]] || [[ ! "$remove_base" =~ ^global_step_[0-9]+$ ]]; then
        log "SAFETY SKIP: refusing to remove unexpected path: $remove_path" >&2
        ((skip_count += 1))
        continue
      fi

      if [[ "$MODE" == "delete" ]]; then
        log "delete $remove_path"
        rm -rf -- "$remove_path"
      else
        log "dry-run delete $remove_path"
      fi
      ((remove_count += 1))
    done
  done < <(find "$CHECKPOINT_ROOT" -mindepth 1 -maxdepth 1 -type d -print0)

  log "scan done: tasks_with_steps=$task_count removed_or_would_remove=$remove_count skipped=$skip_count preserved_newer_than_latest=$preserved_newer_count"
}

log "checkpoint root: $CHECKPOINT_ROOT"
log "mode: $MODE, keep newest completed: $KEEP"

if (( WATCH == 1 )); then
  log "watch mode enabled, interval_seconds=$INTERVAL"
  while true; do
    prune_once
    sleep "$INTERVAL"
  done
else
  prune_once
fi
