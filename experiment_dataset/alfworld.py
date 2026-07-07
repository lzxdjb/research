import json
from alfworld.agents.environment import AlfredTWEnv
from alfworld.agents.utils.misc import add_task_to_grammar
import os
def collect_alfworld_data(output_path, num_episodes=100):
    env = AlfredTWEnv(config={"env": {"type": "AlfredTWEnv"}})
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for ep in range(num_episodes):
            obs, info = env.reset()
            done = False
            
            episode = {
                "task": info.get("task_desc", ""),
                "steps": []
            }

            while not done:
                # For dataset creation, you can use random or oracle policy
                admissible_commands = info.get("admissible_commands", [])
                
                if not admissible_commands:
                    break
                
                action = admissible_commands[0]  # placeholder policy
                
                next_obs, reward, done, info = env.step(action)

                step = {
                    "observation": obs,
                    "action": action,
                    "next_observation": next_obs,
                    "reward": reward,
                    "done": done
                }

                episode["steps"].append(step)
                obs = next_obs

            f.write(json.dumps(episode, ensure_ascii=False) + "\n")

    env.close()


if __name__ == "__main__":
    collect_alfworld_data("./data/alfworld/alfworld.jsonl", num_episodes=100)