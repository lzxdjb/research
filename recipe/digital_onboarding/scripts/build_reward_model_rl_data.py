#!/usr/bin/env python3
"""Convert 122B teacher labels into RL data for training R_phi."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from recipe.digital_onboarding.prompts import REWARD_MODEL_SYSTEM_PROMPT


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    import datasets

    datasets.Dataset.from_list(rows).to_parquet(str(path))


def _score_prompt(row: dict[str, Any]) -> str:
    return (
        "Hidden scenario summary, for judging only:\n"
        f"{json.dumps(row.get('scenario_summary', {}), ensure_ascii=False, sort_keys=True)}\n\n"
        "Full service trajectory:\n"
        f"{row.get('trajectory', '')}\n\n"
        "Return the JSON score now."
    )


def _pairwise_prompt(row_a: dict[str, Any], row_b: dict[str, Any]) -> str:
    scenario = row_a.get("scenario_summary", {})
    return (
        "Hidden scenario summary, for judging only:\n"
        f"{json.dumps(scenario, ensure_ascii=False, sort_keys=True)}\n\n"
        "Trajectory A:\n"
        f"{row_a.get('trajectory', '')}\n\n"
        "Trajectory B:\n"
        f"{row_b.get('trajectory', '')}\n\n"
        'Which trajectory is better? Return JSON only: {"winner": "A" or "B" or "TIE", "reason": "...", "score_a": float, "score_b": float}'
    )


def _score_row(row: dict[str, Any], index: int, split: str) -> dict[str, Any]:
    label = row.get("teacher_label", {})
    ground_truth = {
        "task": "score",
        "teacher_label": label,
        "score": row.get("score", label.get("score", 0.0)),
        "scenario_id": row.get("scenario_id"),
    }
    return {
        "data_source": "digital_onboarding_reward_model",
        "prompt": [
            {"role": "system", "content": REWARD_MODEL_SYSTEM_PROMPT},
            {"role": "user", "content": _score_prompt(row)},
        ],
        "ability": "reward_model_training",
        "reward_model": {"style": "teacher_label", "ground_truth": json.dumps(ground_truth, ensure_ascii=False)},
        "extra_info": {"split": split, "index": index, "scenario_id": row.get("scenario_id"), "task": "score"},
    }


def _pairwise_row(row_a: dict[str, Any], row_b: dict[str, Any], index: int, split: str) -> dict[str, Any]:
    score_a = float(row_a.get("score", row_a.get("teacher_label", {}).get("score", 0.0)))
    score_b = float(row_b.get("score", row_b.get("teacher_label", {}).get("score", 0.0)))
    if abs(score_a - score_b) < 0.05:
        winner = "TIE"
    else:
        winner = "A" if score_a > score_b else "B"
    ground_truth = {
        "task": "pairwise",
        "winner": winner,
        "score_a": score_a,
        "score_b": score_b,
        "scenario_id": row_a.get("scenario_id"),
    }
    return {
        "data_source": "digital_onboarding_reward_model_pairwise",
        "prompt": [
            {"role": "system", "content": REWARD_MODEL_SYSTEM_PROMPT},
            {"role": "user", "content": _pairwise_prompt(row_a, row_b)},
        ],
        "ability": "reward_model_training",
        "reward_model": {"style": "teacher_label", "ground_truth": json.dumps(ground_truth, ensure_ascii=False)},
        "extra_info": {"split": split, "index": index, "scenario_id": row_a.get("scenario_id"), "task": "pairwise"},
    }


def _split_rows(rows: list[dict[str, Any]], val_ratio: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    if len(rows) == 1:
        return rows, rows
    val_count = max(1, int(len(rows) * val_ratio)) if len(rows) > 1 else 0
    return rows[val_count:], rows[:val_count]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/digital_onboarding/teacher_labels/reward_teacher.jsonl")
    parser.add_argument("--output-dir", default="data/digital_onboarding")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--max-pairwise-per-scenario", type=int, default=8)
    parser.add_argument("--jsonl-only", action="store_true")
    args = parser.parse_args()

    source_rows = list(_iter_jsonl(Path(args.input).expanduser()))
    if not source_rows:
        raise RuntimeError(f"No reward teacher labels found in {args.input}.")
    score_rows = [_score_row(row, i, "all") for i, row in enumerate(source_rows)]

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        grouped[str(row.get("scenario_id", "unknown"))].append(row)

    pairwise_rows: list[dict[str, Any]] = []
    for scenario_id, group in grouped.items():
        group = sorted(group, key=lambda item: float(item.get("score", 0.0)))
        made = 0
        for low_index, low in enumerate(group):
            for high in reversed(group[low_index + 1 :]):
                if made >= args.max_pairwise_per_scenario:
                    break
                if abs(float(high.get("score", 0.0)) - float(low.get("score", 0.0))) < 0.05:
                    continue
                pairwise_rows.append(_pairwise_row(high, low, len(pairwise_rows), "all"))
                made += 1
            if made >= args.max_pairwise_per_scenario:
                break

    all_rows = score_rows + pairwise_rows
    train_rows, val_rows = _split_rows(all_rows, args.val_ratio)
    out = Path(args.output_dir).expanduser()
    _write_jsonl(train_rows, out / "reward_model_rl_train.jsonl")
    _write_jsonl(val_rows, out / "reward_model_rl_val.jsonl")
    if not args.jsonl_only:
        _write_parquet(train_rows, out / "reward_model_rl_train.parquet")
        _write_parquet(val_rows, out / "reward_model_rl_val.parquet")
    print(
        json.dumps(
            {
                "source_rows": len(source_rows),
                "score_rows": len(score_rows),
                "pairwise_rows": len(pairwise_rows),
                "train_rows": len(train_rows),
                "val_rows": len(val_rows),
                "output_dir": str(out),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
