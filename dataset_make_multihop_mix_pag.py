"""
Build Hotpot/2Wiki/MuSiQue/Bamboogle RL data for the PAG agent loop.

This is intentionally close to dataset_make_multihop_mix.py, but every row uses
agent_name="hotpot_qa_pag_agent" and a PAG prompt. The hidden ground truth is
only placed in reward/tool metadata; it is not exposed in the prompt.
"""

import argparse
import json
import random
from pathlib import Path

from dataset_make_multihop_mix import _ITERS, write_dataset


PAG_SYSTEM_PROMPT = (
    "You are a multi-hop question answering agent that uses PAG: the same model "
    "acts as both SOLVER and VERIFIER.\n\n"
    "## Available Tools\n"
    "### Search\n"
    "- Retrieve relevant passages from Wikipedia or a local knowledge base.\n"
    "- Use Search when a factual link is uncertain; do not guess unsupported facts.\n\n"
    "## Solver Role\n"
    "When solving, reason with the available evidence and finish each answer attempt "
    "with exactly:\n"
    "<ATTEMPT>\n"
    "Answer: [your concise answer]\n"
    "</ATTEMPT>\n\n"
    "## Verifier Role\n"
    "When the conversation asks you to verify an attempt, judge whether that attempt "
    "is correct using only the question, retrieved evidence, and conversation context. "
    "You are not given the ground truth. Output exactly:\n"
    "<VERIFY>\n"
    "Judgment: CORRECT or WRONG\n"
    "Reason: one brief sentence\n"
    "</VERIFY>\n\n"
    "The rollout stops only when your verifier says CORRECT or when the attempt limit "
    "is reached. Do not output a FINISHED block."
)


def _load_system_prompt(path: str | None) -> str:
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8").strip()
    return PAG_SYSTEM_PROMPT


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
    return {
        "data_source": rl_data_source,
        "metric_data_source": data_source,
        "agent_name": agent_name,
        "prompt": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "ability": "multi_hop_qa_pag",
        "reward_model": {
            "ground_truth": answer,
            "style": "rule",
        },
        "extra_info": {
            "ground_truth": answer,
            "question_id": qid,
            "index": index,
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
            "meta_json": json.dumps(extra or {}, ensure_ascii=False),
        },
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
    rows = []
    skipped = 0
    for question, answer, qid, extra in _ITERS[name](path):
        if exclude_qids and qid in exclude_qids:
            skipped += 1
            continue
        if max_samples and len(rows) >= max_samples:
            break
        rows.append(
            _make_sample(
                data_source=name,
                qid=qid,
                question=question,
                answer=answer,
                index=len(rows),
                system_prompt=system_prompt,
                agent_name=agent_name,
                rl_data_source=rl_data_source,
                extra=extra,
            )
        )
    msg = f"  [{name}] loaded {len(rows)} samples from {path}"
    if skipped:
        msg += f"  (skipped {skipped} overlapping with test)"
    print(msg, flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Build mixed multi-hop RL datasets for PAG")
    parser.add_argument("--hotpot_train", default=None, help="HotpotQA train JSONL/JSON")
    parser.add_argument("--wiki2_train", default=None, help="2WikiMultiHopQA train JSONL")
    parser.add_argument("--musique_train", default=None, help="MuSiQue train JSONL")
    parser.add_argument("--hotpot_test", default=None, help="HotpotQA dev/test JSONL/JSON")
    parser.add_argument("--wiki2_test", default=None, help="2WikiMultiHopQA dev JSONL")
    parser.add_argument("--musique_test", default=None, help="MuSiQue dev JSONL")
    parser.add_argument("--bamboogle_test", default=None, help="Bamboogle JSONL")
    parser.add_argument("--output_dir", required=True, help="Directory for output files")
    parser.add_argument("--val_ratio", type=float, default=0.02)
    parser.add_argument("--max_per_source", type=int, default=None)
    parser.add_argument("--max_test_per_source", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--system_prompt_path", default=None)
    parser.add_argument("--agent_name", default="hotpot_qa_pag_agent")
    parser.add_argument("--rl_data_source", default="hotpot_pag")
    args = parser.parse_args()

    random.seed(args.seed)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    system_prompt = _load_system_prompt(args.system_prompt_path)

    test_sources = [
        ("hotpotqa", args.hotpot_test, "test_hotpotqa"),
        ("2wiki", args.wiki2_test, "test_2wiki"),
        ("musique", args.musique_test, "test_musique"),
        ("bamboogle", args.bamboogle_test, "test_bamboogle"),
    ]

    print("\n=== Loading test sets ===")
    test_qids: dict[str, set[str]] = {}
    test_rows_by_source: list[tuple[str, list[dict], str]] = []
    for name, path_str, stem in test_sources:
        if path_str is None:
            print(f"  [{name}] test skipped (not provided)")
            continue
        rows = load_source(
            name=name,
            path=Path(path_str),
            max_samples=args.max_test_per_source,
            system_prompt=system_prompt,
            agent_name=args.agent_name,
            rl_data_source=args.rl_data_source,
        )
        for i, sample in enumerate(rows):
            sample["extra_info"]["index"] = i
        test_qids[name] = {sample["extra_info"]["question_id"] for sample in rows}
        test_rows_by_source.append((stem, rows, name))

    train_sources = [
        ("hotpotqa", args.hotpot_train),
        ("2wiki", args.wiki2_train),
        ("musique", args.musique_train),
    ]

    print("\n=== Loading train sources ===")
    all_train: list[dict] = []
    for name, path_str in train_sources:
        if path_str is None:
            print(f"  [{name}] skipped (not provided)")
            continue
        rows = load_source(
            name=name,
            path=Path(path_str),
            max_samples=args.max_per_source,
            system_prompt=system_prompt,
            agent_name=args.agent_name,
            rl_data_source=args.rl_data_source,
            exclude_qids=test_qids.get(name),
        )
        all_train.extend(rows)

    random.shuffle(all_train)
    for i, sample in enumerate(all_train):
        sample["extra_info"]["index"] = i

    val_size = max(1, int(len(all_train) * args.val_ratio)) if all_train else 0
    val_rows = all_train[:val_size]
    train_rows = all_train[val_size:]

    print("\n=== Writing train/val mix ===")
    write_dataset(out / "train_mix", train_rows, "train_mix")
    write_dataset(out / "val_mix", val_rows, "val_mix  ")

    print("\n=== Writing individual test sets ===")
    for stem, rows, _name in test_rows_by_source:
        write_dataset(out / stem, rows, stem)

    print("\nDone.")


if __name__ == "__main__":
    main()
