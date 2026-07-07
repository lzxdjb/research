#!/usr/bin/env python3
"""Append all rows from one parquet file after the first N rows from another."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Take the first N rows from --first, append all rows from --second, and write parquet."
    )
    parser.add_argument(
        "--first",
        type=Path,
        default=Path("/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/new_train_2/train.parquet"),
        help="Parquet file to sample from the beginning.",
    )
    parser.add_argument(
        "--second",
        type=Path,
        default=Path("/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/gold_train/train.parquet"),
        help="Parquet file to include completely.",
    )
    parser.add_argument(
        "--first-rows",
        type=int,
        default=600,
        help="Number of rows to take from the first parquet file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./data/mixed_new_train_2_600_gold_train_no_shuffle/train.parquet"),
        help="Output parquet path.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed used when --shuffle is enabled.",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Shuffle the combined rows before writing. By default rows stay in curriculum order.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.first_rows < 0:
        raise ValueError("--first-rows must be non-negative")
    if not args.first.exists():
        raise FileNotFoundError(f"First parquet file not found: {args.first}")
    if not args.second.exists():
        raise FileNotFoundError(f"Second parquet file not found: {args.second}")

    first_df = pd.read_parquet(args.first)
    second_df = pd.read_parquet(args.second)

    if list(first_df.columns) != list(second_df.columns):
        raise ValueError(
            "Input parquet columns do not match:\n"
            f"first columns: {list(first_df.columns)}\n"
            f"second columns: {list(second_df.columns)}"
        )
    if args.first_rows > len(first_df):
        raise ValueError(
            f"Requested {args.first_rows} rows from first file, but it only has {len(first_df)} rows."
        )

    first_part = first_df.head(args.first_rows).copy()
    mixed_df = pd.concat([first_part, second_df], ignore_index=True)

    if args.shuffle:
        mixed_df = mixed_df.sample(frac=1, random_state=args.seed).reset_index(drop=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    mixed_df.to_parquet(args.output, index=False)

    print(f"Wrote: {args.output}")
    print(f"Rows from first file: {len(first_part)}")
    print(f"Rows from second file: {len(second_df)}")
    print(f"Total rows: {len(mixed_df)}")
    print(f"Shuffled: {args.shuffle}")
    print(f"Columns: {list(mixed_df.columns)}")


if __name__ == "__main__":
    main()
