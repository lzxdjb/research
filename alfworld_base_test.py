import json
import numpy as np
from alfworld.agents.environment import get_environment
import alfworld.agents.modules.generic as generic
import os
import sys
os.environ["ALFWORLD_DATA"] = os.path.expanduser("/cpfs01/nlp/leizhengxing/stock-rl/data/alfworld_env_data/alfworld")
sys.argv = ["alfworld_base_test", os.path.abspath("alfworld/configs/base_config.yaml")]

config = generic.load_config()
env = get_environment("AlfredTWEnv")(config, train_eval='train')
env = env.init_env(batch_size=1)
os.environ["ALFWORLD_DATA"] = os.path.expanduser("/cpfs01/nlp/leizhengxing/stock-rl/data/alfworld_env_data/alfworld")
dataset = []

for episode in range(100):
    obs, info = env.reset()
    
    episode_data = []
    task = info.get("extra.gamefile", ["unknown"])[0]

    while True:
        admissible = list(info['admissible_commands'][0])
        action = np.random.choice(admissible)

        next_obs, scores, dones, infos = env.step([action])
        print("action:", action)
        print("next_obs:", next_obs)
        print("scores: ", scores)
        print("dones: ", dones)
        print("infos: ", infos)
        print("admissible_commands: ", list(infos.get("admissible_commands", [[]])[0]))


        episode_data.append({
            "observation": obs[0],
            "action": action,
            "reward": scores[0],
            "next_observation": next_obs[0],
            "done": dones[0],
            "admissible_commands": list(infos.get("admissible_commands", [[]])[0])
        })

        obs = next_obs
        info = infos

        if dones[0]:
            break

    dataset.append({
        "task": task,
        "trajectory": episode_data
    })

with open("./data/alfworld_text_dataset.json", "w") as f:
    json.dump(dataset, f, indent=2)

    