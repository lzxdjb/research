"""
make_webshop_rl_dataset.py
──────────────────────────
Builds train / test JSONL + Parquet files for WebShop multi-turn RL,
guaranteeing that the goals actually exist in the loaded search engine.
"""

import json
import os
import random
from pathlib import Path

import pandas as pd

# ── Paths ────────────────────────────────────────────────────────────────────
WEBSHOP_DATA = os.path.expanduser(
    os.environ.get(
        "WEBSHOP_DATA",
        "/cpfs01/nlp/leizhengxing/stock-rl/webshop/data",
    )
)

SYSTEM_PROMPT = open(
    Path(__file__).parent / "system_prompt_webshop.md"
).read().strip()


# ── Scenario loading ──────────────────────────────────────────────────────────

def load_webshop_goals(split: str = "train", num_products: int = 1000):
    base_dir = Path(WEBSHOP_DATA)
    
    # 1. Read the full products list to find the first N ASINs
    products_path = base_dir / "items_shuffle.json"
    with open(products_path, 'r') as f:
        all_products = json.load(f)
    
    # The server truncates to the first `num_products`, so we must too!
    valid_asins = set([p["asin"] for p in all_products[:num_products]])

    # 2. Load the instructions and filter
    ins_path = base_dir / "items_ins_v2.json"
    with open(ins_path) as f:
        raw = json.load(f)

    goals = []
    if isinstance(raw, dict):
        for asin, item_data in raw.items():
            if asin not in valid_asins: # STRICTLY REQUIRE IT TO BE IN THE 5000
                continue
            if not isinstance(item_data, dict): continue
            
            if "instruction" in item_data:
                instruction = item_data["instruction"].strip()
                if instruction:
                    goals.append({"instruction": instruction, "asin": asin, "attributes": item_data.get("attributes", [])})
                    
    # Deduplicate strictly by instruction + target ASIN
    unique_goals = {}
    for g in goals:
        key = f"{g['instruction']}|||{g['asin']}"
        unique_goals[key] = g
    goals = list(unique_goals.values())

    random.shuffle(goals)
    cut = int(len(goals) * 0.95)
    return goals[:cut] if split == "train" else goals[cut:]
def _synthetic_goals(n: int) :
    templates = [
        "i am looking for a wireless mouse that is ergonomic and under 30 dollars",
        "i need a 16 oz bottle of organic coconut oil",
    ]
    return [{"instruction": templates[i % len(templates)], "asin": f"SYN{i:05d}", "attributes": {}} for i in range(n)]


# ── Sample construction ───────────────────────────────────────────────────────

def make_sample(idx: int, goal_entry: dict, split: str) -> dict:
    instruction = goal_entry["instruction"]
    asin        = goal_entry.get("asin", "")
    attributes  = goal_entry.get("attributes", {})

    prompt = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Your task is: {instruction}\n\n"
                "Start by searching for the product, then navigate the results "
                "to find and purchase the best matching item."
            ),
        },
    ]

    return {
        "data_source":  "webshop_textworld",
        "agent_name":   "webshop_agent",
        "prompt":       prompt,
        "reward_model": {
            "ground_truth": asin,
            "style":        "rule",
        },
        "extra_info": {
            "ground_truth": asin,
            "goal":         instruction,
            "attributes":   attributes,
            "index":        idx,
            "split":        split,
            "need_tools_kwargs": True,
            "tools_kwargs": {
                "calc_webshop_reward": {
                    "create_kwargs": {
                        "ground_truth": asin,
                        "attributes":   attributes,
                    }
                },
                "EnvStep": {
                    "create_kwargs": {
                        "session_id": asin,
                        "goal":       instruction,
                        "asin":       asin,
                        "attributes": attributes,
                    }
                },
            },
        },
        "ability": "embodied_task_completion",
    }


# ── Output helpers ─────────────────────────────────────────────────────────────

def write_outputs(samples, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"{name}.jsonl"
    with open(jsonl_path, "w") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"  {len(samples):>5} rows → {jsonl_path}")
    pd.DataFrame(samples).to_parquet(out_dir / f"{name}.parquet", index=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    random.seed(42)

    train_goals = load_webshop_goals("train")
    test_goals  = load_webshop_goals("test")

    # Cap sizes (match ALFWorld defaults)
    train_goals = train_goals[:2500]
    test_goals  = test_goals

    train_samples = [make_sample(i, g, "train") for i, g in enumerate(train_goals)]
    test_samples  = [
        make_sample(i + len(train_samples), g, "test")
        for i, g in enumerate(test_goals)
    ]

    out_dir = Path(__file__).parent / "data/webshop"
    write_outputs(train_samples, out_dir, "webshop_train_1000")
    write_outputs(test_samples,  out_dir, "webshop_test_1000")


if __name__ == "__main__":
    main()