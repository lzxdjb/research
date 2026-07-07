#!/usr/bin/env python3
"""Judge a rule-based or local-model simulated user against hidden scenarios."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

from recipe.digital_onboarding.interactions import (
    OpenAICompatibleOnboardingUserInteraction,
    RuleBasedOnboardingUserInteraction,
)
from recipe.digital_onboarding.scenario import SYSTEM_PROMPT, make_scenarios
from recipe.digital_onboarding.sim_user_probes import ASSISTANT_PROBES


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _expected_for_probe(probe: str, scenario: dict[str, Any]) -> list[Any]:
    profile = scenario["profile"]
    behavior = scenario.get("user_behavior", "cooperative")
    probe_l = probe.lower()
    available = set(profile.get("available_auth_methods", ["MOBILE", "EMAIL"]))
    if "mobile or email" in probe_l or "phone or email" in probe_l:
        if not available:
            return ["right now"]
        return [profile.get("contact_type")]
    if "mobile" in probe_l or "phone" in probe_l:
        if "MOBILE" in available:
            return [profile.get("mobile"), profile.get("area_code")]
        if "EMAIL" in available:
            return [profile.get("email")]
        return ["right now"]
    if "email" in probe_l:
        if "EMAIL" in available:
            return [profile.get("email")]
        if "MOBILE" in available:
            return [profile.get("mobile"), profile.get("area_code")]
        return ["right now"]
    if "verification code" in probe_l:
        if behavior == "wrong_code_once":
            return ["000000"]
        return [profile.get("verification_code")]
    if "account type" in probe_l:
        return [profile.get("account_type")]
    if "driver" in probe_l or "license" in probe_l:
        if behavior == "passport_only":
            return ["passport"]
        return ["license"]
    if "date of birth" in probe_l:
        return [profile.get("date_of_birth")]
    if "employment" in probe_l:
        return [profile.get("employment_status")]
    if "annual income" in probe_l:
        return [profile.get("annual_income_usd_min"), profile.get("annual_income_usd_max")]
    if "objective" in probe_l or "risk tolerance" in probe_l:
        return [profile.get("investment_objective"), profile.get("risk_tolerance")]
    if "confirm" in probe_l or "submit" in probe_l:
        return ["confirm"]
    return []


async def _judge_one(sim, scenario: dict[str, Any], probe: str) -> dict[str, Any]:
    scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
    instance_id = await sim.start_interaction(scenario_json=scenario_json)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": scenario["initial_user_utterance"]},
        {"role": "assistant", "content": probe},
    ]
    _, response, _, metrics = await sim.generate_response(instance_id, messages)
    await sim.finalize_interaction(instance_id)

    expected = [x for x in _expected_for_probe(probe, scenario) if x not in (None, "")]
    response_n = _norm(response)
    matched = sum(1 for item in expected if _norm(item) in response_n)
    leak = bool(re.search(r"hidden scenario|scenario json|profile", response, re.I))
    return {
        "scenario_id": scenario["scenario_id"],
        "probe": probe,
        "response": response,
        "expected_count": len(expected),
        "matched_count": matched,
        "consistent": matched == len(expected),
        "nonempty": bool(response.strip()),
        "leak": leak,
        "metrics": metrics,
    }


async def _run(args) -> list[dict[str, Any]]:
    if args.kind == "local":
        sim = OpenAICompatibleOnboardingUserInteraction(
            {
                "endpoint": args.endpoint,
                "model": args.model,
                "temperature": args.temperature,
                "max_tokens": args.max_tokens,
                "fallback_to_rule": False,
            }
        )
    else:
        sim = RuleBasedOnboardingUserInteraction({"name": "onboarding_user"})

    rows = []
    for scenario in make_scenarios(args.scenarios, split="sim_eval", seed=args.seed):
        for probe in ASSISTANT_PROBES:
            rows.append(await _judge_one(sim, scenario, probe))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["rule", "local"], default="rule")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/v1/chat/completions")
    parser.add_argument("--model", default="onboarding-sim-user")
    parser.add_argument("--scenarios", type=int, default=64)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=192)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    rows = asyncio.run(_run(args))
    total = len(rows)
    summary = {
        "count": total,
        "nonempty_rate": sum(row["nonempty"] for row in rows) / total if total else 0.0,
        "consistency_rate": sum(row["consistent"] for row in rows) / total if total else 0.0,
        "leak_rate": sum(row["leak"] for row in rows) / total if total else 0.0,
    }
    print(json.dumps(summary, indent=2))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
