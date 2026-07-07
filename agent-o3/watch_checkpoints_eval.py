#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import torch
except Exception:  # pragma: no cover - optional dependency
    torch = None

try:
    import wandb
except Exception:  # pragma: no cover - optional dependency
    wandb = None


STEP_RE = re.compile(r"^global_step_(\d+)$")
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth")
DEFAULT_OUTPUT_NAMES = {
    "simple_plan_output": "simple_plan_output.jsonl",
    "simple_summary_output": "simple_summary_output.jsonl",
    "deep_plan_output": "deep_plan_output.jsonl",
    "deep_summary_output": "deep_summary_output.jsonl",
    "log_file": "log.txt",
    "vllm_log_file": "vllm_server.log",
}


def parse_args() -> tuple[argparse.Namespace, List[str]]:
    parser = argparse.ArgumentParser(
        description="Watch a checkpoint folder and evaluate each HF checkpoint after it settles."
    )
    parser.add_argument(
        "--checkpoint-root",
        required=True,
        help="Root folder that contains global_step_* subfolders.",
    )
    parser.add_argument(
        "--checkpoint-subdir",
        default="actor/huggingface",
        help="Relative HF checkpoint subdir inside each global_step folder.",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Where per-checkpoint eval outputs are written.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="How often to rescan the checkpoint folder.",
    )
    parser.add_argument(
        "--ready-wait-seconds",
        type=int,
        default=300,
        help="Minimum quiet time after the last file modification before eval starts.",
    )
    parser.add_argument(
        "--failure-retry-seconds",
        type=int,
        default=900,
        help="Wait time before retrying a failed checkpoint.",
    )
    parser.add_argument(
        "--running-timeout-seconds",
        type=int,
        default=24 * 3600,
        help="How long a checkpoint may remain marked running before being retried.",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="JSON file used to remember completed and failed checkpoints.",
    )
    parser.add_argument(
        "--begin-step",
        type=int,
        default=None,
        help="Only evaluate checkpoints with step >= this value.",
    )
    parser.add_argument(
        "--end-step",
        type=int,
        default=None,
        help="Only evaluate checkpoints with step <= this value.",
    )
    parser.add_argument(
        "--force-reeval",
        action="store_true",
        help="Clear saved state for checkpoints in the selected step range so they are evaluated again.",
    )
    parser.add_argument(
        "--wandb-project",
        default="stock-rl-reflect-eval",
        help="W&B project name. Leave empty to disable wandb logging.",
    )
    parser.add_argument(
        "--wandb-entity",
        default=None,
        help="Optional W&B entity.",
    )
    parser.add_argument(
        "--wandb-run-name",
        default=None,
        help="Optional W&B run name.",
    )
    parser.add_argument(
        "--wandb-mode",
        default=None,
        help="Optional W&B mode, e.g. online or offline.",
    )
    parser.add_argument(
        "--keepalive-device",
        choices=["auto", "cpu", "cuda"],
        default="auto",
        help="Device to use for the waiting-phase heartbeat.",
    )
    parser.add_argument(
        "--keepalive-matrix-size",
        type=int,
        default=768,
        help="Square matrix size used by the heartbeat computation.",
    )
    parser.add_argument(
        "--keepalive-burst-seconds",
        type=int,
        default=20,
        help="How long each heartbeat burst runs before sleeping.",
    )
    parser.add_argument(
        "--keepalive-sleep-seconds",
        type=int,
        default=10,
        help="Sleep time between heartbeat bursts.",
    )
    parser.add_argument(
        "--keepalive-repeats",
        type=int,
        default=12,
        help="Number of matmul repeats inside each heartbeat burst.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exit after the current scan cycle finishes.",
    )
    parser.add_argument(
        "--eval-script",
        default=str(Path(__file__).with_name("main_contrast_out.py")),
        help="Eval entrypoint to invoke for each checkpoint.",
    )
    args, eval_args = parser.parse_known_args()
    return args, eval_args


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "checkpoints": {}}
    with path.open("r", encoding="utf-8") as f:
        try:
            state = json.load(f)
        except json.JSONDecodeError:
            return {"version": 1, "checkpoints": {}}
    if not isinstance(state, dict):
        return {"version": 1, "checkpoints": {}}
    state.setdefault("version", 1)
    state.setdefault("checkpoints", {})
    if not isinstance(state["checkpoints"], dict):
        state["checkpoints"] = {}
    return state


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def acquire_watch_lock(output_root: Path):
    lock_path = output_root / "watch.lock"
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"Another checkpoint watcher is already running for {output_root}. Lock: {lock_path}", flush=True)
        lock_file.close()
        return None
    lock_file.write(f"pid={os.getpid()}\n")
    lock_file.flush()
    return lock_file


def reset_interrupted_running_checkpoints(state: Dict[str, Any]) -> bool:
    changed = False
    now = time.time()
    for step_name, checkpoint_state in state.get("checkpoints", {}).items():
        if checkpoint_state.get("status") != "running":
            continue
        checkpoint_state["status"] = "failed"
        checkpoint_state["interrupted_at"] = now
        checkpoint_state["failure_reason"] = "watcher restarted while checkpoint was marked running"
        print(f"[{step_name}] reset stale running state to failed", flush=True)
        changed = True
    return changed


def step_in_range(step: int, begin_step: Optional[int], end_step: Optional[int]) -> bool:
    if begin_step is not None and step < begin_step:
        return False
    if end_step is not None and step > end_step:
        return False
    return True


def format_step_range(begin_step: Optional[int], end_step: Optional[int]) -> str:
    begin = str(begin_step) if begin_step is not None else "-inf"
    end = str(end_step) if end_step is not None else "+inf"
    return f"[{begin}, {end}]"


def clear_selected_checkpoint_state(
    state: Dict[str, Any],
    begin_step: Optional[int],
    end_step: Optional[int],
) -> bool:
    checkpoints = state.get("checkpoints", {})
    if not isinstance(checkpoints, dict):
        return False
    selected_names = []
    for step_name in checkpoints:
        step = parse_step_name(step_name)
        if step is not None and step_in_range(step, begin_step, end_step):
            selected_names.append(step_name)
    for step_name in selected_names:
        checkpoints.pop(step_name, None)
        print(f"[{step_name}] cleared saved state for re-eval", flush=True)
    return bool(selected_names)


def parse_step_name(name: str) -> Optional[int]:
    match = STEP_RE.match(name)
    return int(match.group(1)) if match else None


def discover_checkpoints(checkpoint_root: Path, checkpoint_subdir: str) -> List[Tuple[int, str, Path]]:
    items: List[Tuple[int, str, Path]] = []
    if not checkpoint_root.exists():
        return items
    for entry in checkpoint_root.iterdir():
        if not entry.is_dir():
            continue
        step = parse_step_name(entry.name)
        if step is None:
            continue
        model_path = entry / checkpoint_subdir
        items.append((step, entry.name, model_path))
    items.sort(key=lambda x: x[0])
    return items


def snapshot_tree(path: Path) -> Dict[str, Any]:
    file_count = 0
    latest_mtime = 0.0
    weight_seen = False
    config_seen = False
    total_size = 0
    if not path.exists():
        return {
            "file_count": 0,
            "latest_mtime": 0.0,
            "weight_seen": False,
            "config_seen": False,
            "total_size": 0,
        }
    for root, _, files in os.walk(path):
        for name in files:
            file_path = Path(root) / name
            try:
                stat = file_path.stat()
            except FileNotFoundError:
                continue
            file_count += 1
            total_size += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
            lower_name = name.lower()
            if lower_name == "config.json":
                config_seen = True
            if lower_name.endswith(WEIGHT_SUFFIXES):
                weight_seen = True
    return {
        "file_count": file_count,
        "latest_mtime": latest_mtime,
        "weight_seen": weight_seen,
        "config_seen": config_seen,
        "total_size": total_size,
    }


def checkpoint_ready(model_path: Path, ready_wait_seconds: int) -> Tuple[bool, Dict[str, Any], str]:
    if not model_path.exists():
        return False, snapshot_tree(model_path), "missing"
    snap = snapshot_tree(model_path)
    if snap["file_count"] == 0:
        return False, snap, "empty"
    if not snap["weight_seen"]:
        return False, snap, "no_weight_file"
    if not snap["config_seen"]:
        return False, snap, "no_config"
    age = time.time() - snap["latest_mtime"]
    if age < ready_wait_seconds:
        return False, snap, f"fresh_for_{int(age)}s"
    return True, snap, "ready"


def strip_option(args: List[str], option: str) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == option:
            i += 1
            if i < len(args) and not args[i].startswith("--"):
                i += 1
            continue
        if token.startswith(option + "="):
            i += 1
            continue
        out.append(token)
        i += 1
    return out


def strip_options(args: List[str], options: Iterable[str]) -> List[str]:
    for option in options:
        args = strip_option(args, option)
    return args


def maybe_init_wandb(args: argparse.Namespace, checkpoint_root: Path):
    if not args.wandb_project or wandb is None:
        return None
    init_kwargs: Dict[str, Any] = {
        "project": args.wandb_project,
        "name": args.wandb_run_name or f"watch-{checkpoint_root.name}",
        "settings": wandb.Settings(init_timeout=1200),
    }
    if args.wandb_entity:
        init_kwargs["entity"] = args.wandb_entity
    if args.wandb_mode:
        init_kwargs["mode"] = args.wandb_mode
    run = wandb.init(**init_kwargs)
    wandb.define_metric("checkpoint_step")
    wandb.define_metric("*", step_metric="checkpoint_step")
    return run


def choose_keepalive_devices(mode: str) -> List[int]:
    if torch is None or not hasattr(torch, "cuda") or not torch.cuda.is_available():
        return []
    count = torch.cuda.device_count()
    if count <= 0:
        return []
    if mode == "cpu":
        return []
    return list(range(count))


class KeepAlive:
    def __init__(
        self,
        devices: List[int],
        matrix_size: int,
        burst_seconds: int,
        sleep_seconds: int,
        repeats: int,
    ) -> None:
        self.devices = devices
        self.matrix_size = matrix_size
        self.burst_seconds = burst_seconds
        self.sleep_seconds = sleep_seconds
        self.repeats = repeats
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        if not self.devices:
            while not self._stop.is_set():
                time.sleep(self.sleep_seconds)
            return
        if torch is None:
            return
        while not self._stop.is_set():
            burst_end = time.time() + self.burst_seconds
            workspaces = {}
            for device_id in self.devices:
                with torch.cuda.device(device_id):
                    a = torch.randn(
                        (self.matrix_size, self.matrix_size),
                        device=f"cuda:{device_id}",
                        dtype=torch.float16,
                    )
                    b = torch.randn(
                        (self.matrix_size, self.matrix_size),
                        device=f"cuda:{device_id}",
                        dtype=torch.float16,
                    )
                workspaces[device_id] = (a, b)
            while time.time() < burst_end and not self._stop.is_set():
                for device_id, (a, b) in workspaces.items():
                    if self._stop.is_set():
                        break
                    with torch.cuda.device(device_id):
                        for _ in range(self.repeats):
                            _ = a @ b
                for device_id in self.devices:
                    torch.cuda.synchronize(device_id)
            time.sleep(self.sleep_seconds)


def summarize_jsonl(path: Path) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "line_count": 0,
        "best_scores": [],
        "local_scores": [],
        "wencai_scores": [],
        "wins": 0,
        "losses": 0,
    }
    if not path.exists():
        return stats
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            choices = item.get("choices") or []
            sort_list = item.get("sort_list") or []
            if not isinstance(choices, list) or not isinstance(sort_list, list) or not choices:
                continue
            score_by_original: Dict[int, float] = {}
            for choice, original_idx in zip(choices, sort_list):
                try:
                    score = float(choice.get("score"))
                    score_by_original[int(original_idx)] = score
                except Exception:
                    continue
            if not score_by_original:
                continue
            best_score = None
            if isinstance(choices[0], dict):
                try:
                    best_score = float(choices[0].get("score"))
                except Exception:
                    best_score = None
            if best_score is None:
                best_score = max(score_by_original.values())
            stats["best_scores"].append(best_score)
            if 1 in score_by_original:
                stats["local_scores"].append(score_by_original[1])
            if 2 in score_by_original:
                stats["wencai_scores"].append(score_by_original[2])
            if sort_list and int(sort_list[0]) == 1:
                stats["wins"] += 1
            else:
                stats["losses"] += 1
            stats["line_count"] += 1
    return stats


def mean_or_none(values: List[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


def stdev_or_none(values: List[float]) -> Optional[float]:
    return statistics.stdev(values) if len(values) > 1 else None


def summarize_outputs(output_dir: Path, log_file: Path) -> Dict[str, Any]:
    stage_files = {
        "simple_plan": output_dir / DEFAULT_OUTPUT_NAMES["simple_plan_output"],
        "simple_summary": output_dir / DEFAULT_OUTPUT_NAMES["simple_summary_output"],
        "deep_plan": output_dir / DEFAULT_OUTPUT_NAMES["deep_plan_output"],
        "deep_summary": output_dir / DEFAULT_OUTPUT_NAMES["deep_summary_output"],
    }
    metrics: Dict[str, Any] = {}
    overall_best_scores: List[float] = []
    overall_local_scores: List[float] = []
    overall_wencai_scores: List[float] = []
    overall_wins = 0
    overall_losses = 0

    for stage_name, stage_path in stage_files.items():
        stage_stats = summarize_jsonl(stage_path)
        metrics[f"{stage_name}/line_count"] = stage_stats["line_count"]
        metrics[f"{stage_name}/best_score_mean"] = mean_or_none(stage_stats["best_scores"])
        metrics[f"{stage_name}/best_score_std"] = stdev_or_none(stage_stats["best_scores"])
        metrics[f"{stage_name}/best_score_max"] = max(stage_stats["best_scores"]) if stage_stats["best_scores"] else None
        metrics[f"{stage_name}/best_score_min"] = min(stage_stats["best_scores"]) if stage_stats["best_scores"] else None
        metrics[f"{stage_name}/local_score_mean"] = mean_or_none(stage_stats["local_scores"])
        metrics[f"{stage_name}/wencai_score_mean"] = mean_or_none(stage_stats["wencai_scores"])
        metrics[f"{stage_name}/local_win_rate"] = (
            stage_stats["wins"] / stage_stats["line_count"] if stage_stats["line_count"] else None
        )
        metrics[f"{stage_name}/win_count"] = stage_stats["wins"]
        metrics[f"{stage_name}/loss_count"] = stage_stats["losses"]

        overall_best_scores.extend(stage_stats["best_scores"])
        overall_local_scores.extend(stage_stats["local_scores"])
        overall_wencai_scores.extend(stage_stats["wencai_scores"])
        overall_wins += stage_stats["wins"]
        overall_losses += stage_stats["losses"]

    metrics["overall/best_score_mean"] = mean_or_none(overall_best_scores)
    metrics["overall/local_score_mean"] = mean_or_none(overall_local_scores)
    metrics["overall/wencai_score_mean"] = mean_or_none(overall_wencai_scores)
    metrics["overall/local_win_rate"] = (
        overall_wins / (overall_wins + overall_losses) if (overall_wins + overall_losses) else None
    )
    metrics["overall/records_from_plan_files"] = sum(
        summarize_jsonl(stage_files[stage])["line_count"] for stage in stage_files
    )
    if log_file.exists():
        success = 0
        errors = 0
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                if "Successfully processed line:" in line:
                    success += 1
                elif "Error processing line:" in line:
                    errors += 1
        metrics["log/success_count"] = success
        metrics["log/error_count"] = errors
    else:
        metrics["log/success_count"] = None
        metrics["log/error_count"] = None
    return metrics


def format_metrics(metrics: Dict[str, Any]) -> str:
    parts = []
    for key in sorted(metrics):
        value = metrics[key]
        if value is None:
            continue
        if isinstance(value, float):
            parts.append(f"{key}={value:.4f}")
        else:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def build_eval_command(
    eval_script: Path,
    checkpoint_path: Path,
    output_dir: Path,
    forwarded_args: List[str],
) -> List[str]:
    forwarded_args = strip_options(
        forwarded_args,
        [
            "--model",
            "--simple_plan_output",
            "--simple_summary_output",
            "--deep_plan_output",
            "--deep_summary_output",
            "--log_file",
            "--output_images_dir",
            "--vllm_log_file",
        ],
    )
    cmd = [sys.executable, "-u", str(eval_script)]
    cmd.extend(["--model", str(checkpoint_path)])
    cmd.extend(["--simple_plan_output", str(output_dir / DEFAULT_OUTPUT_NAMES["simple_plan_output"])])
    cmd.extend(["--simple_summary_output", str(output_dir / DEFAULT_OUTPUT_NAMES["simple_summary_output"])])
    cmd.extend(["--deep_plan_output", str(output_dir / DEFAULT_OUTPUT_NAMES["deep_plan_output"])])
    cmd.extend(["--deep_summary_output", str(output_dir / DEFAULT_OUTPUT_NAMES["deep_summary_output"])])
    cmd.extend(["--log_file", str(output_dir / DEFAULT_OUTPUT_NAMES["log_file"])])
    cmd.extend(["--output_images_dir", str(output_dir / "images")])
    cmd.extend(["--vllm_log_file", str(output_dir / DEFAULT_OUTPUT_NAMES["vllm_log_file"])])
    has_served_model_name = any(
        token == "--served_model_name" or token.startswith("--served_model_name=") for token in forwarded_args
    )
    if not has_served_model_name:
        cmd.extend(["--served_model_name", "local-model"])
    cmd.extend(forwarded_args)
    return cmd


def run_checkpoint_eval(
    step_name: str,
    checkpoint_path: Path,
    output_dir: Path,
    eval_script: Path,
    forwarded_args: List[str],
    clean_output: bool,
) -> subprocess.CompletedProcess:
    if clean_output and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_eval_command(eval_script, checkpoint_path, output_dir, forwarded_args)
    print(f"[{step_name}] running: {' '.join(cmd)}", flush=True)
    log_path = output_dir / "wrapper_stdout.log"
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_file.write(" ".join(cmd) + "\n")
        log_file.flush()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        result = subprocess.run(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
    return result


def main() -> int:
    args, eval_args = parse_args()
    if args.begin_step is not None and args.end_step is not None and args.begin_step > args.end_step:
        print(
            f"--begin-step must be <= --end-step, got {args.begin_step} > {args.end_step}",
            file=sys.stderr,
            flush=True,
        )
        return 2
    checkpoint_root = Path(args.checkpoint_root).expanduser().resolve()
    output_root = (
        Path(args.output_root).expanduser().resolve()
        if args.output_root
        else checkpoint_root.parent / f"{checkpoint_root.name}_eval"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    lock_file = acquire_watch_lock(output_root)
    if lock_file is None:
        return 1
    state_path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else output_root / "watch_state.json"
    )

    run = maybe_init_wandb(args, checkpoint_root)
    keepalive = KeepAlive(
        choose_keepalive_devices(args.keepalive_device),
        args.keepalive_matrix_size,
        args.keepalive_burst_seconds,
        args.keepalive_sleep_seconds,
        args.keepalive_repeats,
    )
    state = load_state(state_path)
    if reset_interrupted_running_checkpoints(state):
        save_state(state_path, state)
    if args.force_reeval:
        if clear_selected_checkpoint_state(state, args.begin_step, args.end_step):
            save_state(state_path, state)
        else:
            print(
                f"No saved checkpoint state matched re-eval range {format_step_range(args.begin_step, args.end_step)}",
                flush=True,
            )
    print(f"Watching checkpoint step range {format_step_range(args.begin_step, args.end_step)}", flush=True)

    try:
        keepalive.start()
        while True:
            state = load_state(state_path)
            candidates = [
                item
                for item in discover_checkpoints(checkpoint_root, args.checkpoint_subdir)
                if step_in_range(item[0], args.begin_step, args.end_step)
            ]
            any_pending = False
            for step, step_name, model_path in candidates:
                checkpoint_state = state["checkpoints"].get(step_name, {})
                status = checkpoint_state.get("status")
                last_attempt_at = float(checkpoint_state.get("last_attempt_at", 0) or 0)
                if status == "completed":
                    continue
                if status == "running" and time.time() - last_attempt_at < args.running_timeout_seconds:
                    any_pending = True
                    continue
                ready, snap, reason = checkpoint_ready(model_path, args.ready_wait_seconds)
                if not ready:
                    any_pending = True
                    print(f"[{step_name}] waiting: {reason}", flush=True)
                    continue
                if status == "failed" and time.time() - last_attempt_at < args.failure_retry_seconds:
                    any_pending = True
                    continue

                any_pending = True
                checkpoint_state = {
                    "status": "running",
                    "step": step,
                    "model_path": str(model_path),
                    "snapshot": snap,
                    "last_attempt_at": time.time(),
                }
                state["checkpoints"][step_name] = checkpoint_state
                save_state(state_path, state)

                keepalive.stop()
                checkpoint_output_dir = output_root / step_name
                try:
                    result = run_checkpoint_eval(
                        step_name=step_name,
                        checkpoint_path=model_path,
                        output_dir=checkpoint_output_dir,
                        eval_script=Path(args.eval_script).expanduser().resolve(),
                        forwarded_args=eval_args,
                        clean_output=True,
                    )
                    metrics = summarize_outputs(
                        checkpoint_output_dir,
                        checkpoint_output_dir / DEFAULT_OUTPUT_NAMES["log_file"],
                    )
                    metrics["checkpoint_step"] = step
                    metrics["checkpoint_name"] = step_name
                    metrics["checkpoint_path"] = str(model_path)
                    metrics["return_code"] = result.returncode
                    metrics["ready_wait_seconds"] = args.ready_wait_seconds
                    metrics["snapshot/latest_mtime_age_seconds"] = (
                        time.time() - snap["latest_mtime"] if snap.get("latest_mtime") else None
                    )
                    metrics["eval/succeeded"] = int(result.returncode == 0)
                    if result.returncode != 0:
                        checkpoint_state = {
                            **checkpoint_state,
                            "status": "failed",
                            "last_attempt_at": time.time(),
                            "return_code": result.returncode,
                            "metrics": metrics,
                        }
                        state["checkpoints"][step_name] = checkpoint_state
                        save_state(state_path, state)
                        print(f"[{step_name}] eval failed with code {result.returncode}", flush=True)
                    else:
                        checkpoint_state = {
                            **checkpoint_state,
                            "status": "completed",
                            "completed_at": time.time(),
                            "return_code": result.returncode,
                            "metrics": metrics,
                        }
                        state["checkpoints"][step_name] = checkpoint_state
                        save_state(state_path, state)
                        print(f"[{step_name}] eval done: {format_metrics(metrics)}", flush=True)
                    if run is not None:
                        wandb.log({k: v for k, v in metrics.items() if v is not None}, step=step)
                finally:
                    keepalive = KeepAlive(
                        choose_keepalive_devices(args.keepalive_device),
                        args.keepalive_matrix_size,
                        args.keepalive_burst_seconds,
                        args.keepalive_sleep_seconds,
                        args.keepalive_repeats,
                    )
                    keepalive.start()
            if args.once:
                break
            if not any_pending:
                print(
                    f"No checkpoints found in range {format_step_range(args.begin_step, args.end_step)} yet; waiting.",
                    flush=True,
                )
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        print("Interrupted.", flush=True)
    finally:
        keepalive.stop()
        if run is not None:
            wandb.finish()
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
