"""Reward for training the simulated user with RL.

The simulator is rewarded for being realistic and goal-consistent, not for
blocking the service model. It should answer with hidden-profile facts, express
reasonable difficulty when the scenario says so, and never leak hidden scenario
JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any


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


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _extract_response(text: str) -> str:
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return text
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return text
    if isinstance(parsed, dict):
        return str(parsed.get("response") or parsed.get("utterance") or parsed.get("content") or text)
    return str(parsed)


def _expected_for_probe(probe: str, scenario: dict[str, Any]) -> tuple[list[Any], list[str]]:
    profile = scenario.get("profile", {})
    behavior = scenario.get("user_behavior", "cooperative")
    available = set(profile.get("available_auth_methods", ["MOBILE", "EMAIL"]))
    probe_l = probe.lower()
    must_contain: list[Any] = []
    semantic_hints: list[str] = []

    if "mobile or email" in probe_l or "phone or email" in probe_l:
        if not available:
            semantic_hints += ["unavailable"]
        elif behavior == "forgot_mobile_use_email":
            semantic_hints += ["forgot", "email"]
        elif behavior == "forgot_email_use_mobile":
            semantic_hints += ["email", "mobile"]
        else:
            semantic_hints += [profile.get("contact_type", "").lower()]
    elif "mobile" in probe_l or "phone" in probe_l:
        if "MOBILE" in available:
            must_contain += [profile.get("mobile"), profile.get("area_code")]
        elif "EMAIL" in available:
            must_contain += [profile.get("email")]
            semantic_hints += ["email"]
        else:
            semantic_hints += ["unavailable"]
    elif "email" in probe_l:
        if "EMAIL" in available:
            must_contain += [profile.get("email")]
        elif "MOBILE" in available:
            must_contain += [profile.get("mobile"), profile.get("area_code")]
            semantic_hints += ["mobile"]
        else:
            semantic_hints += ["unavailable"]
    elif "verification code" in probe_l or re.search(r"\b(code|otp)\b", probe_l):
        if behavior == "wrong_code_once":
            semantic_hints += ["000000"]
        else:
            must_contain += [profile.get("verification_code")]
    elif "account type" in probe_l:
        must_contain += [profile.get("account_type")]
    elif "driver" in probe_l or "license" in probe_l or "document" in probe_l:
        semantic_hints += ["passport"] if behavior == "passport_only" else ["license"]
    elif "date of birth" in probe_l:
        must_contain += [profile.get("date_of_birth")]
    elif "employment" in probe_l:
        must_contain += [profile.get("employment_status")]
    elif "annual income" in probe_l:
        must_contain += [profile.get("annual_income_usd_min"), profile.get("annual_income_usd_max")]
    elif "objective" in probe_l or "risk tolerance" in probe_l:
        must_contain += [profile.get("investment_objective"), profile.get("risk_tolerance")]
    elif "confirm" in probe_l or "submit" in probe_l:
        semantic_hints += ["confirm"]
    return [x for x in must_contain if x not in (None, "")], [x for x in semantic_hints if x]


def _score_one(solution_str: str, ground_truth: Any, extra_info: dict[str, Any] | None = None) -> dict[str, Any]:
    gt = _as_dict(ground_truth)
    scenario = _as_dict(gt.get("scenario") or gt.get("scenario_json"))
    if not scenario and extra_info:
        scenario = _as_dict(extra_info.get("scenario_json") or extra_info.get("scenario"))
    probe = gt.get("probe") or (extra_info or {}).get("probe", "")

    response = _extract_response(solution_str)
    response_n = _norm(response)
    must_contain, semantic_hints = _expected_for_probe(probe, scenario)

    fact_hits = sum(1 for item in must_contain if _norm(item) in response_n)
    hint_hits = sum(1 for hint in semantic_hints if _norm(hint) in response_n)
    fact_score = fact_hits / max(1, len(must_contain)) if must_contain else 1.0
    hint_score = hint_hits / max(1, len(semantic_hints)) if semantic_hints else 1.0
    nonempty = bool(response.strip())
    leak = bool(re.search(r"hidden scenario|scenario json|available_auth_methods|auth_contacts", response, re.I))
    too_long = len(response.split()) > 80

    score = 0.45 * fact_score + 0.25 * hint_score + 0.20 * nonempty + 0.10 * (not too_long)
    if leak:
        score -= 0.6
    score = max(-1.0, min(1.0, score))
    return {
        "score": score,
        "fact_score": fact_score,
        "hint_score": hint_score,
        "nonempty": float(nonempty),
        "leak": float(leak),
        "too_long": float(too_long),
    }


def compute_score(
    data_source: str | None = None,
    solution_str: str | None = None,
    ground_truth: Any | None = None,
    extra_info: dict[str, Any] | None = None,
    solution_strs: list[str] | None = None,
    ground_truths: list[Any] | None = None,
    extra_infos: list[dict[str, Any]] | None = None,
    **kwargs,
):
    if solution_strs is not None:
        ground_truths = ground_truths or [None] * len(solution_strs)
        extra_infos = extra_infos or [{} for _ in solution_strs]
        return [
            _score_one(solution, gt, info)
            for solution, gt, info in zip(solution_strs, ground_truths, extra_infos, strict=False)
        ]
    return _score_one(solution_str or "", ground_truth, extra_info or {})
