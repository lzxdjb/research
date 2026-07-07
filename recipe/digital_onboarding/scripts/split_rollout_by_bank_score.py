#!/usr/bin/env python3
"""Split rollout JSONL files by binary real-bank success score."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _bank_success(row: dict[str, Any]) -> bool:
    return bool(row.get("bank_submit_success"))


def split_rollout_dir(input_dir: Path, output_dir: Path, *, overwrite_score: bool = True) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = {"rows": 0, "score_0": 0, "score_1": 0, "files": 0}
    sources = [path for path in input_dir.glob("[0-9]*.jsonl") if path.stem.isdigit()]
    for source in sorted(sources, key=lambda path: int(path.stem)):
        zero_path = output_dir / f"{source.stem}_score0.jsonl"
        one_path = output_dir / f"{source.stem}_score1.jsonl"
        with source.open("r", encoding="utf-8") as src, zero_path.open("w", encoding="utf-8") as zero_out, one_path.open(
            "w", encoding="utf-8"
        ) as one_out:
            counts["files"] += 1
            for line in src:
                if not line.strip():
                    continue
                row = json.loads(line)
                score = 1.0 if _bank_success(row) else 0.0
                if overwrite_score:
                    row["score"] = score
                    row["bank_rule_score"] = score
                    row["reward_backend"] = "bank_rule_only_rescored"
                    row["format_penalty"] = 0.0
                    line = json.dumps(row, ensure_ascii=False) + "\n"
                if score >= 1.0:
                    one_out.write(line)
                    counts["score_1"] += 1
                else:
                    zero_out.write(line)
                    counts["score_0"] += 1
                counts["rows"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir_pos", nargs="?", type=Path)
    parser.add_argument("--input_dir", "--input-dir", dest="input_dir", type=Path)
    parser.add_argument("--output_dir", "--output-dir", dest="output_dir", type=Path, default=None)
    parser.add_argument("--keep-original-score", action="store_true")
    args = parser.parse_args()

    input_dir = args.input_dir or args.input_dir_pos
    if input_dir is None:
        parser.error("provide input_dir or --input_dir")

    output_dir = args.output_dir or input_dir
    counts = split_rollout_dir(input_dir, output_dir, overwrite_score=not args.keep_original_score)
    print(json.dumps({"output_dir": str(output_dir), **counts}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
