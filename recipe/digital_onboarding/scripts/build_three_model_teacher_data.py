#!/usr/bin/env python3
"""Build teacher-labeled data for R_phi and C_theta.

This script uses a local 122B/OpenAI-compatible endpoint. It does not call any
external API. The outputs are JSONL files that can be used to train:

- reward model R_phi: judge service trajectories;
- customer simulator C_theta: generate realistic next customer utterances;
- customer simulator judge data: score C_theta responses for RL or distillation.

The input can be either scenario rows from build_data.py or rollout rows from
VERL/evaluation logs. If a row has no trajectory text, the script still builds
simulator examples from standard assistant probes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from recipe.digital_onboarding.local_model_client import call_chat_completion, clamp_score, extract_json_object
from recipe.digital_onboarding.prompts import TEACHER_122B_SYSTEM_PROMPT
from recipe.digital_onboarding.sim_user_probes import ASSISTANT_PROBES


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _scenario_from_row(row: dict[str, Any]) -> dict[str, Any]:
    reward_model = _as_dict(row.get("reward_model"))
    extra_info = _as_dict(row.get("extra_info"))
    candidates = [
        row.get("scenario_json"),
        row.get("scenario"),
        row.get("gts"),
        row.get("ground_truth"),
        extra_info.get("scenario_json"),
        extra_info.get("scenario"),
        reward_model.get("ground_truth"),
    ]
    for value in candidates:
        parsed = _as_dict(value)
        if parsed:
            return parsed
    return {}


def _solution_from_row(row: dict[str, Any]) -> str:
    if row.get("input") and row.get("output"):
        return f"{row.get('input')}\n{row.get('output')}"
    return str(
        row.get("solution_str")
        or row.get("trajectory")
        or row.get("output")
        or row.get("response")
        or row.get("text")
        or row.get("completion")
        or ""
    )


def _compact_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    profile = scenario.get("profile", {})
    profile_keep = [
        "available_auth_methods",
        "required_auth_methods",
        "contact_type",
        "email",
        "mobile",
        "area_code",
        "verification_code",
        "account_type",
        "date_of_birth",
        "employment_status",
        "drivers_license",
        "passport_photo",
    ]
    return {
        "goal": scenario.get("goal"),
        "scenario_id": scenario.get("scenario_id"),
        "user_behavior": scenario.get("user_behavior"),
        "initial_collected": scenario.get("initial_collected", {}),
        "required_fields": scenario.get("required_fields", []),
        "profile": {key: profile.get(key) for key in profile_keep if key in profile},
    }


def _teacher_json(
    *,
    endpoint: str,
    model: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    text = call_chat_completion(
        endpoint=endpoint,
        model=model,
        system_prompt=TEACHER_122B_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    parsed = extract_json_object(text)
    if not parsed:
        raise ValueError(f"teacher returned non-JSON text: {text[:300]}")
    return parsed


def _build_reward_label(
    *,
    row: dict[str, Any],
    scenario: dict[str, Any],
    endpoint: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any] | None:
    solution = _solution_from_row(row)
    if not solution:
        return None
    prompt = (
        "Task: JUDGE_SERVICE_TRAJECTORY\n\n"
        "Hidden scenario summary:\n"
        f"{json.dumps(_compact_scenario(scenario), ensure_ascii=False, sort_keys=True)}\n\n"
        "Full service trajectory:\n"
        f"{solution}\n\n"
        "Return the JSON score now."
    )
    label = _teacher_json(
        endpoint=endpoint,
        model=model,
        user_prompt=prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return {
        "data_source": "digital_onboarding_reward_teacher",
        "scenario_id": scenario.get("scenario_id"),
        "prompt": [
            {"role": "system", "content": TEACHER_122B_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "teacher_label": label,
        "score": clamp_score(label.get("score", 0.0)),
        "trajectory": solution,
        "scenario_summary": _compact_scenario(scenario),
        "source_row": row.get("extra_info", {}),
    }


def _build_sim_user_generation_label(
    *,
    scenario: dict[str, Any],
    probe: str,
    endpoint: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    prompt = (
        "Task: SIMULATE_USER\n\n"
        "Hidden scenario JSON:\n"
        f"{json.dumps(scenario, ensure_ascii=False, sort_keys=True)}\n\n"
        "Recent chat history:\n"
        f"User: {scenario.get('initial_user_utterance', 'I want to open an account.')}\n"
        f"Assistant: {probe}\n\n"
        "Return the JSON customer response now."
    )
    label = _teacher_json(
        endpoint=endpoint,
        model=model,
        user_prompt=prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    response = str(label.get("response", "")).strip()
    return {
        "data_source": "digital_onboarding_customer_teacher",
        "scenario_id": scenario.get("scenario_id"),
        "probe": probe,
        "prompt": [
            {"role": "system", "content": TEACHER_122B_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "teacher_label": label,
        "response": response,
        "scenario_json": json.dumps(scenario, ensure_ascii=False, sort_keys=True),
    }


def _build_sim_user_judge_label(
    *,
    scenario: dict[str, Any],
    probe: str,
    candidate: str,
    endpoint: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    prompt = (
        "Task: JUDGE_SIMULATED_USER\n\n"
        "Hidden scenario summary:\n"
        f"{json.dumps(_compact_scenario(scenario), ensure_ascii=False, sort_keys=True)}\n\n"
        "Assistant latest request:\n"
        f"{probe}\n\n"
        "Candidate customer utterance:\n"
        f"{candidate}\n\n"
        "Return the JSON score now."
    )
    label = _teacher_json(
        endpoint=endpoint,
        model=model,
        user_prompt=prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return {
        "data_source": "digital_onboarding_customer_judge_teacher",
        "scenario_id": scenario.get("scenario_id"),
        "probe": probe,
        "candidate": candidate,
        "prompt": [
            {"role": "system", "content": TEACHER_122B_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "teacher_label": label,
        "score": clamp_score(label.get("score", 0.0)),
        "scenario_summary": _compact_scenario(scenario),
    }


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Scenario JSONL or rollout JSONL.")
    parser.add_argument("--output-dir", default="data/digital_onboarding/teacher_labels")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8002/v1/chat/completions")
    parser.add_argument("--model", required=True, help="Local 122B model name/path served by the endpoint.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--judge-temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=768)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--sim-probes", type=int, default=4)
    parser.add_argument("--skip-reward", action="store_true")
    parser.add_argument("--skip-customer", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    reward_rows: list[dict[str, Any]] = []
    customer_rows: list[dict[str, Any]] = []
    customer_judge_rows: list[dict[str, Any]] = []

    for index, row in enumerate(_iter_jsonl(input_path)):
        if args.limit and index >= args.limit:
            break
        scenario = _scenario_from_row(row)
        if not scenario:
            continue

        if not args.skip_reward:
            reward_row = _build_reward_label(
                row=row,
                scenario=scenario,
                endpoint=args.endpoint,
                model=args.model,
                temperature=args.judge_temperature,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            if reward_row is not None:
                reward_rows.append(reward_row)

        if not args.skip_customer:
            for probe in ASSISTANT_PROBES[: max(0, args.sim_probes)]:
                customer_row = _build_sim_user_generation_label(
                    scenario=scenario,
                    probe=probe,
                    endpoint=args.endpoint,
                    model=args.model,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                )
                customer_rows.append(customer_row)
                customer_judge_rows.append(
                    _build_sim_user_judge_label(
                        scenario=scenario,
                        probe=probe,
                        candidate=customer_row["response"],
                        endpoint=args.endpoint,
                        model=args.model,
                        temperature=args.judge_temperature,
                        max_tokens=args.max_tokens,
                        timeout=args.timeout,
                    )
                )

    if reward_rows:
        _write_jsonl(reward_rows, output_dir / "reward_teacher.jsonl")
    if customer_rows:
        _write_jsonl(customer_rows, output_dir / "customer_teacher.jsonl")
    if customer_judge_rows:
        _write_jsonl(customer_judge_rows, output_dir / "customer_judge_teacher.jsonl")

    print(
        json.dumps(
            {
                "reward_rows": len(reward_rows),
                "customer_rows": len(customer_rows),
                "customer_judge_rows": len(customer_judge_rows),
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
