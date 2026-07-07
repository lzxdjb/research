#!/usr/bin/env python3
"""Build prompt/scenario data for RL-training the simulated user."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from recipe.digital_onboarding.prompts import CUSTOMER_SIMULATOR_SYSTEM_PROMPT
from recipe.digital_onboarding.scenario import make_scenarios
from recipe.digital_onboarding.sim_user_probes import ASSISTANT_PROBES


def _row(scenario: dict, probe: str, index: int, split: str) -> dict:
    scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
    ground_truth = {"scenario": scenario, "probe": probe}
    prompt = (
        f"Hidden scenario JSON:\n{scenario_json}\n\n"
        f"Recent chat history:\n"
        f"User: {scenario['initial_user_utterance']}\n"
        f"Assistant: {probe}\n\n"
        'Return only JSON: {"response": "..."}'
    )
    return {
        "data_source": "digital_onboarding_sim_user",
        "prompt": [
            {"role": "system", "content": CUSTOMER_SIMULATOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "ability": "simulated_user",
        "reward_model": {"style": "rule", "ground_truth": json.dumps(ground_truth, ensure_ascii=False)},
        "extra_info": {
            "split": split,
            "index": index,
            "scenario_json": scenario_json,
            "probe": probe,
        },
    }


def _write_jsonl(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_parquet(rows: list[dict], path: Path) -> None:
    import datasets

    datasets.Dataset.from_list(rows).to_parquet(str(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="data/digital_onboarding")
    parser.add_argument("--train-scenarios", type=int, default=32)
    parser.add_argument("--val-scenarios", type=int, default=8)
    parser.add_argument("--seed", type=int, default=41)
    parser.add_argument(
        "--behavior-mode",
        default="cooperative",
        help=(
            "User behavior distribution: cooperative, mixed, finishable, unfinishable, "
            "or a specific scenario behavior."
        ),
    )
    parser.add_argument("--jsonl-only", action="store_true")
    args = parser.parse_args()

    out = Path(args.output_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    for split, count in [("train", args.train_scenarios), ("val", args.val_scenarios)]:
        rows = []
        scenarios = make_scenarios(
            count,
            split=f"sim_rl_{split}",
            seed=args.seed,
            behavior_mode=args.behavior_mode,
        )
        for i, scenario in enumerate(scenarios):
            for j, probe in enumerate(ASSISTANT_PROBES):
                rows.append(_row(scenario, probe, i * len(ASSISTANT_PROBES) + j, split))
        _write_jsonl(rows, out / f"sim_user_rl_{split}.jsonl")
        if not args.jsonl_only:
            _write_parquet(rows, out / f"sim_user_rl_{split}.parquet")
    print(f"Wrote simulator RL data to {out}")


if __name__ == "__main__":
    main()
