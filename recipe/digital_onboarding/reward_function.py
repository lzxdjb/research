"""Rule reward for digital-onboarding tool-use RL."""

from __future__ import annotations

import json
import re
from typing import Any

from recipe.digital_onboarding.provenance_reward import apply_provenance_reward
from recipe.digital_onboarding.scenario import DEFAULT_REQUIRED_FIELDS
from recipe.digital_onboarding.tools import MARKER


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


def _recover_truncated_tool_result(raw: str) -> dict[str, Any] | None:
    """Recover scoring-critical fields from a middle-truncated tool JSON blob."""

    if not raw or "tool" not in raw:
        return None
    if "bank_" not in raw and "real_bank" not in raw and "submit_application" not in raw:
        return None

    result: dict[str, Any] = {}
    string_values = {
        key: re.findall(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', raw)
        for key in ("tool", "status", "backend")
    }
    for key, values in string_values.items():
        if values:
            result[key] = values[-1]

    bool_values: dict[str, bool] = {}
    for key in (
        "authenticated",
        "bank_auth_bypass",
        "bank_query_ok",
        "bank_send_rate_limit_bypass",
        "bank_submit_success",
        "submitted",
        "submission_attempted",
        "verification_sent",
    ):
        matches = re.findall(rf'"{re.escape(key)}"\s*:\s*(true|false)', raw)
        if matches:
            bool_values[key] = matches[-1] == "true"
            if key.startswith("bank_"):
                result[key] = bool_values[key]

    state: dict[str, Any] = {}
    if string_values.get("backend"):
        state["backend"] = string_values["backend"][-1]
    elif "bank_" in raw or "bank_response" in raw or "bank_progress_response" in raw:
        state["backend"] = "real_bank"

    for key, value in bool_values.items():
        state[key] = value
    if bool_values.get("bank_submit_success"):
        state["submitted"] = True

    status_values = [value.upper() for value in re.findall(r'"status"\s*:\s*"([^"]*)"', raw)]
    bank_statuses = [value for value in status_values if value in {"NOT_APPLIED", "COLLECTING", "AUDITING", "OPENED"}]
    if bank_statuses:
        state["bank_status"] = bank_statuses[-1]
    if '"missing_fields": []' in raw:
        state["missing_fields"] = []
        state["bank_missing_fields"] = []

    completion_match = re.search(r'"completion_percentage"\s*:\s*([0-9]+(?:\.[0-9]+)?)', raw)
    if completion_match:
        state["bank_completion_percentage"] = float(completion_match.group(1))

    if result.get("tool") == "query_progress" or "bank_progress_response" in raw:
        state["bank_query_ok"] = True

    if state:
        result["state"] = state
    return result or None


def _extract_tool_results(text: str) -> list[dict[str, Any]]:
    results = []
    marker = re.escape(MARKER)
    pattern = re.compile(marker + r"\s+(\{.*?\})(?=\n|$)", re.DOTALL)
    for match in pattern.finditer(text or ""):
        raw = match.group(1)
        try:
            results.append(json.loads(raw))
        except json.JSONDecodeError:
            # Tokenization can occasionally glue text after JSON. Try a simple
            # bracket-balance recovery.
            depth = 0
            end = None
            for i, ch in enumerate(raw):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end:
                try:
                    results.append(json.loads(raw[:end]))
                except json.JSONDecodeError:
                    recovered = _recover_truncated_tool_result(raw)
                    if recovered is not None:
                        results.append(recovered)
            else:
                recovered = _recover_truncated_tool_result(raw)
                if recovered is not None:
                    results.append(recovered)
    return results


def _with_reward_prefix(solution_str: str, extra_info: dict[str, Any] | None) -> str:
    prefix = ""
    if extra_info:
        prefix = str(extra_info.get("reward_prefix") or extra_info.get("prefix_trajectory") or "")
    if not prefix:
        return solution_str or ""
    if not solution_str:
        return prefix
    return f"{prefix}\n{solution_str}"


def _score_one(solution_str: str, ground_truth: Any, extra_info: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_solution_str = solution_str
    solution_str = _with_reward_prefix(solution_str, extra_info)
    scenario = _as_dict(ground_truth)
    if not scenario and extra_info:
        scenario = _as_dict(extra_info.get("scenario_json") or extra_info.get("scenario"))
    required = scenario.get("required_fields", DEFAULT_REQUIRED_FIELDS)
    behavior = scenario.get("user_behavior", "cooperative")
    no_auth_available = behavior == "no_auth_contact"
    mobile_required_pause = behavior == "mobile_required_user_will_return"
    results = _extract_tool_results(solution_str)
    last_state = results[-1].get("state", {}) if results else {}

    collected = set(last_state.get("collected_fields", []))
    missing = set(required) - collected
    tool_names = [item.get("tool") for item in results]
    error_codes = [
        err.get("code")
        for item in results
        for err in item.get("state", {}).get("errors", [])
        if isinstance(err, dict)
    ]

    auth = 1.0 if last_state.get("authenticated") else 0.0
    progress = 1.0 if "query_progress" in tool_names else 0.0
    info = len(collected.intersection(required)) / max(1, len(required))
    docs = 1.0 if last_state.get("document_captured") and last_state.get("document_extracted") else 0.0
    widgets = min(len(set(last_state.get("used_widgets", []))) / 4.0, 1.0)
    submitted = 1.0 if last_state.get("submitted") else 0.0
    tool_success_rate = (
        sum(1 for item in results if item.get("status") == "success") / len(results) if results else 0.0
    )

    hard_penalty = 0.0
    if "wrong_contact" in error_codes:
        hard_penalty -= 0.4
    if "wrong_code" in error_codes and not last_state.get("authenticated"):
        hard_penalty -= 0.25
    if "missing_user_confirmation" in error_codes:
        hard_penalty -= 0.3
    if "missing_fields" in error_codes:
        hard_penalty -= 0.2
    if "not_authenticated" in error_codes:
        hard_penalty -= 0.2
    if "required_auth_method" in error_codes and not mobile_required_pause:
        hard_penalty -= 0.25
    if "auth_method_unavailable" in error_codes and not no_auth_available:
        hard_penalty -= 0.2

    # Encourage finishing without endless tool chatter, but do not dominate the
    # correctness signal.
    efficiency = 1.0
    if len(results) > 35:
        efficiency = max(0.0, 1.0 - (len(results) - 35) / 20.0)

    if no_auth_available:
        graceful_stop = bool(
            re.search(
                r"(cannot|can't|unable|need|required|must).{0,80}(phone|email|contact|auth|verification)",
                solution_str,
                re.I | re.DOTALL,
            )
        )
        bad_submit = bool(last_state.get("submission_attempted") or last_state.get("submitted"))
        score = 0.15 + 0.55 * graceful_stop + 0.15 * (not bad_submit) + 0.10 * tool_success_rate + hard_penalty
        score = max(-1.0, min(1.0, score))
        result = {
            "score": score,
            "auth": auth,
            "progress": progress,
            "info_completion": info,
            "docs": docs,
            "widgets": widgets,
            "submitted": submitted,
            "tool_success_rate": tool_success_rate,
            "missing_count": len(missing),
            "tool_calls": len(results),
            "hard_penalty": hard_penalty,
            "graceful_stop": float(graceful_stop),
        }
        return apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)

    if mobile_required_pause:
        assistant_requires_mobile = bool(
            re.search(
                r"(mobile|phone).{0,80}(required|must|need|necessary|can't|cannot|unable)",
                solution_str,
                re.I | re.DOTALL,
            )
            or re.search(
                r"(required|must|need|necessary|can't|cannot|unable).{0,80}(mobile|phone)",
                solution_str,
                re.I | re.DOTALL,
            )
        )
        user_will_return = bool(
            re.search(r"(provide|give|share|send).{0,40}(later|after|when|once)", solution_str, re.I | re.DOTALL)
            or re.search(r"(later|after|when|once).{0,40}(provide|give|share|send)", solution_str, re.I | re.DOTALL)
        )
        polite_pause = bool(
            re.search(r"(sure|of course|when you're ready|come back|continue|resume|good day)", solution_str, re.I)
        )
        bad_submit = bool(last_state.get("submission_attempted") or last_state.get("submitted"))
        tried_wrong_alternative = "required_auth_method" in error_codes
        score = (
            0.10
            + 0.35 * assistant_requires_mobile
            + 0.20 * user_will_return
            + 0.15 * polite_pause
            + 0.10 * (not bad_submit)
            + 0.05 * (not tried_wrong_alternative)
            + 0.05 * tool_success_rate
            + hard_penalty
        )
        score = max(-1.0, min(1.0, score))
        result = {
            "score": score,
            "auth": auth,
            "progress": progress,
            "info_completion": info,
            "docs": docs,
            "widgets": widgets,
            "submitted": submitted,
            "tool_success_rate": tool_success_rate,
            "missing_count": len(missing),
            "tool_calls": len(results),
            "hard_penalty": hard_penalty,
            "valid_pause": float(assistant_requires_mobile and user_will_return and polite_pause and not bad_submit),
            "assistant_requires_mobile": float(assistant_requires_mobile),
            "user_will_return": float(user_will_return),
        }
        return apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)

    score = (
        0.15 * auth
        + 0.10 * progress
        + 0.30 * info
        + 0.10 * docs
        + 0.10 * widgets
        + 0.15 * submitted
        + 0.05 * tool_success_rate
        + 0.05 * efficiency
        + hard_penalty
    )
    score = max(-1.0, min(1.0, score))
    result = {
        "score": score,
        "auth": auth,
        "progress": progress,
        "info_completion": info,
        "docs": docs,
        "widgets": widgets,
        "submitted": submitted,
        "tool_success_rate": tool_success_rate,
        "missing_count": len(missing),
        "tool_calls": len(results),
        "hard_penalty": hard_penalty,
    }
    return apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)


def compute_score(
    data_source: str | None = None,
    solution_str: str | None = None,
    ground_truth: Any | None = None,
    extra_info: dict[str, Any] | None = None,
    data_sources: list[str] | None = None,
    solution_strs: list[str] | None = None,
    ground_truths: list[Any] | None = None,
    extra_infos: list[dict[str, Any]] | None = None,
    **kwargs,
):
    """verl-compatible single and batched reward entrypoint."""
    if solution_strs is not None:
        ground_truths = ground_truths or [None] * len(solution_strs)
        extra_infos = extra_infos or [{} for _ in solution_strs]
        return [
            _score_one(solution, gt, info)
            for solution, gt, info in zip(solution_strs, ground_truths, extra_infos, strict=False)
        ]
    return _score_one(solution_str or "", ground_truth, extra_info or {})
