#!/usr/bin/env python3
"""Evaluate decoded rollout text with the onboarding rule reward."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from recipe.digital_onboarding.reward_function import compute_score


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("rollout_jsonl", help="JSONL containing response/solution_str/text fields.")
    args = parser.parse_args()

    scores = []
    for row in _iter_jsonl(Path(args.rollout_jsonl).expanduser()):
        solution = row.get("solution_str") or row.get("response") or row.get("text") or ""
        ground_truth = row.get("ground_truth") or row.get("reward_model", {}).get("ground_truth")
        extra_info = row.get("extra_info", {})
        score = compute_score(
            data_source=row.get("data_source", "digital_onboarding"),
            solution_str=solution,
            ground_truth=ground_truth,
            extra_info=extra_info,
        )
        scores.append(score)

    if not scores:
        print("No rows found.")
        return
    mean = sum(item["score"] for item in scores) / len(scores)
    submitted = sum(item["submitted"] for item in scores) / len(scores)
    info = sum(item["info_completion"] for item in scores) / len(scores)
    print(json.dumps({"count": len(scores), "mean_score": mean, "submit_rate": submitted, "info_completion": info}, indent=2))


if __name__ == "__main__":
    main()

