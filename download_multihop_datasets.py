import json
from pathlib import Path
from datasets import load_dataset

OUT = Path("./data/multihop_raw")
OUT.mkdir(parents=True, exist_ok=True)

def save_jsonl(rows, path):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  saved {len(rows):>6} rows → {path}")

# ── HotpotQA ──────────────────────────────────────────────────────────────────
print("HotpotQA...")
ds = load_dataset("hotpotqa/hotpot_qa", "distractor")
save_jsonl(list(ds["train"]),      OUT / "hotpot_train.jsonl")
save_jsonl(list(ds["validation"]), OUT / "hotpot_test.jsonl")

# ── 2WikiMultiHopQA ───────────────────────────────────────────────────────────
print("2WikiMultiHopQA...")
ds = load_dataset("voidful/2WikiMultiHopQA")   # or "akariasai/2wikimultihopqa"
save_jsonl(list(ds["train"]),      OUT / "2wiki_train.jsonl")
save_jsonl(list(ds["validation"]), OUT / "2wiki_test.jsonl")

# ── MuSiQue ───────────────────────────────────────────────────────────────────
print("MuSiQue...")
ds = load_dataset("dgslibisey/MuSiQue")
save_jsonl(list(ds["train"]),      OUT / "musique_train.jsonl")
save_jsonl(list(ds["validation"]), OUT / "musique_test.jsonl")

# ── Bamboogle (only 125 questions, used as test only) ─────────────────────────
print("Bamboogle...")
ds = load_dataset("chiayewken/bamboogle")
save_jsonl(list(ds["test"]),       OUT / "bamboogle.jsonl")

print("\nAll done.")
