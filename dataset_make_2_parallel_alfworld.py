import os
import json
import random
from pathlib import Path
import pandas as pd

# ---> MUST BE SET TO YOUR ALFWORLD PATH
ALFWORLD_DATA = os.path.expanduser("/cpfs01/nlp/leizhengxing/stock-rl/data/alfworld_env_data/alfworld")

SYSTEM_PROMPT = open(
    Path(__file__).parent / "system_prompt_alfworld.md"
).read().strip()
import glob
# In your dataset generator script (make_rl_dataset.py)
def generate_all_scenarios_from_disk(split="train", target=2500):
    search_dir = "train" if split == "train" else "valid_seen"
    search_path = os.path.join(ALFWORLD_DATA, "json_2.1.1", search_dir, "**", "trial_*")
    trial_dirs = glob.glob(search_path)
    
    scenarios = []
    for trial_path in trial_dirs:
        traj_file = os.path.join(trial_path, "traj_data.json")
        
        # 🚀 THE CRITICAL FIX: Only include trials where ALFWorld successfully generated a game file!
        tw_pddl_file = os.path.join(trial_path, "game.tw-pddl")
        if not os.path.exists(traj_file) or not os.path.exists(tw_pddl_file):
            print("skip!")
            continue
            
        with open(traj_file, "r") as f:
            data = json.load(f)
            
        task_type = data.get("task_type", "unknown")
        anns = data.get("turk_annotations", {}).get("anns", [])
        if not anns:
            continue
            
        goal = random.choice(anns).get("task_desc", "")
        
        scenarios.append({
            "game_file": trial_path,
            "goal": goal,
            "task_type": task_type
        })
        
    random.shuffle(scenarios)
    return scenarios[:target]

def make_sample(idx, scenario, split):
    task_type = scenario["task_type"]
    goal      = scenario["goal"]
    game_file = scenario["game_file"]

    prompt = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                # f"Your task is: {goal}\n\n"
                "You have been provided with the initial observation and available actions. "
                "Complete the task step by step."

            ),
        },
    ]

    return {
        "data_source":  "alfworld_textworld",
        "agent_name":   "alfworld_agent",
        "prompt":       prompt,
        "reward_model": {"ground_truth": task_type, "style": "rule"},
        "extra_info": {
            "ground_truth":       task_type,
            "goal":               goal,
            "task_type":          task_type,
            "index":              idx,
            "split":              split,
            "game_file":          game_file,
            "need_tools_kwargs":  True,
            "tools_kwargs": {
                "calc_alfworld_reward": {
                    "create_kwargs": {"ground_truth": task_type}
                },
                "EnvStep": {
                    "create_kwargs": {
                        "game_file": game_file,  # <-- Tell the tool which game to load
                        "goal": goal,
                        "task_type": task_type,
                    }
                },
            },
        },
        "ability": "embodied_task_completion",
    }

def write_outputs(samples, out_dir, name):
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"{name}.jsonl"
    with open(jsonl_path, "w") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"  {len(samples):>5} rows → {jsonl_path}")
    pd.DataFrame(samples).to_parquet(out_dir / f"{name}.parquet", index=False)

def main():
    random.seed(42)
    # Pull real scenarios from disk
    train_scenarios = generate_all_scenarios_from_disk("train")
    test_scenarios  = generate_all_scenarios_from_disk("valid_seen")

    # Optional: Cap dataset size if you have thousands
    print(len(train_scenarios))
    train_scenarios = train_scenarios[:2500]

    test_scenarios = train_scenarios[-50:]
    train_scenarios = train_scenarios[:-50]

    train_samples = [make_sample(i, sc, "train") for i, sc in enumerate(train_scenarios)]
    test_samples  = [make_sample(i + len(train_samples), sc, "test") for i, sc in enumerate(test_scenarios)]

    out_dir = Path(__file__).parent / "data/alfworld"
    write_outputs(train_samples, out_dir, "alfworld_train")
    write_outputs(test_samples,  out_dir, "alfworld_test")

if __name__ == "__main__":
    main()