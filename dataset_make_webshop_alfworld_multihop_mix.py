#!/usr/bin/env python3
"""Build a mixed train parquet from MultiHop, ALFWorld, and WebShop data.

Validation is intentionally not mixed by this script.  The mixed-domain run
script points validation at the original per-domain validation files through
``data.multi_val_files``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_MULTIHOP_TRAIN = Path(
    "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/multihop_mix/train_mix.parquet"
)
DEFAULT_ALFWORLD_TRAIN = Path(
    "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/alfworld/alfworld_train.parquet"
)
DEFAULT_WEBSHOP_TRAIN = Path(
    "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/webshop/webshop_train.parquet"
)
DEFAULT_OUTPUT = Path(
    "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/research/data/mixed_webshop_alfworld_multihop/train_mix.parquet"
)

REQUIRED_COLUMNS = {"data_source", "agent_name", "prompt", "reward_model", "extra_info"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mix MultiHop, ALFWorld, and WebShop train datasets with independent sample counts."
    )
    parser.add_argument("--multihop-train", type=Path, default=DEFAULT_MULTIHOP_TRAIN)
    parser.add_argument("--alfworld-train", type=Path, default=DEFAULT_ALFWORLD_TRAIN)
    parser.add_argument("--webshop-train", type=Path, default=DEFAULT_WEBSHOP_TRAIN)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--multihop-samples", type=int, default=-1, help="Rows to draw from MultiHop; -1 means all.")
    parser.add_argument("--alfworld-samples", type=int, default=-1, help="Rows to draw from ALFWorld; -1 means all.")
    parser.add_argument("--webshop-samples", type=int, default=-1, help="Rows to draw from WebShop; -1 means all.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--sample-mode",
        choices=("random", "head"),
        default="random",
        help="Use random sampling or the first N rows within each domain.",
    )
    parser.add_argument(
        "--allow-repeat",
        action="store_true",
        help="Sample with replacement when a requested count exceeds the available rows.",
    )
    parser.add_argument(
        "--no-shuffle",
        action="store_true",
        help="Keep domain blocks in MultiHop, ALFWorld, WebShop order after sampling.",
    )
    parser.add_argument(
        "--write-jsonl",
        action="store_true",
        help="Also write a JSONL copy next to the parquet output.",
    )
    return parser.parse_args()


def _read_parquet(path: Path, domain_name: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"{domain_name} train file not found: {path}")

    df = pd.read_parquet(path)
    missing = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(f"{domain_name} train file is missing required columns: {missing}")
    return df


def _sample_domain(
    df: pd.DataFrame,
    *,
    count: int,
    seed: int,
    mode: str,
    allow_repeat: bool,
    domain_name: str,
) -> pd.DataFrame:
    if count < -1:
        raise ValueError(f"{domain_name} sample count must be -1 or non-negative, got {count}")
    if count == -1:
        count = len(df)
    if count == 0:
        return df.iloc[0:0].copy()
    if count > len(df) and not allow_repeat:
        raise ValueError(
            f"Requested {count} {domain_name} samples, but only {len(df)} rows are available. "
            "Use --allow-repeat to sample with replacement."
        )
    if mode == "head":
        if count <= len(df):
            return df.head(count).copy()
        repeats = count // len(df)
        remainder = count % len(df)
        return pd.concat([df] * repeats + [df.head(remainder)], ignore_index=True)
    return df.sample(n=count, random_state=seed, replace=count > len(df)).reset_index(drop=True)


def _as_extra_info(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    return {"raw_extra_info": value}


def _tag_domain(df: pd.DataFrame, *, domain: str, metric_data_source: str) -> pd.DataFrame:
    df = df.copy()
    df["domain"] = domain
    df["metric_data_source"] = metric_data_source
    if "ability" not in df.columns:
        df["ability"] = domain
    df["extra_info"] = df["extra_info"].map(_as_extra_info)
    return df


def _renumber_extra_info(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    new_extra_infos = []
    for new_index, extra_info in enumerate(df["extra_info"].tolist()):
        info = _as_extra_info(extra_info)
        if "index" in info:
            info["source_index"] = info["index"]
        info["index"] = int(new_index)
        info["domain"] = str(df["domain"].iloc[new_index])
        info["metric_data_source"] = str(df["metric_data_source"].iloc[new_index])
        new_extra_infos.append(info)
    df["extra_info"] = new_extra_infos
    return df


def _write_outputs(df: pd.DataFrame, output: Path, manifest: dict[str, Any], write_jsonl: bool) -> None:
    if output.suffix != ".parquet":
        output = output / "train_mix.parquet"
    output.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output, index=False)

    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    if write_jsonl:
        df.to_json(output.with_suffix(".jsonl"), orient="records", lines=True, force_ascii=False)

    print(f"Wrote mixed train parquet: {output}")
    print(f"Wrote manifest: {manifest_path}")


def main() -> None:
    args = _parse_args()

    raw_inputs = {
        "multihop": _read_parquet(args.multihop_train, "MultiHop"),
        "alfworld": _read_parquet(args.alfworld_train, "ALFWorld"),
        "webshop": _read_parquet(args.webshop_train, "WebShop"),
    }
    requested_counts = {
        "multihop": args.multihop_samples,
        "alfworld": args.alfworld_samples,
        "webshop": args.webshop_samples,
    }

    frames = [
        _tag_domain(
            _sample_domain(
                raw_inputs["multihop"],
                count=args.multihop_samples,
                seed=args.seed,
                mode=args.sample_mode,
                allow_repeat=args.allow_repeat,
                domain_name="MultiHop",
            ),
            domain="multihop",
            metric_data_source="multihop",
        ),
        _tag_domain(
            _sample_domain(
                raw_inputs["alfworld"],
                count=args.alfworld_samples,
                seed=args.seed + 1,
                mode=args.sample_mode,
                allow_repeat=args.allow_repeat,
                domain_name="ALFWorld",
            ),
            domain="alfworld",
            metric_data_source="alfworld",
        ),
        _tag_domain(
            _sample_domain(
                raw_inputs["webshop"],
                count=args.webshop_samples,
                seed=args.seed + 2,
                mode=args.sample_mode,
                allow_repeat=args.allow_repeat,
                domain_name="WebShop",
            ),
            domain="webshop",
            metric_data_source="webshop",
        ),
    ]

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        raise ValueError("No rows selected. At least one domain sample count must be non-zero.")

    mixed = pd.concat(frames, ignore_index=True, sort=False)
    if not args.no_shuffle:
        mixed = mixed.sample(frac=1.0, random_state=args.seed).reset_index(drop=True)
    mixed = _renumber_extra_info(mixed)

    counts = mixed["domain"].value_counts().sort_index().to_dict()
    manifest = {
        "seed": args.seed,
        "sample_mode": args.sample_mode,
        "shuffled": not args.no_shuffle,
        "allow_repeat": args.allow_repeat,
        "requested_counts": requested_counts,
        "available_counts": {name: int(len(df)) for name, df in raw_inputs.items()},
        "selected_counts": {name: int(counts.get(name, 0)) for name in raw_inputs},
        "source_files": {
            "multihop": str(args.multihop_train),
            "alfworld": str(args.alfworld_train),
            "webshop": str(args.webshop_train),
        },
    }
    _write_outputs(mixed, args.output, manifest, args.write_jsonl)

    print("Selected rows:")
    for domain in ("multihop", "alfworld", "webshop"):
        print(f"  {domain}: {counts.get(domain, 0)}")
    print(f"  total: {len(mixed)}")
    print(f"Columns: {list(mixed.columns)}")


if __name__ == "__main__":
    main()
