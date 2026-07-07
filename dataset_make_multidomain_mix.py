"""Merge multi-hop QA, Math, and Code RL datasets into one domain-labeled mix.

This script intentionally does not download Math or Code data. It expects those
datasets to already be in Verl RL parquet/jsonl format, because this repository
already supports several Math and Code reward data sources. The script only
normalizes domain labels and agent-loop routing, then writes mixed train/val
files for multi-domain GRPO experiments.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


DOMAIN_MULTI_HOP = "multi_hop_qa"
DOMAIN_MATH = "math"
DOMAIN_CODE = "code"


def _read_table(path: str | None) -> pd.DataFrame:
    if not path:
        return pd.DataFrame()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"dataset file not found: {p}")
    if p.suffix == ".parquet":
        return pd.read_parquet(p)
    if p.suffix in {".json", ".jsonl"}:
        return pd.read_json(p, lines=p.suffix == ".jsonl")
    raise ValueError(f"unsupported dataset format for {p}; expected .parquet/.json/.jsonl")


def _write_table(df: pd.DataFrame, stem: Path) -> None:
    stem.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(stem.with_suffix(".parquet"), index=False)
    df.to_json(stem.with_suffix(".jsonl"), orient="records", lines=True, force_ascii=False)


def _ensure_extra_info_domain(extra_info, domain: str):
    if not isinstance(extra_info, dict):
        extra_info = {}
    extra_info = dict(extra_info)
    extra_info["domain"] = domain
    extra_info["metric_data_source"] = domain
    return extra_info


def _normalize(
    df: pd.DataFrame,
    *,
    domain: str,
    agent_name: str,
    data_source_override: str | None = None,
    max_samples: int | None = None,
    seed: int = 42,
) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    if max_samples is not None and max_samples > 0 and len(df) > max_samples:
        df = df.sample(n=max_samples, random_state=seed).reset_index(drop=True)

    df["domain"] = domain
    df["metric_data_source"] = domain
    df["agent_name"] = agent_name
    if data_source_override:
        df["data_source"] = data_source_override
    if "ability" not in df.columns:
        df["ability"] = domain
    if "extra_info" not in df.columns:
        df["extra_info"] = [{} for _ in range(len(df))]
    df["extra_info"] = df["extra_info"].map(lambda x: _ensure_extra_info_domain(x, domain))
    return df


def _merge(frames: list[pd.DataFrame], *, seed: int) -> pd.DataFrame:
    frames = [df for df in frames if not df.empty]
    if not frames:
        raise ValueError("no input rows were provided")
    out = pd.concat(frames, ignore_index=True)
    out = out.sample(frac=1.0, random_state=seed).reset_index(drop=True)
    out["index"] = range(len(out))
    if "extra_info" in out.columns:
        out["extra_info"] = [
            {**(x if isinstance(x, dict) else {}), "index": int(i)}
            for i, x in enumerate(out["extra_info"].tolist())
        ]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a three-domain RL dataset mix.")
    parser.add_argument("--multihop_train", required=True)
    parser.add_argument("--multihop_val", required=True)
    parser.add_argument("--math_train", default=None)
    parser.add_argument("--math_val", default=None)
    parser.add_argument("--code_train", default=None)
    parser.add_argument("--code_val", default=None)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--max_multihop_train", type=int, default=None)
    parser.add_argument("--max_math_train", type=int, default=None)
    parser.add_argument("--max_code_train", type=int, default=None)
    parser.add_argument("--max_multihop_val", type=int, default=None)
    parser.add_argument("--max_math_val", type=int, default=None)
    parser.add_argument("--max_code_val", type=int, default=None)
    parser.add_argument("--math_data_source", default=None)
    parser.add_argument("--code_data_source", default=None)
    parser.add_argument("--single_turn_agent", default="single_turn_agent")
    parser.add_argument("--multihop_agent", default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    mh_train_raw = _read_table(args.multihop_train)
    mh_agent = args.multihop_agent
    if not mh_agent:
        if "agent_name" in mh_train_raw.columns and len(mh_train_raw) > 0:
            mh_agent = str(mh_train_raw["agent_name"].iloc[0])
        else:
            mh_agent = "hotpot_qa_agent"

    train_frames = [
        _normalize(
            mh_train_raw,
            domain=DOMAIN_MULTI_HOP,
            agent_name=mh_agent,
            max_samples=args.max_multihop_train,
            seed=args.seed,
        ),
        _normalize(
            _read_table(args.math_train),
            domain=DOMAIN_MATH,
            agent_name=args.single_turn_agent,
            data_source_override=args.math_data_source,
            max_samples=args.max_math_train,
            seed=args.seed,
        ),
        _normalize(
            _read_table(args.code_train),
            domain=DOMAIN_CODE,
            agent_name=args.single_turn_agent,
            data_source_override=args.code_data_source,
            max_samples=args.max_code_train,
            seed=args.seed,
        ),
    ]
    train_mix = _merge(train_frames, seed=args.seed)
    _write_table(train_mix, out / "train_mix")

    val_frames = [
        _normalize(
            _read_table(args.multihop_val),
            domain=DOMAIN_MULTI_HOP,
            agent_name=mh_agent,
            max_samples=args.max_multihop_val,
            seed=args.seed,
        ),
        _normalize(
            _read_table(args.math_val),
            domain=DOMAIN_MATH,
            agent_name=args.single_turn_agent,
            data_source_override=args.math_data_source,
            max_samples=args.max_math_val,
            seed=args.seed,
        ),
        _normalize(
            _read_table(args.code_val),
            domain=DOMAIN_CODE,
            agent_name=args.single_turn_agent,
            data_source_override=args.code_data_source,
            max_samples=args.max_code_val,
            seed=args.seed,
        ),
    ]
    val_mix = _merge(val_frames, seed=args.seed)
    _write_table(val_mix, out / "val_mix")

    for name, df in [
        ("val_multihop", val_frames[0]),
        ("val_math", val_frames[1]),
        ("val_code", val_frames[2]),
    ]:
        if not df.empty:
            _write_table(df.reset_index(drop=True), out / name)

    print(f"Wrote train_mix: {len(train_mix)} rows")
    print(f"Wrote val_mix:   {len(val_mix)} rows")
    print("Domain counts:")
    print(train_mix["domain"].value_counts().to_string())


if __name__ == "__main__":
    main()
