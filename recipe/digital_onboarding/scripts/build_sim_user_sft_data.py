#!/usr/bin/env python3
"""Build SFT data for training a model-based simulated user.

This creates examples where the simulator sees hidden scenario JSON plus recent
chat history and emits a JSON object with ``current_answer``, ``thought``, and
``response``. It is intentionally generated from the rule simulator first; you
can later augment it with local 122B-model generations.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from recipe.digital_onboarding.interactions import RuleBasedOnboardingUserInteraction
from recipe.digital_onboarding.prompts import CUSTOMER_SIMULATOR_SYSTEM_PROMPT
from recipe.digital_onboarding.scenario import SYSTEM_PROMPT, make_scenarios
from recipe.digital_onboarding.sim_user_probes import ASSISTANT_PROBES


SIM_USER_SYSTEM = CUSTOMER_SIMULATOR_SYSTEM_PROMPT


async def _make_example(sim: RuleBasedOnboardingUserInteraction, scenario: dict, probe: str, idx: int) -> dict:
    scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
    instance_id = await sim.start_interaction(scenario_json=scenario_json)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": scenario["initial_user_utterance"]},
        {"role": "assistant", "content": probe},
    ]
    _, response, _, _ = await sim.generate_response(instance_id, messages)
    await sim.finalize_interaction(instance_id)
    prompt = (
        f"Hidden scenario JSON:\n{scenario_json}\n\n"
        f"Recent chat history:\n"
        f"User: {scenario['initial_user_utterance']}\n"
        f"Assistant: {probe}\n"
    )
    target = {
        "current_answer": probe,
        "thought": "Answer the assistant using only the hidden user profile and keep it brief.",
        "response": response,
    }
    return {
        "messages": [
            {"role": "system", "content": SIM_USER_SYSTEM},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)},
        ],
        "enable_thinking": False,
        "id": f"{scenario['scenario_id']}_{idx}",
    }


async def _build(args) -> list[dict]:
    sim = RuleBasedOnboardingUserInteraction({"name": "onboarding_user"})
    rows = []
    scenarios = make_scenarios(
        args.scenarios,
        split="sim_user",
        seed=args.seed,
        behavior_mode=args.behavior_mode,
    )
    for i, scenario in enumerate(scenarios):
        for j, probe in enumerate(ASSISTANT_PROBES):
            rows.append(await _make_example(sim, scenario, probe, j))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/digital_onboarding/sim_user_sft.jsonl")
    parser.add_argument("--scenarios", type=int, default=512)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument(
        "--behavior-mode",
        default="cooperative",
        help=(
            "User behavior distribution: cooperative, mixed, finishable, unfinishable, "
            "or a specific scenario behavior."
        ),
    )
    args = parser.parse_args()

    rows = asyncio.run(_build(args))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Wrote {len(rows)} simulated-user SFT examples to {output}")


if __name__ == "__main__":
    main()
