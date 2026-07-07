#!/usr/bin/env python3
"""Build RL scenario data for digital-onboarding GRPO."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from recipe.digital_onboarding.real_bank import prepare_real_bank_scenario
from recipe.digital_onboarding.scenario import SYSTEM_PROMPT, make_scenarios


def _row(scenario: dict, index: int, tool_backend: str = "simulator") -> dict:
    if tool_backend in {"real_bank", "bank", "open_account", "open-account"}:
        scenario = prepare_real_bank_scenario(scenario, request_id=None, force_unique_identity=True)
    scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
    tools_kwargs = {"__onboarding_scenario_json__": scenario_json}
    interaction_kwargs = {"name": "onboarding_user", "scenario_json": scenario_json}
    if tool_backend != "simulator":
        tools_kwargs["__onboarding_tool_backend__"] = tool_backend
        interaction_kwargs["tool_backend"] = tool_backend
    return {
        "data_source": "digital_onboarding",
        "prompt": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": scenario["initial_user_utterance"]},
        ],
        "ability": "tool_use_onboarding",
        "reward_model": {"style": "rule", "ground_truth": scenario_json},
        "extra_info": {
            "split": scenario["split"],
            "index": index,
            "need_tools_kwargs": True,
            "tools_kwargs": tools_kwargs,
            "interaction_kwargs": interaction_kwargs,
            "scenario_json": scenario_json,
        },
        "agent_name": "tool_agent",
    }


def _write_jsonl(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_parquet(rows: list[dict], path: Path) -> None:
    try:
        import datasets
    except ImportError as exc:
        raise RuntimeError("Install datasets/pyarrow or use --jsonl-only.") from exc
    ds = datasets.Dataset.from_list(rows)
    ds.to_parquet(str(path))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="data/digital_onboarding")
    parser.add_argument("--train-size", type=int, default=1024)
    parser.add_argument("--val-size", type=int, default=128)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--behavior-mode",
        default="cooperative",
        help=(
            "User behavior distribution: cooperative, mixed, finishable, unfinishable, "
            "phase1 (cooperative-only), phase2 (unfinishable-only), or a specific scenario behavior."
        ),
    )
    parser.add_argument(
        "--branch-mode",
        default=os.environ.get("DIGITAL_ONBOARDING_BRANCH_MODE", "us_market"),
        choices=[
            "mixed",
            "us_market",
            "domestic",
            "us_pr",
            "us_visa",
            "foreigner",
            "legacy_mixed",
        ],
        help="Onboarding branch distribution to generate. mixed/us_market covers U.S. citizen, U.S. PR, and U.S. visa residents.",
    )
    parser.add_argument(
        "--tool-backend",
        default=os.environ.get("DIGITAL_ONBOARDING_TOOL_BACKEND", "simulator"),
        choices=["simulator", "real_bank", "bank", "open_account", "open-account"],
        help="Tool backend to stamp into dataset rows.",
    )
    parser.add_argument(
        "--upload-image-path",
        default=os.environ.get("DIGITAL_ONBOARDING_UPLOAD_IMAGE_PATH", ""),
        help="Accepted for backward compatibility; ignored. Customer upload is represented by the upload action.",
    )
    parser.add_argument(
        "--images-dir",
        default=os.environ.get("DIGITAL_ONBOARDING_IMAGES_DIR", ""),
        help="Accepted for backward compatibility; ignored.",
    )
    parser.add_argument(
        "--no-upload-image",
        action="store_true",
        help="Accepted for backward compatibility; ignored.",
    )
    parser.add_argument("--jsonl-only", action="store_true")
    args = parser.parse_args()

    out = Path(args.output_dir).expanduser()
    print(f"[INFO] Output dir: {out}")
    out.mkdir(parents=True, exist_ok=True)

    for split, count in [("train", args.train_size), ("val", args.val_size)]:
        scenarios = make_scenarios(
            count,
            split=split,
            seed=args.seed,
            behavior_mode=args.behavior_mode,
            branch_mode=args.branch_mode,
        )
        rows = [_row(scenario, i, tool_backend=args.tool_backend) for i, scenario in enumerate(scenarios)]
        _write_jsonl(rows, out / f"{split}.jsonl")
        if not args.jsonl_only:
            _write_parquet(rows, out / f"{split}.parquet")

    print(f"Wrote digital onboarding data to {os.fspath(out)}")


if __name__ == "__main__":
    main()
