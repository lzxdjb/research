"""Model-judged reward for training the customer simulator C_theta.

C_theta should model the customer distribution, not attack the service model.
The reward therefore measures realism, profile consistency, calibrated
difficulty, and hidden-state hygiene. A strong 122B local model can be used as
the first judge. Later, point this reward at a smaller trained R_phi-style judge.
"""

from __future__ import annotations

import json
import os
from typing import Any

from recipe.digital_onboarding.local_model_client import call_chat_completion, clamp_score, extract_json_object
from recipe.digital_onboarding.prompts import TEACHER_122B_SYSTEM_PROMPT
from recipe.digital_onboarding.sim_user_reward_function import compute_score as rule_sim_user_score


DEFAULT_SIM_USER_JUDGE_PROMPT = TEACHER_122B_SYSTEM_PROMPT


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


def _compact_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    profile = scenario.get("profile", {})
    compact_profile = {
        key: profile.get(key)
        for key in [
            "branch",
            "available_auth_methods",
            "required_auth_methods",
            "contact_type",
            "email",
            "mobile",
            "area_code",
            "verification_code",
            "account_type",
            "given_name",
            "gvie_name",
            "family_name",
            "date_of_birth",
            "gender",
            "marital_status",
            "num_dependents",
            "citizenship_country",
            "birth_country",
            "permanent_resident",
            "social_security_number",
            "tax_id",
            "tax_id_country",
            "weight_form",
            "home_address",
            "employment_status",
            "funding_source",
            "annual_income_usd_min",
            "annual_income_usd_max",
            "liquid_net_worth_usd_min",
            "liquid_net_worth_usd_max",
            "total_net_worth_usd_min",
            "total_net_worth_usd_max",
            "investment_experience",
            "investment_objective",
            "time_horizon",
            "risk_tolerance",
            "liquidity_needs",
            "is_control_person",
            "is_affiliated_exchangeorfinra",
            "is_politically_exposed",
            "is_trade_authorization",
            "is_identify",
            "agreements_accepted",
            "drivers_license",
            "passport_photo",
            "address_proof",
            "card_photo",
        ]
        if key in profile
    }
    return {
        "scenario_id": scenario.get("scenario_id"),
        "user_behavior": scenario.get("user_behavior"),
        "initial_user_utterance": scenario.get("initial_user_utterance"),
        "profile": compact_profile,
    }


def _extract_candidate_response(solution_str: str) -> str:
    parsed = extract_json_object(solution_str)
    if parsed:
        return str(parsed.get("response") or parsed.get("utterance") or parsed.get("content") or solution_str).strip()
    return solution_str.strip()


def _score_one(
    solution_str: str,
    ground_truth: Any,
    extra_info: dict[str, Any] | None,
    *,
    endpoint: str | None,
    model: str | None,
    system_prompt: str,
    timeout: float,
    temperature: float,
    max_tokens: int,
    fallback_to_rule: bool,
) -> dict[str, Any]:
    gt = _as_dict(ground_truth)
    scenario = _as_dict(gt.get("scenario") or gt.get("scenario_json"))
    if not scenario and extra_info:
        scenario = _as_dict(extra_info.get("scenario") or extra_info.get("scenario_json"))
    probe = gt.get("probe") or (extra_info or {}).get("probe", "")
    candidate = _extract_candidate_response(solution_str)

    if not endpoint or not model:
        result = rule_sim_user_score(solution_str=solution_str, ground_truth=ground_truth, extra_info=extra_info)
        result["reward_backend"] = "rule_fallback_no_endpoint"
        return result

    user_prompt = (
        "Task: JUDGE_SIMULATED_USER\n\n"
        "Hidden scenario summary:\n"
        f"{json.dumps(_compact_scenario(scenario), ensure_ascii=False, sort_keys=True)}\n\n"
        "Assistant latest request:\n"
        f"{probe}\n\n"
        "Candidate customer utterance:\n"
        f"{candidate}\n\n"
        "Return the JSON score now."
    )
    try:
        text = call_chat_completion(
            endpoint=endpoint,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        judged = extract_json_object(text)
        return {
            "score": clamp_score(judged.get("score", 0.0)),
            "reward_backend": "local_model",
            "sim_realism": float(judged.get("realism", 0.0)),
            "sim_profile_consistency": float(judged.get("profile_consistency", 0.0)),
            "sim_calibrated_difficulty": float(judged.get("calibrated_difficulty", 0.0)),
            "sim_no_hidden_leak": float(judged.get("no_hidden_leak", 0.0)),
            "judge_reason": str(judged.get("reason", ""))[:512],
        }
    except Exception as exc:
        if not fallback_to_rule:
            return {"score": -1.0, "reward_backend": "judge_error", "judge_error": str(exc)[:512]}
        result = rule_sim_user_score(solution_str=solution_str, ground_truth=ground_truth, extra_info=extra_info)
        result["reward_backend"] = "rule_fallback_judge_error"
        result["judge_error"] = str(exc)[:512]
        return result


def compute_score(
    data_source: str | None = None,
    solution_str: str | None = None,
    ground_truth: Any | None = None,
    extra_info: dict[str, Any] | None = None,
    solution_strs: list[str] | None = None,
    ground_truths: list[Any] | None = None,
    extra_infos: list[dict[str, Any]] | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    system_prompt: str = DEFAULT_SIM_USER_JUDGE_PROMPT,
    timeout: float = 120.0,
    temperature: float = 0.0,
    max_tokens: int = 512,
    fallback_to_rule: bool = True,
    **kwargs,
):
    endpoint = endpoint or os.environ.get("DIGITAL_ONBOARDING_TEACHER_ENDPOINT")
    model = model or os.environ.get("DIGITAL_ONBOARDING_TEACHER_MODEL")
    if solution_strs is not None:
        ground_truths = ground_truths or [None] * len(solution_strs)
        extra_infos = extra_infos or [{} for _ in solution_strs]
        return [
            _score_one(
                solution,
                gt,
                info,
                endpoint=endpoint,
                model=model,
                system_prompt=system_prompt,
                timeout=timeout,
                temperature=temperature,
                max_tokens=max_tokens,
                fallback_to_rule=fallback_to_rule,
            )
            for solution, gt, info in zip(solution_strs, ground_truths, extra_infos, strict=False)
        ]
    return _score_one(
        solution_str or "",
        ground_truth,
        extra_info or {},
        endpoint=endpoint,
        model=model,
        system_prompt=system_prompt,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_tokens,
        fallback_to_rule=fallback_to_rule,
    )
