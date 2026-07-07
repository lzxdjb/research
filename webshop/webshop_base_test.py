"""
WebShop Trajectory Collector
Collects agent trajectories from the WebAgentTextEnv and saves them as JSON.
"""

import json
import random
import time
from pathlib import Path
from typing import Optional

import gym
from web_agent_site.envs import WebAgentTextEnv


# ── Config ────────────────────────────────────────────────────────────────────

NUM_EPISODES    = 1         # how many trajectories to collect
NUM_PRODUCTS    = 1000        # products loaded into the env
MAX_STEPS       = 10000          # max steps per episode before forced termination
OUTPUT_FILE     = "./data/webshop_trajectories.json"
RANDOM_SEED     = 42

# ── Simple rule-based agent (replace with your own model) ─────────────────────

def select_action(observation: str, valid_actions) -> str:
    """
    Placeholder policy: picks a random valid action.
    Replace this function with your actual agent / LLM call.
    """
    return random.choice(valid_actions)


# ── Core collection loop ───────────────────────────────────────────────────────

def collect_trajectories(
    num_episodes: int = NUM_EPISODES,
    num_products: int = NUM_PRODUCTS,
    max_steps: int    = MAX_STEPS,
    seed: Optional[int] = RANDOM_SEED,
):

    if seed is not None:
        random.seed(seed)

    env = gym.make(
        "WebAgentTextEnv-v0",
        observation_mode="text",
        num_products=num_products,
    )

    all_trajectories = []

    for episode_idx in range(num_episodes):
        print(f"\n{'='*60}")
        print(f"Episode {episode_idx + 1} / {num_episodes}")
        print(f"{'='*60}")

        # ── reset ──────────────────────────────────────────────────
        # WebAgentTextEnv (old gym API) returns only the observation string.
        # Guard against both old gym (<0.26) and new gym (>=0.26) return styles.
        reset_result = env.reset()
        if isinstance(reset_result, tuple):
            observation, info = reset_result
        else:
            observation, info = reset_result, {}
        if info is None:
            info = {}

        # The goal / instruction is embedded in the observation for WebShop.
        # Try info dict first, then fall back to parsing the observation.
        goal = ""
        if isinstance(info, dict):
            goal = info.get("goal", "")
        if not goal and isinstance(observation, str):
            # WebShop prefixes the instruction with "Instruction:" on the first line
            for line in observation.splitlines():
                if line.lower().startswith("instruction"):
                    goal = line.split(":", 1)[-1].strip()
                    break
            if not goal:
                goal = observation[:200]   # fallback: first 200 chars

        trajectory = {
            "episode":    episode_idx,
            "goal":       goal,
            "steps":      [],
            "total_reward": 0.0,
            "done":       False,
            "truncated":  False,
            "metadata":   {
                "num_products": num_products,
                "max_steps":    max_steps,
                "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        }

        # ── step loop ──────────────────────────────────────────────
        for step_idx in range(max_steps):
            # WebShop exposes actions via get_available_actions(), not via info.
            # Returns: {"has_search_bar": True, "clickables": ["Buy Now", ...]}
            available = env.get_available_actions()
            valid_actions = []
            if available.get("has_search_bar"):
                keywords = goal.split("Find me")[-1].strip() if "Find me" in goal else goal
                valid_actions.append(f"search[{keywords}]")
            valid_actions += [f"click[{a}]" for a in available.get("clickables", [])]

            if not valid_actions:
                print(f"  [step {step_idx}] No valid actions — ending episode.")
                break

            print(f"  [step {step_idx}] Available: {valid_actions[:5]}{'...' if len(valid_actions) > 5 else ''}")
            action = select_action(observation, valid_actions)

            # Handle both 4-tuple (old gym) and 5-tuple (new gym) step returns.
            step_result = env.step(action)
            if len(step_result) == 5:
                next_observation, reward, done, truncated, info = step_result
            else:
                next_observation, reward, done, info = step_result
                truncated = False
            if info is None:
                info = {}

            step_record = {
                "step":           step_idx,
                "observation":    observation,
                "valid_actions":  valid_actions,
                "action":         action,
                "reward":         reward,
                "done":           done,
                "next_observation": next_observation,
            }
            trajectory["steps"].append(step_record)
            trajectory["total_reward"] += reward

            print(
                f"  step {step_idx:>3} | action: {action[:60]!r:60s} | "
                f"reward: {reward:.3f} | done: {done}"
            )

            observation = next_observation

            if done or truncated:
                trajectory["done"]      = done
                trajectory["truncated"] = truncated
                print(f"  → Episode finished. Total reward: {trajectory['total_reward']:.4f}")
                break

        all_trajectories.append(trajectory)

    env.close()
    return all_trajectories


# ── Save ───────────────────────────────────────────────────────────────────────

def save_trajectories(trajectories, output_path: str = OUTPUT_FILE) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(trajectories, f, indent=2, ensure_ascii=False)

    total_steps   = sum(len(t["steps"]) for t in trajectories)
    total_reward  = sum(t["total_reward"] for t in trajectories)
    success_count = sum(1 for t in trajectories if t["total_reward"] > 0)

    print(f"\n{'='*60}")
    print(f"Saved {len(trajectories)} trajectories → {path.resolve()}")
    print(f"  Total steps  : {total_steps}")
    print(f"  Avg steps    : {total_steps / len(trajectories):.1f}")
    print(f"  Total reward : {total_reward:.4f}")
    print(f"  Avg reward   : {total_reward / len(trajectories):.4f}")
    print(f"  Success rate : {success_count}/{len(trajectories)}")
    print(f"{'='*60}\n")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    trajectories = collect_trajectories()
    save_trajectories(trajectories, OUTPUT_FILE)