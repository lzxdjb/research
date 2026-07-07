from datasets import load_dataset
import json
import os
def save_hotpotqa_to_jsonl(output_path, split="train"):
    # Load dataset
    dataset = load_dataset("hotpot_qa", "fullwiki", split=split)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    i = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for example in dataset:
            # Optional: select only useful fields
            data = {
                "id": example["id"],
                "question": example["question"],
                "answer": example["answer"],
                "type": example["type"],  # bridge / comparison
                "supporting_facts": example["supporting_facts"],
                "context": example["context"]
            }
            i = i + 1
            # if i > 50:
            #     break
            
            
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    save_hotpotqa_to_jsonl("./data/hotpot/hotpotqa_train.jsonl", split="train")