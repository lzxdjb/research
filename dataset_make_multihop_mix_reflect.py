"""
Convert and mix HotpotQA / 2WikiMultiHopQA / MuSiQue / Bamboogle into RL format.

Train split : all four datasets merged and shuffled.
Test splits : each dataset kept individually (no val split for test files).

Usage example
-------------
python dataset_make_multihop_mix.py \
    --hotpot_train   /data/hotpotqa/hotpot_train_v1.1.json \
    --wiki2_train    /data/2wikimultihop/train.jsonl \
    --musique_train  /data/musique/musique_ans_v1.0_train.jsonl \
    --bamboogle      /data/bamboogle/bamboogle.jsonl \
    --hotpot_test    /data/hotpotqa/hotpot_dev_distractor_v1.json \
    --wiki2_test     /data/2wikimultihop/dev.jsonl \
    --musique_test   /data/musique/musique_ans_v1.0_dev.jsonl \
    --output_dir     /data/multihop_mix \
    --val_ratio      0.02 \
    --max_per_source 20000 \
    --seed           42

Output
------
  <output_dir>/train_mix.jsonl          (all sources merged)
  <output_dir>/train_mix.parquet
  <output_dir>/val_mix.jsonl            (held-out val from train sources)
  <output_dir>/val_mix.parquet
  <output_dir>/test_hotpotqa.jsonl      (individual test sets)
  <output_dir>/test_2wiki.jsonl
  <output_dir>/test_musique.jsonl
  <output_dir>/test_bamboogle.jsonl
"""

import argparse
import json
import random
import sys
from pathlib import Path

import pandas as pd


# ── System prompt ──────────────────────────────────────────────────────────────

_DEFAULT_SYSTEM_PROMPT = (
    "You are a multi-hop question answering agent. You reason step by step by "
    "retrieving evidence from a knowledge base before committing to a final answer.\n\n"
    "## Available Tools\n"
    "### Search\n"
    "- **Purpose**: Retrieve relevant passages from Wikipedia or a local knowledge base.\n"
    "- **Note**: Each call returns the top matching passages. Decide after reading them "
    "whether further searches are needed.\n\n"
    "## Workflow\n"
    "1. Read the question and identify what facts are needed to answer it.\n"
    "2. Call the Search tool to retrieve relevant information.\n"
    "3. If the retrieved passages do not fully answer the question, search again with a "
    "more targeted query.\n"
    "4. Repeat until you have sufficient evidence, then output your final answer.\n"
    "5. If you receive a skeptical follow-up after a rejected answer, re-check the "
    "specific suspicious step before answering again.\n\n"
    "## Final Answer Format\n"
    "When you have gathered enough evidence to answer the question, output exactly:\n"
    "<FINAL>\n"
    "Answer: [your answer here]\n"
    "</FINAL>\n\n"
    "The answer should be concise and match the question type "
    "(person name, place, number, yes/no, etc.). "
    "Do not include extra explanation inside the FINAL block."
)


def _load_system_prompt(path: str | None) -> str:
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return _DEFAULT_SYSTEM_PROMPT


# ── Shared output schema ───────────────────────────────────────────────────────

def _make_sample(
    data_source: str,
    qid: str,
    question: str,
    answer: str,
    index: int,
    system_prompt: str,
    agent_name: str,
    rl_data_source: str,
    extra: dict | None = None,
) -> dict:
    sample = {
        "data_source": rl_data_source,
        "metric_data_source": data_source,
        "agent_name":  agent_name,
        "prompt": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": question},
        ],
        "ability": "multi_hop_qa",
        "domain": "multi_hop_qa",
        "reward_model": {
            "ground_truth": answer,
            "style": "rule",
        },
        "extra_info": {
            # Fields rl_dataset.py reads directly — must be uniform types across all sources
            "ground_truth":   answer,
            "domain":         "multi_hop_qa",
            "metric_data_source": "multi_hop_qa",
            "question_id":    qid,
            "index":          index,
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
            # Dataset-specific metadata serialized as a JSON string so all rows
            # have an identical extra_info schema → PyArrow can infer types cleanly.
            "meta_json": json.dumps(extra or {}, ensure_ascii=False),
        },
    }
    return sample


# ── Per-dataset converters ─────────────────────────────────────────────────────

def _iter_hotpotqa(path: Path):
    """
    HotpotQA — supports both:
      • JSON array  (original release: hotpot_train_v1.1.json)
      • JSONL       (one record per line, produced by HuggingFace download)
    Fields: id/_id, question, answer, type, supporting_facts
    """
    with path.open("r", encoding="utf-8") as f:
        first_char = f.read(1)
        f.seek(0)
        if first_char == "[":
            # JSON array
            data = json.load(f)
            if isinstance(data, dict):
                data = data.get("data", data.get("examples", list(data.values())[0]))
        else:
            # JSONL
            data = []
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    for raw in data:
        qid      = raw.get("_id") or raw.get("id", "")
        question = raw.get("question", "")
        answer   = raw.get("answer", "")
        qtype    = raw.get("type", "")
        sf       = raw.get("supporting_facts", {})
        # supporting_facts may be a dict {"title": [...]} or a list of [title, sent_id]
        if isinstance(sf, dict):
            sup_titles = list(dict.fromkeys(sf.get("title", [])))
        elif isinstance(sf, list):
            sup_titles = list(dict.fromkeys(
                item[0] for item in sf if isinstance(item, (list, tuple)) and item
            ))
        else:
            sup_titles = []
        if not question or not answer:
            continue
        yield question, answer, qid, {
            "question_type":    qtype,
            "supporting_titles": sup_titles,
        }


def _iter_2wiki(path: Path):
    """
    2WikiMultiHopQA JSONL.
    Fields: _id, question, answer, type, supporting_facts [[title, sent_id], ...]
    """
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            qid      = raw.get("_id") or raw.get("id", "")
            question = raw.get("question", "")
            answer   = raw.get("answer", "")
            qtype    = raw.get("type", "")
            sf       = raw.get("supporting_facts", [])
            # supporting_facts is a list of [title, sent_id] pairs
            sup_titles = list(dict.fromkeys(
                item[0] for item in sf if isinstance(item, (list, tuple)) and item
            ))
            if not question or not answer:
                continue
            yield question, answer, qid, {
                "question_type":    qtype,
                "supporting_titles": sup_titles,
            }


def _iter_musique(path: Path):
    """
    MuSiQue JSONL.
    Fields: id, question, answer, answer_aliases, answerable, question_decomposition
    """
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not raw.get("answerable", True):
                continue
            qid      = raw.get("id", "")
            question = raw.get("question", "")
            answer   = raw.get("answer", "")
            aliases  = raw.get("answer_aliases", [])
            if not question or not answer:
                continue
            yield question, answer, qid, {
                "answer_aliases": aliases,
            }


def _iter_bamboogle(path: Path):
    """
    Bamboogle — tiny benchmark (~125 questions).
    Supports JSON (list) or JSONL.
    Fields: Question / question, Answer / answer
    """
    with path.open("r", encoding="utf-8") as f:
        raw_text = f.read().strip()

    # Try as JSON array first
    try:
        data = json.loads(raw_text)
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = list(data.values()) if all(
                isinstance(v, dict) for v in data.values()
            ) else [data]
        else:
            rows = []
    except json.JSONDecodeError:
        rows = []
        for line in raw_text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    for i, raw in enumerate(rows):
        question = raw.get("Question") or raw.get("question", "")
        answer   = raw.get("Answer")   or raw.get("answer", "")
        qid      = raw.get("id", f"bamboogle_{i}")
        if not question or not answer:
            continue
        yield question, answer, str(qid), {}


# ── Load helpers ───────────────────────────────────────────────────────────────

_ITERS = {
    "hotpotqa":  _iter_hotpotqa,
    "2wiki":     _iter_2wiki,
    "musique":   _iter_musique,
    "bamboogle": _iter_bamboogle,
}


def load_source(
    name: str,
    path: Path,
    max_samples: int | None,
    system_prompt: str,
    agent_name: str,
    rl_data_source: str,
    exclude_qids: set[str] | None = None,
) -> list[dict]:
    """Load one source file and return a list of RL-format dicts."""
    iterate = _ITERS[name]
    rows = []
    skipped = 0
    for question, answer, qid, extra in iterate(path):
        if exclude_qids and qid in exclude_qids:
            skipped += 1
            continue
        if max_samples and len(rows) >= max_samples:
            break
        rows.append(_make_sample(
            data_source=name,
            qid=qid,
            question=question,
            answer=answer,
            index=len(rows),
            system_prompt=system_prompt,
            agent_name=agent_name,
            rl_data_source=rl_data_source,
            extra=extra,
        ))
    msg = f"  [{name}] loaded {len(rows)} samples from {path}"
    if skipped:
        msg += f"  (skipped {skipped} overlapping with test)"
    print(msg, flush=True)
    return rows


# ── I/O helpers ────────────────────────────────────────────────────────────────

def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_parquet(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def write_dataset(path_stem: Path, rows: list[dict], label: str) -> None:
    write_jsonl(path_stem.with_suffix(".jsonl"), rows)
    write_parquet(path_stem.with_suffix(".parquet"), rows)
    print(f"  {label}: {len(rows)} samples  →  {path_stem}.*")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Build mixed multi-hop RL datasets (train/val mix + individual tests)"
    )

    # Train sources (all optional so you can use a subset)
    p.add_argument("--hotpot_train",  default=None, help="HotpotQA train JSONL/JSON")
    p.add_argument("--wiki2_train",   default=None, help="2WikiMultiHopQA train JSONL")
    p.add_argument("--musique_train", default=None, help="MuSiQue train JSONL")
    # Note: Bamboogle is test-only (125 questions); it is never added to train.

    # Test sources (kept individually, no overlap with train)
    p.add_argument("--hotpot_test",   default=None, help="HotpotQA dev/test JSONL/JSON")
    p.add_argument("--wiki2_test",    default=None, help="2WikiMultiHopQA dev JSONL")
    p.add_argument("--musique_test",  default=None, help="MuSiQue dev JSONL")
    p.add_argument("--bamboogle_test", default=None, help="Bamboogle JSONL (test-only benchmark)")

    p.add_argument("--output_dir",    required=True, help="Directory for all output files")
    p.add_argument("--val_ratio",     type=float, default=0.02,
                   help="Fraction of train mix held out as val (default 0.02)")
    p.add_argument("--max_per_source", type=int, default=None,
                   help="Max samples per source for the train mix (default: all)")
    p.add_argument("--max_test_per_source", type=int, default=None,
                   help="Max samples per source for each test set (default: all)")
    p.add_argument("--seed",          type=int, default=42)
    p.add_argument("--system_prompt_path", default=None,
                   help="Path to a .md/.txt file whose content is used as the system prompt. "
                        "Defaults to the built-in reflect prompt if not provided.")
    p.add_argument("--agent_name", default="wiki_user_sim_reflect_agent",
                   help="Agent loop name written into each sample.")
    p.add_argument("--data_source_name", default="wiki_user_sim_reflect",
                   help="RL reward data_source written into each sample.")

    args = p.parse_args()

    system_prompt = _load_system_prompt(args.system_prompt_path)
    print(f"System prompt source: {'file: ' + args.system_prompt_path if args.system_prompt_path else 'built-in default'}")
    print(f"System prompt length: {len(system_prompt)} chars\n")

    random.seed(args.seed)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── Load test sets FIRST so we can exclude their IDs from train ────────────
    test_sources = [
        ("hotpotqa",  args.hotpot_test,    "test_hotpotqa"),
        ("2wiki",     args.wiki2_test,     "test_2wiki"),
        ("musique",   args.musique_test,   "test_musique"),
        ("bamboogle", args.bamboogle_test, "test_bamboogle"),
    ]

    print("\n=== Loading test sets ===")
    # Map source name → set of qids present in test, for overlap removal
    test_qids: dict[str, set[str]] = {}
    test_rows_by_source: list[tuple[str, list[dict], str]] = []
    for name, path_str, stem in test_sources:
        if path_str is None:
            print(f"  [{name}] test skipped (not provided)")
            continue
        rows = load_source(
            name,
            Path(path_str),
            max_samples=args.max_test_per_source,
            system_prompt=system_prompt,
            agent_name=args.agent_name,
            rl_data_source=args.data_source_name,
        )
        for i, s in enumerate(rows):
            s["extra_info"]["index"] = i
        test_qids[name] = {s["extra_info"]["question_id"] for s in rows}
        test_rows_by_source.append((stem, rows, name))

    # ── Build train mix (excluding any question IDs seen in test) ──────────────
    train_sources = [
        ("hotpotqa", args.hotpot_train),
        ("2wiki",    args.wiki2_train),
        ("musique",  args.musique_train),
        # Bamboogle intentionally excluded — test-only benchmark, no train split
    ]

    print("\n=== Loading train sources ===")
    all_train: list[dict] = []
    for name, path_str in train_sources:
        if path_str is None:
            print(f"  [{name}] skipped (not provided)")
            continue
        rows = load_source(
            name, Path(path_str), args.max_per_source,
            system_prompt=system_prompt,
            agent_name=args.agent_name,
            rl_data_source=args.data_source_name,
            exclude_qids=test_qids.get(name),
        )
        all_train.extend(rows)

    # Re-assign global index after merge
    random.shuffle(all_train)
    for i, s in enumerate(all_train):
        s["extra_info"]["index"] = i

    # Val split
    val_size   = max(1, int(len(all_train) * args.val_ratio))
    val_rows   = all_train[:val_size]
    train_rows = all_train[val_size:]

    print(f"\n=== Writing train/val mix ===")
    write_dataset(out / "train_mix", train_rows, "train_mix")
    write_dataset(out / "val_mix",   val_rows,   "val_mix  ")

    # ── Write individual test sets ─────────────────────────────────────────────
    print("\n=== Writing individual test sets ===")
    for stem, rows, _name in test_rows_by_source:
        write_dataset(out / stem, rows, stem)

    print("\nDone.")


if __name__ == "__main__":
    main()
