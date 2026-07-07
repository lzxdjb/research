import argparse
import json
import sys
import random
from pathlib import Path

import pandas as pd


SYSTEM_PROMPT = """You are a multi-hop question answering agent. You reason step by step by retrieving evidence from a knowledge base before committing to a final answer. ## Available Tools ### Search - **Purpose**: Retrieve relevant passages from Wikipedia or a local knowledge base. **Note**: Each call returns the top matching passages. Decide after reading them whether further searches are needed. ## Workflow 1. Read the question and identify what facts are needed to answer it. 2. Call the Search tool to retrieve relevant information. 3. If the retrieved passages do not fully answer the question, search again with a more targeted query. 4. Repeat until you have sufficient evidence, then output your final answer. ## Final Answer Format When you have gathered enough evidence to answer the question, output exactly: <FINISHED> Answer: [your answer here] </FINISHED> The answer should be concise and match the question type (person name, place, number, yes/no, etc.). Do not include extra explanation inside the FINISHED block."""

def convert_one(raw: dict) -> dict:
    qid       = raw.get("id", "")
    question  = raw.get("question", "")
    answer    = raw.get("answer", "")
    qtype     = raw.get("type", "")
    sup_facts = raw.get("supporting_facts", {})
    sup_titles = list(dict.fromkeys(sup_facts.get("title", [])))

    return {
        "data_source": "hotpotqa",
        "agent_name":  "hotpot_qa_agent",
        "prompt": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": question},
        ],
        "ability": "multi_hop_qa",
        "reward_model": {
            "ground_truth": answer,
            "style": "rule",
        },
        "extra_info": {
            "ground_truth": answer,
            "question_id": qid,
            "question_type": qtype,
            "supporting_titles": sup_titles,
            "interaction_kwargs": {
                "ground_truth": answer,
            },
            "need_tools_kwargs": True,
            "tools_kwargs": {
                "calc_hotpot_reward": {
                    "create_kwargs": {
                        "ground_truth": answer,
                    }
                }
            },
        },
    }


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Convert HotpotQA to RL format + split")
    parser.add_argument("--input",  required=True)
    parser.add_argument("--output", required=True, help="Base output path")
    parser.add_argument("--max_samples", type=int, default=None)
    parser.add_argument("--val_ratio", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    random.seed(args.seed)

    in_path = Path(args.input)
    base    = Path(args.output)
    base.parent.mkdir(parents=True, exist_ok=True)

    all_rows = []
    converted = 0
    skipped = 0

    # ── Read + convert ───────────────────────────────────────────────────────
    with in_path.open("r", encoding="utf-8") as fin:
        for line_no, line in enumerate(fin):
            if args.max_samples and converted >= args.max_samples:
                break

            line = line.strip()
            if not line:
                continue

            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[WARN] line {line_no}: {e}", file=sys.stderr)
                skipped += 1
                continue

            rl_sample = convert_one(raw)
            rl_sample["extra_info"]["index"] = converted

            all_rows.append(rl_sample)
            converted += 1

    # ── Shuffle ──────────────────────────────────────────────────────────────
    random.shuffle(all_rows)

    # ── Split ────────────────────────────────────────────────────────────────
    val_size = int(len(all_rows) * args.val_ratio)
    val_rows = all_rows[:val_size]
    train_rows = all_rows[val_size:]

    print(f"Split: train={len(train_rows)}, val={len(val_rows)}")

    # ── Write JSONL ──────────────────────────────────────────────────────────
    write_jsonl(base.with_name(base.name + "_train.jsonl"), train_rows)
    write_jsonl(base.with_name(base.name + "_val.jsonl"), val_rows)

    # ── Write Parquet (FIXED: preserve nested structure) ─────────────────────
    train_df = pd.DataFrame(train_rows)
    val_df   = pd.DataFrame(val_rows)
    train_df.to_parquet(base.with_name(base.name + "_train.parquet"), index=False)
    val_df.to_parquet(base.with_name(base.name + "_val.parquet"), index=False)

    print("Done.")
    print(f"  Train JSONL   → {base}_train.jsonl")
    print(f"  Val   JSONL   → {base}_val.jsonl")
    print(f"  Train Parquet → {base}_train.parquet")
    print(f"  Val   Parquet → {base}_val.parquet")
    print(f"  Converted={converted}, Skipped={skipped}")


if __name__ == "__main__":
    main()