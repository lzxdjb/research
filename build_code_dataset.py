import json
import os
from pathlib import Path

import pandas as pd
from datasets import load_dataset

OUT = Path(os.environ.get("APPS_OUTPUT_DIR", "./data/code_apps"))
OUT.mkdir(parents=True, exist_ok=True)
CACHE_DIR = Path(os.environ.get("APPS_CACHE_DIR", OUT / ".hf_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MAX_TRAIN = 5000
MAX_VAL = 100

APPS_DATA_BASE_URL = os.environ.get(
    "APPS_DATA_BASE_URL",
    "https://huggingface.co/datasets/codeparrot/apps/resolve/main",
)


def get_apps_data_files():
    """Return raw APPS JSONL files for the built-in datasets JSON loader."""
    train_file = os.environ.get("APPS_TRAIN_FILE")
    test_file = os.environ.get("APPS_TEST_FILE")
    data_dir = os.environ.get("APPS_DATA_DIR")

    if train_file or test_file:
        if not train_file or not test_file:
            raise ValueError("Set both APPS_TRAIN_FILE and APPS_TEST_FILE, or neither.")
        return {"train": train_file, "test": test_file}

    if data_dir:
        data_dir = Path(data_dir)
        return {"train": str(data_dir / "train.jsonl"), "test": str(data_dir / "test.jsonl")}

    base_url = APPS_DATA_BASE_URL.rstrip("/")
    return {"train": f"{base_url}/train.jsonl", "test": f"{base_url}/test.jsonl"}


def load_apps_dataset():
    data_files = get_apps_data_files()
    try:
        return load_dataset("json", data_files=data_files, cache_dir=str(CACHE_DIR))
    except Exception as exc:
        raise RuntimeError(
            "Failed to load APPS raw JSONL files with the built-in JSON loader. "
            "The old codeparrot/apps Python loading script is no longer supported "
            "by current versions of datasets. If this machine cannot reach the "
            "Hugging Face Hub, download train.jsonl and test.jsonl from the APPS "
            "dataset repo and rerun with APPS_DATA_DIR=/path/to/apps, or set "
            "APPS_TRAIN_FILE and APPS_TEST_FILE explicitly."
        ) from exc


def parse_tests(raw):
    if raw is None:
        return None
    if isinstance(raw, dict):
        tests = raw
    else:
        try:
            tests = json.loads(raw)
        except Exception:
            return None

    if not isinstance(tests, dict):
        return None
    if "inputs" not in tests or "outputs" not in tests:
        return None
    if not tests["inputs"] or not tests["outputs"]:
        return None
    if len(tests["inputs"]) != len(tests["outputs"]):
        return None
    return tests


def make_prompt(question, starter_code=""):
    instruction = (
        "Solve the following Python programming problem. "
        "Return only the final answer as executable Python code inside one "
        "```python code block. Do not include extra explanation."
    )

    if starter_code:
        return (
            f"{instruction}\n\n"
            f"Problem:\n{question}\n\n"
            f"Starter code:\n```python\n{starter_code}\n```"
        )

    return f"{instruction}\n\nProblem:\n{question}"


def get_problem_id(ex):
    return ex.get("problem_id", ex.get("id"))


def serialize_tests(tests):
    return json.dumps(tests, ensure_ascii=False)


def convert(ds, split, max_rows):
    rows = []

    for raw_idx, ex in enumerate(ds[split]):
        tests = parse_tests(ex.get("input_output"))
        if tests is None:
            continue

        question = (ex.get("question") or "").strip()
        if not question:
            continue

        starter_code = ex.get("starter_code") or ""
        prompt = make_prompt(question, starter_code)

        rows.append(
            {
                "data_source": "apps",
                "prompt": [{"role": "user", "content": prompt}],
                "ability": "code",
                "domain": "code",
                "metric_data_source": "code",
                "agent_name": "single_turn_agent",
                "reward_model": {
                    "style": "rule",
                    "ground_truth": serialize_tests(tests),
                },
                "extra_info": {
                    "split": split,
                    "index": len(rows),
                    "raw_index": raw_idx,
                    "domain": "code",
                    "metric_data_source": "code",
                    "problem_id": get_problem_id(ex),
                    "difficulty": ex.get("difficulty"),
                    "url": ex.get("url"),
                },
            }
        )

        if max_rows is not None and len(rows) >= max_rows:
            break

    return rows


def main():
    ds = load_apps_dataset()

    train_rows = convert(ds, "train", MAX_TRAIN)
    val_rows = convert(ds, "test", MAX_VAL)

    pd.DataFrame(train_rows).to_parquet(OUT / "train.parquet", index=False)
    pd.DataFrame(val_rows).to_parquet(OUT / "test.parquet", index=False)

    pd.DataFrame(train_rows).to_json(OUT / "train.jsonl", orient="records", lines=True, force_ascii=False)
    pd.DataFrame(val_rows).to_json(OUT / "test.jsonl", orient="records", lines=True, force_ascii=False)

    print(f"Wrote {len(train_rows)} train rows to {OUT / 'train.parquet'}")
    print(f"Wrote {len(val_rows)} val rows to {OUT / 'test.parquet'}")


if __name__ == "__main__":
    main()
