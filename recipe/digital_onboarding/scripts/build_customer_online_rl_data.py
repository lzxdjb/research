#!/usr/bin/env python3
"""Build an online C_theta RL buffer from customer turns logged during service rollouts.

This is not an offline teacher dataset. It converts the fresh customer turns
from the latest service/customer interaction round into VERL prompt rows so the
customer simulator can be updated with a 122B teacher reward.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from recipe.digital_onboarding.prompts import CUSTOMER_SIMULATOR_SYSTEM_PROMPT


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    import datasets

    datasets.Dataset.from_list(rows).to_parquet(str(path))


def _format_recent_messages(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages[-10:]:
        role = message.get("role", "unknown")
        content = str(message.get("content", ""))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _row(source: dict[str, Any], index: int, split: str) -> dict[str, Any]:
    scenario = _as_dict(source.get("scenario_json") or source.get("scenario"))
    scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
    recent_messages = source.get("recent_messages") or []
    assistant_request = str(source.get("assistant_request") or "")
    prompt = (
        f"Hidden scenario JSON:\n{scenario_json}\n\n"
        "Recent chat history:\n"
        f"{_format_recent_messages(recent_messages)}\n\n"
        'Return JSON only, for example: {"response": "My email is user@example.com."}'
    )
    ground_truth = {
        "scenario": scenario,
        "probe": assistant_request,
        "source_customer_response": source.get("customer_response", ""),
        "source_backend": source.get("backend", ""),
    }
    return {
        "data_source": "digital_onboarding_sim_user_online",
        "prompt": [
            {"role": "system", "content": CUSTOMER_SIMULATOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "ability": "simulated_user",
        "reward_model": {"style": "teacher_online", "ground_truth": json.dumps(ground_truth, ensure_ascii=False)},
        "extra_info": {
            "split": split,
            "index": index,
            "scenario_id": scenario.get("scenario_id") or source.get("scenario_id"),
            "scenario_json": scenario_json,
            "probe": assistant_request,
            "source_customer_response": source.get("customer_response", ""),
        },
    }


def _split(rows: list[dict[str, Any]], val_ratio: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    if len(rows) == 1:
        return rows, rows
    val_count = max(1, int(len(rows) * val_ratio)) if len(rows) > 1 else 0
    return rows[val_count:], rows[:val_count]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL written by CUSTOMER_ROLLOUT_LOG.")
    parser.add_argument("--output-dir", default="data/digital_onboarding")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--jsonl-only", action="store_true")
    args = parser.parse_args()

    sources = list(_iter_jsonl(Path(args.input).expanduser()))
    if args.limit:
        sources = sources[: args.limit]
    if not sources:
        raise RuntimeError(f"No customer turns found in {args.input}. Did service rollouts set CUSTOMER_ROLLOUT_LOG?")
    rows = [_row(source, i, "all") for i, source in enumerate(sources)]
    train_rows, val_rows = _split(rows, args.val_ratio)

    out = Path(args.output_dir).expanduser()
    _write_jsonl(train_rows, out / "sim_user_online_rl_train.jsonl")
    _write_jsonl(val_rows, out / "sim_user_online_rl_val.jsonl")
    if not args.jsonl_only:
        _write_parquet(train_rows, out / "sim_user_online_rl_train.parquet")
        _write_parquet(val_rows, out / "sim_user_online_rl_val.parquet")

    print(
        json.dumps(
            {
                "source_rows": len(sources),
                "train_rows": len(train_rows),
                "val_rows": len(val_rows),
                "output_dir": str(out),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
