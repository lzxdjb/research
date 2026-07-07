"""Model-judged reward for service-model RL.

This reward calls a local OpenAI-compatible judge model, for example your 122B
teacher. It is the intended service reward for the three-model setup:

1. customer simulator model generates user turns,
2. reward model judges the full trajectory,
3. service model is optimized with GRPO.

The rule reward in ``reward_function.py`` is kept only as a proxy/debug fallback.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recipe.digital_onboarding.debug_logging import append_debug_csv
from recipe.digital_onboarding.prompts import REWARD_MODEL_SYSTEM_PROMPT
from recipe.digital_onboarding.provenance_reward import apply_provenance_reward
from recipe.digital_onboarding.real_bank import bank_rule_score_from_tool_results
from recipe.digital_onboarding.reward_function import _extract_tool_results, compute_score as rule_compute_score


DEFAULT_JUDGE_PROMPT = REWARD_MODEL_SYSTEM_PROMPT
STABLE_REWARD_KEYS: tuple[str, ...] = (
    "score",
    "reward_backend",
    "judge_safety",
    "judge_task_success",
    "judge_tool_use",
    "judge_customer_helpfulness",
    "judge_reason",
    "judge_error",
    "auth",
    "progress",
    "info_completion",
    "docs",
    "widgets",
    "submitted",
    "tool_success_rate",
    "missing_count",
    "tool_calls",
    "hard_penalty",
    "graceful_stop",
    "valid_pause",
    "assistant_requires_mobile",
    "user_will_return",
    "score_before_bank_rule",
    "bank_rule_score",
    "bank_reward_weight",
    "bank_status",
    "bank_missing_count",
    "bank_completion_percentage",
    "bank_completion_ratio",
    "bank_procedure_reward",
    "bank_final_reward",
    "bank_submission_success",
    "bank_submit_success",
    "finish_format_ok",
    "final_phrase_found",
    "format_penalty",
    "score_before_provenance",
    "provenance_enabled",
    "provenance_score",
    "provenance_total_fields",
    "provenance_grounded_fields",
    "provenance_ungrounded_fields",
    "provenance_ungrounded_field_names",
    "provenance_penalty",
    "provenance_submitted",
)
DEFAULT_REWARD_VALUES: dict[str, Any] = {
    "score": 0.0,
    "reward_backend": "",
    "judge_safety": 0.0,
    "judge_task_success": 0.0,
    "judge_tool_use": 0.0,
    "judge_customer_helpfulness": 0.0,
    "judge_reason": "",
    "judge_error": "",
    "auth": 0.0,
    "progress": 0.0,
    "info_completion": 0.0,
    "docs": 0.0,
    "widgets": 0.0,
    "submitted": 0.0,
    "tool_success_rate": 0.0,
    "missing_count": 0,
    "tool_calls": 0,
    "hard_penalty": 0.0,
    "graceful_stop": 0.0,
    "valid_pause": 0.0,
    "assistant_requires_mobile": 0.0,
    "user_will_return": 0.0,
    "score_before_bank_rule": 0.0,
    "bank_rule_score": 0.0,
    "bank_reward_weight": 0.0,
    "bank_status": "",
    "bank_missing_count": 0,
    "bank_completion_percentage": 0.0,
    "bank_completion_ratio": 0.0,
    "bank_procedure_reward": 0.0,
    "bank_final_reward": 0.0,
    "bank_submission_success": 0.0,
    "bank_submit_success": False,
    "finish_format_ok": 0.0,
    "final_phrase_found": 0.0,
    "format_penalty": 0.0,
    "score_before_provenance": 0.0,
    "provenance_enabled": False,
    "provenance_score": 1.0,
    "provenance_total_fields": 0,
    "provenance_grounded_fields": 0,
    "provenance_ungrounded_fields": 0,
    "provenance_ungrounded_field_names": "",
    "provenance_penalty": 0.0,
    "provenance_submitted": False,
}
FINISHABLE_GOAL = "authenticate_collect_required_kyc_and_submit_after_confirmation"
IMPOSSIBLE_GOALS = {
    "gracefully_stop_if_authentication_is_impossible",
    "gracefully_pause_until_required_mobile_is_available",
}
CHAT_BLOCK_RE = re.compile(
    r"<\|im_start\|>\s*(system|user|assistant|service|tool)\s*\n(.*?)(?:<\|im_end\|>|$)",
    re.IGNORECASE | re.DOTALL,
)
ROLE_PREFIX_RE = re.compile(r"(?=(?:^|\n)(?:user|assistant|service|tool)\s*:)", re.IGNORECASE)


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


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _append_jsonl(path: str | None, row: dict[str, Any]) -> None:
    if not path:
        return
    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _compact_scenario(ground_truth: Any, extra_info: dict[str, Any] | None) -> dict[str, Any]:
    scenario = _as_dict(ground_truth)
    if extra_info:
        runtime_scenario = _as_dict(extra_info.get("scenario_json") or extra_info.get("scenario"))
        if runtime_scenario.get("real_bank", {}).get("enabled"):
            scenario = runtime_scenario
    if not scenario and extra_info:
        scenario = _as_dict(extra_info.get("scenario_json") or extra_info.get("scenario"))
    profile = scenario.get("profile", {})
    return {
        "scenario_id": scenario.get("scenario_id"),
        "branch": scenario.get("branch"),
        "residency_category": scenario.get("residency_category"),
        "goal": scenario.get("goal"),
        "required_fields": scenario.get("required_fields", []),
        "initial_collected": scenario.get("initial_collected", {}),
        "user_behavior": scenario.get("user_behavior"),
        "citizenship_country": profile.get("citizenship_country"),
        "residence_country": (profile.get("home_address") or {}).get("country") if isinstance(profile.get("home_address"), dict) else None,
        "permanent_resident": profile.get("permanent_resident"),
        "available_auth_methods": profile.get("available_auth_methods"),
        "required_auth_methods": profile.get("required_auth_methods", []),
    }


def _scenario_id_from_inputs(ground_truth: Any, extra_info: dict[str, Any] | None) -> str:
    scenario = _as_dict(ground_truth)
    if extra_info:
        scenario = _as_dict(extra_info.get("scenario_json") or extra_info.get("scenario")) or scenario
    return str(scenario.get("scenario_id") or "")


def _default_reward_log_path(extra_info: dict[str, Any] | None = None) -> str:
    explicit = os.environ.get("DIGITAL_ONBOARDING_REWARD_LOG") or os.environ.get("REWARD_JUDGE_LOG")
    if explicit:
        return explicit
    debug_csv = os.environ.get("DIGITAL_ONBOARDING_DEBUG_CSV")
    if debug_csv:
        return os.fspath(Path(debug_csv).expanduser().parent / "reward_judge.jsonl")
    return ""


def _reward_debug_csv_path() -> str:
    explicit = os.environ.get("DIGITAL_ONBOARDING_REWARD_DEBUG_CSV") or os.environ.get("DIGITAL_ONBOARDING_DEBUG_CSV")
    if explicit:
        return explicit
    log_path = os.environ.get("DIGITAL_ONBOARDING_REWARD_LOG") or os.environ.get("REWARD_JUDGE_LOG")
    if log_path:
        return os.fspath(Path(log_path).expanduser().parent / "debug_trace")
    return ""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
    return parsed if isinstance(parsed, dict) else {}


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _strip_chat_artifacts(text: str) -> str:
    text = re.sub(r"<\|im_start\|>\s*", "", text or "")
    text = re.sub(r"<\|im_end\|>", "", text)
    text = re.sub(r"</?tool_response>", "", text)
    text = re.sub(r"</?response>", "", text)
    return text.strip()


def _normalize_reward_role(role: str, content: str) -> str:
    if "ONBOARDING_TOOL_RESULT" in (content or ""):
        return "tool"
    role = (role or "").lower()
    return "service" if role == "assistant" else role


def _normalize_role_boundaries(text: str) -> str:
    text = text or ""
    text = re.sub(r"(?im)(^|\n)(user|assistant|service|tool)\s*(?=\n|$)", r"\1\2: ", text)
    text = re.sub(r"(?<!^)(?<!\n)(user|assistant|service|tool)\s*[:\n]", r"\n\1: ", text)
    text = re.sub(r"\n(user|assistant|service|tool)\s*\n", r"\n\1: ", text, flags=re.IGNORECASE)
    return text


def _with_reward_prefix(solution_str: str, extra_info: dict[str, Any] | None) -> str:
    prefix = ""
    if extra_info:
        prefix = str(extra_info.get("reward_prefix") or extra_info.get("prefix_trajectory") or "")
    if not prefix:
        return solution_str or ""
    if not solution_str:
        return prefix
    return f"{prefix}\n{solution_str}"


def _reward_text_for_tools(solution_str: str, extra_info: dict[str, Any] | None) -> str:
    parts = [_with_reward_prefix(solution_str, extra_info)]
    if extra_info:
        parts.append(str(extra_info.get("service_transcript_for_reward") or ""))
    return "\n".join(part for part in parts if part)


def _bank_signal_for_reward(solution_str: str, extra_info: dict[str, Any] | None) -> dict[str, Any]:
    return bank_rule_score_from_tool_results(_extract_tool_results(_reward_text_for_tools(solution_str, extra_info)))


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _bank_completion_ratio(bank_signal: dict[str, Any]) -> float:
    try:
        completion_percentage = float(bank_signal.get("bank_completion_percentage") or 0.0)
    except (TypeError, ValueError):
        completion_percentage = 0.0
    return max(0.0, min(1.0, completion_percentage / 100.0))


def _bank_reward_components(bank_signal: dict[str, Any]) -> dict[str, float]:
    completion_ratio = _bank_completion_ratio(bank_signal)
    procedure_weight = max(0.0, _env_float("DIGITAL_ONBOARDING_PROCEDURE_REWARD_WEIGHT", 0.15))
    final_success_reward = max(0.0, _env_float("DIGITAL_ONBOARDING_FINAL_SUBMIT_REWARD", 1.0))
    submitted = bool(bank_signal.get("bank_submit_success"))
    return {
        "bank_completion_ratio": completion_ratio,
        "bank_procedure_reward": completion_ratio * procedure_weight,
        "bank_final_reward": final_success_reward if submitted else 0.0,
        "bank_submission_success": 1.0 if submitted else 0.0,
    }


def _attach_bank_metadata(result: dict[str, Any], bank_signal: dict[str, Any]) -> dict[str, Any]:
    if not bank_signal.get("available"):
        return result
    updated = dict(result)
    updated["bank_rule_score"] = float(bank_signal.get("score", 0.0))
    updated["bank_status"] = bank_signal.get("bank_status")
    updated["bank_missing_count"] = bank_signal.get("bank_missing_count")
    updated["bank_completion_percentage"] = bank_signal.get("bank_completion_percentage")
    updated["bank_submit_success"] = bank_signal.get("bank_submit_success")
    updated.update(_bank_reward_components(bank_signal))
    return updated


def _bank_only_reward_result(bank_signal: dict[str, Any]) -> dict[str, Any]:
    if not bank_signal.get("available"):
        return {
            "score": 0.0,
            "reward_backend": "bank_rule_only_missing_signal",
            "judge_reason": "Finishable scenario must be judged by bank signal, but no real-bank tool state was found.",
            "format_penalty": 0.0,
        }
    components = _bank_reward_components(bank_signal)
    if _env_bool("DIGITAL_ONBOARDING_PROCEDURE_REWARD_ENABLED", False):
        score = components["bank_procedure_reward"] + components["bank_final_reward"]
        return {
            "score": score,
            "reward_backend": "bank_rule_procedure",
            "judge_reason": (
                "Finishable account-opening scenario scored from real-bank procedure progress "
                "plus final submit success."
            ),
            "score_before_bank_rule": score,
            "bank_rule_score": score,
            "bank_reward_weight": 1.0,
            "bank_status": bank_signal.get("bank_status"),
            "bank_missing_count": bank_signal.get("bank_missing_count"),
            "bank_completion_percentage": bank_signal.get("bank_completion_percentage"),
            "bank_submit_success": bank_signal.get("bank_submit_success"),
            "format_penalty": 0.0,
            **components,
        }
    binary_only = _env_bool("DIGITAL_ONBOARDING_FINISHABLE_BINARY_REWARD", True)
    if binary_only:
        finished = bool(bank_signal.get("bank_submit_success"))
        score = 1.0 if finished else 0.0
    else:
        score = float(bank_signal.get("score", 0.0))
    return {
        "score": score,
        "reward_backend": "bank_rule_only_binary" if binary_only else "bank_rule_only_graded",
        "judge_reason": (
            "Finishable account-opening scenario scored from a binary final real-bank submit state."
            if binary_only
            else "Finishable account-opening scenario scored from graded real-bank progress and submit state."
        ),
        "score_before_bank_rule": score,
        "bank_rule_score": score,
        "bank_reward_weight": 1.0,
        "bank_status": bank_signal.get("bank_status"),
        "bank_missing_count": bank_signal.get("bank_missing_count"),
        "bank_completion_percentage": bank_signal.get("bank_completion_percentage"),
        "bank_submit_success": bank_signal.get("bank_submit_success"),
        "format_penalty": 0.0,
        **components,
    }


def _apply_bank_reward_signal(result: dict[str, Any], solution_str: str, extra_info: dict[str, Any] | None) -> dict[str, Any]:
    """Blend model/rule reward with real-bank tool-return state when present."""
    if not _env_bool("DIGITAL_ONBOARDING_BANK_REWARD_ENABLED", True):
        return _stable_reward_result(result)
    bank_signal = _bank_signal_for_reward(solution_str, extra_info)
    if not bank_signal.get("available"):
        return _stable_reward_result(result)

    original_score = float(result.get("score", 0.0))
    bank_score = float(bank_signal.get("score", 0.0))
    weight = float(os.environ.get("DIGITAL_ONBOARDING_BANK_REWARD_WEIGHT", "0.5"))
    weight = max(0.0, min(1.0, weight))
    blended = (1.0 - weight) * original_score + weight * bank_score

    if bank_signal.get("bank_submit_success"):
        min_success = float(os.environ.get("DIGITAL_ONBOARDING_BANK_SUBMIT_SUCCESS_MIN_SCORE", "0.95"))
        blended = max(blended, min_success)

    blended = max(-1.0, min(1.0, blended))
    updated = dict(result)
    updated["score_before_bank_rule"] = original_score
    updated["score"] = blended
    updated["bank_rule_score"] = bank_score
    updated["bank_reward_weight"] = weight
    updated["bank_status"] = bank_signal.get("bank_status")
    updated["bank_missing_count"] = bank_signal.get("bank_missing_count")
    updated["bank_completion_percentage"] = bank_signal.get("bank_completion_percentage")
    updated["bank_submit_success"] = bank_signal.get("bank_submit_success")
    updated.update(_bank_reward_components(bank_signal))
    updated["reward_backend"] = f"{result.get('reward_backend', 'unknown')}+bank_rule"
    return updated


def _stable_reward_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep reward_extra_info columns identical across parallel rollouts."""
    stable = {key: DEFAULT_REWARD_VALUES[key] for key in STABLE_REWARD_KEYS}
    stable.update(result)
    for key in ("judge_reason", "judge_error", "reward_backend", "bank_status"):
        stable[key] = "" if stable.get(key) is None else str(stable.get(key))
    if stable.get("bank_completion_percentage") is None:
        stable["bank_completion_percentage"] = 0.0
    if stable.get("bank_missing_count") is None:
        stable["bank_missing_count"] = 0
    for key in ("bank_completion_ratio", "bank_procedure_reward", "bank_final_reward", "bank_submission_success"):
        if stable.get(key) is None:
            stable[key] = 0.0
    return stable


def _messages_from_raw_text(raw_text: str) -> list[tuple[str, str]]:
    chat_messages: list[tuple[str, str]] = []
    for match in CHAT_BLOCK_RE.finditer(raw_text or ""):
        role = match.group(1).lower()
        if role == "system":
            continue
        content = match.group(2).strip()
        role = _normalize_reward_role(role, content)
        if role == "service":
            content = _strip_think(content)
        content = _strip_chat_artifacts(content).strip()
        if content:
            chat_messages.append((role, content))
    if chat_messages:
        return chat_messages

    text = _normalize_role_boundaries(_strip_chat_artifacts(raw_text))
    text = re.sub(r"(?is)^system\s*:?\s*.*?(?=\n(?:user|assistant|service|tool)\s*:|\Z)", "", text).strip()
    chunks = [chunk.strip() for chunk in ROLE_PREFIX_RE.split(text) if chunk.strip()]
    messages: list[tuple[str, str]] = []
    if not chunks:
        cleaned = _strip_think(text)
        return [("service", cleaned)] if cleaned else []
    for chunk in chunks:
        match = re.match(r"^(user|assistant|service|tool)\s*:?\s*(.*)$", chunk, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            cleaned = _strip_think(chunk).strip()
            if cleaned and cleaned.lower().strip(":") not in {"user", "assistant", "service", "tool"}:
                messages.append(("service", cleaned))
            continue
        role = match.group(1).lower()
        content = match.group(2).strip()
        role = _normalize_reward_role(role, content)
        if role == "service":
            content = _strip_think(content)
        content = _strip_chat_artifacts(content).strip()
        if content:
            messages.append((role, content))
    return messages


def _infer_termination_reason(transcript: str, scenario_summary: dict[str, Any]) -> str:
    goal = str(scenario_summary.get("goal") or "")
    lowered = (transcript or "").lower()
    if goal == "gracefully_pause_until_required_mobile_is_available" and re.search(
        r"(phone|mobile).{0,80}(later|return|come back|unavailable|don't have|do not have)|"
        r"(later|return|come back|unavailable).{0,80}(phone|mobile)",
        lowered,
    ):
        return "auth_deferred"
    if goal == "gracefully_stop_if_authentication_is_impossible" and re.search(
        r"(don't have|do not have|cannot access|can't access|no access).{0,80}(phone|mobile|email|contact)",
        lowered,
    ):
        return "auth_unavailable"
    return ""


def _last_sentence_window(text: str, limit: int = 100) -> str:
    pieces = re.split(r"(?<=[.!?。！？])\s+|\n+", text or "")
    pieces = [piece.strip() for piece in pieces if piece.strip()]
    if not pieces:
        return text or ""
    return "\n".join(pieces[-limit:])


def _clip_transcript_for_judge(transcript: str) -> str:
    """Keep judge requests bounded while preserving the start and latest turns."""
    max_chars = int(os.environ.get("DIGITAL_ONBOARDING_JUDGE_TRANSCRIPT_MAX_CHARS", "20000"))
    if max_chars <= 0 or len(transcript or "") <= max_chars:
        return transcript or ""
    tail = _last_sentence_window(transcript, 100)
    tail = tail[-max_chars:] if len(tail) > max_chars else tail
    marker = f"\n...[transcript truncated for reward judge; original_chars={len(transcript)}]...\n"
    if len(tail) >= max_chars - len(marker):
        tail_budget = max(0, max_chars - len(marker))
        return marker[: max(0, max_chars - tail_budget)] + tail[-tail_budget:]
    head_budget = max(0, max_chars - len(marker) - len(tail))
    return (transcript or "")[:head_budget] + marker + tail


def _service_text_from_transcript(transcript: str) -> str:
    service_parts = [content for role, content in _messages_from_raw_text(transcript) if role == "service"]
    return "\n".join(service_parts)


def _sanitize_transcript_for_judge(solution_str: str, extra_info: dict[str, Any] | None) -> str:
    structured = ""
    if extra_info:
        structured = str(extra_info.get("service_transcript_for_reward") or "")
    raw_text = structured or _with_reward_prefix(solution_str, extra_info)
    lines: list[str] = []
    for role, content in _messages_from_raw_text(raw_text):
        if role == "system":
            continue
        if role == "service":
            label = "service"
        elif role == "user":
            label = "user"
        elif role == "tool":
            label = "tool"
        else:
            label = role
        lines.append(f"{label}: {content}")
    if lines:
        return "\n".join(lines)
    fallback = _strip_think(_strip_chat_artifacts(raw_text))
    return f"service: {fallback}" if fallback else ""


def _build_judge_user_prompt(scenario_summary: dict[str, Any], transcript: str, termination_reason: str = "") -> str:
    sections = [
        "Based on the context below and the workflow/rules the service model should obey, give the service model a score.",
        "",
        "Private scenario summary for judging only:",
        json.dumps(scenario_summary, ensure_ascii=False, sort_keys=True),
        "",
        "Sanitized conversation history:",
        transcript or "(empty trajectory)",
    ]
    if termination_reason:
        sections.extend(["", f"Rollout termination reason: {termination_reason}"])
    sections.extend(
        [
            "",
            "Judge these aspects:",
            "1. Business correctness against the onboarding procedure.",
            "2. Tool order and tool argument correctness.",
            "3. Whether the service asks one question at a time.",
            "4. Whether the service waits for successful tool results before claiming success.",
            "5. Whether the service either completes the account opening or pauses correctly when completion is impossible.",
            "",
            "Return JSON only with this schema:",
            '{"score": float between -1 and 1, "reason": "short explanation", "safety": float between 0 and 1, "task_success": float between 0 and 1, "tool_use": float between 0 and 1, "customer_helpfulness": float between 0 and 1}',
        ]
    )
    return "\n".join(sections)


def _payload_to_readable_prompt(payload: dict[str, Any]) -> str:
    parts = []
    for message in payload.get("messages", []):
        role = str(message.get("role", "")).lower()
        if role == "system":
            label = "REWARD MODEL INSTRUCTIONS"
        elif role == "user":
            label = "JUDGMENT INPUT"
        else:
            label = role.upper()
        content = str(message.get("content", ""))
        parts.append(f"{label}:\n{content}")
    return "\n\n".join(parts)


def _reward_debug_payload(
    system_prompt: str,
    scenario_summary: dict[str, Any],
    transcript: str,
    termination_reason: str,
) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _build_judge_user_prompt(scenario_summary, transcript, termination_reason),
            },
        ],
    }


def _call_local_judge(
    *,
    endpoint: str,
    model: str,
    system_prompt: str,
    transcript: str,
    scenario_summary: dict[str, Any],
    termination_reason: str,
    timeout: float,
    temperature: float,
    max_tokens: int,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _build_judge_user_prompt(scenario_summary, transcript, termination_reason)},
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return _extract_json(content), content, payload


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
    raw_solution_str = solution_str
    extra_info = extra_info or {}
    scenario_id = _scenario_id_from_inputs(ground_truth, extra_info)
    log_path = _default_reward_log_path(extra_info)
    debug_csv_path = _reward_debug_csv_path()
    max_tokens = int(os.environ.get("DIGITAL_ONBOARDING_REWARD_MAX_TOKENS") or os.environ.get("REWARD_MAX_TOKENS") or max_tokens)
    scenario_summary = _compact_scenario(ground_truth, extra_info)
    scenario_id = str(scenario_summary.get("scenario_id") or scenario_id)
    judge_solution_str = _with_reward_prefix(solution_str, extra_info)
    judge_transcript_full = _sanitize_transcript_for_judge(solution_str, extra_info)
    termination_reason = str(extra_info.get("termination_reason") or "")
    if not termination_reason:
        termination_reason = _infer_termination_reason(judge_transcript_full, scenario_summary)
    bank_signal = _bank_signal_for_reward(raw_solution_str, extra_info)
    judge_transcript = _clip_transcript_for_judge(judge_transcript_full)

    goal = str(scenario_summary.get("goal") or "")
    if goal == FINISHABLE_GOAL:
        result = _bank_only_reward_result(bank_signal)
        result = apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)
        reward_debug_payload = _reward_debug_payload(system_prompt, scenario_summary, judge_transcript, termination_reason)
        _append_jsonl(
            log_path,
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "backend": result.get("reward_backend"),
                "endpoint": endpoint,
                "model": model,
                "scenario_summary": scenario_summary,
                "sanitized_transcript": judge_transcript,
                "termination_reason": termination_reason,
                "bank_signal": bank_signal,
                "result": result,
            },
        )
        append_debug_csv(
            debug_csv_path,
            {
                "scenario_id": scenario_id,
                "event_type": "REWARD_JUDGE",
                "role": "reward",
                "backend": result.get("reward_backend"),
                "endpoint": endpoint,
                "model": model,
                "content": judge_transcript,
                "prompt": _payload_to_readable_prompt(reward_debug_payload),
                "response": result,
                "raw_response": "",
                "reward_score": result.get("score"),
                "metadata": {"scenario_summary": scenario_summary, "termination_reason": termination_reason, "bank_signal": bank_signal, "result": result},
            },
        )
        return _stable_reward_result(result)

    if not endpoint or not model:
        result = rule_compute_score(solution_str=raw_solution_str, ground_truth=ground_truth, extra_info=extra_info)
        result["reward_backend"] = "rule_fallback_no_endpoint"
        result = _attach_bank_metadata(result, bank_signal)
        result = apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)
        reward_debug_payload = _reward_debug_payload(system_prompt, scenario_summary, judge_transcript, termination_reason)
        _append_jsonl(
            log_path,
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "backend": "rule_fallback_no_endpoint",
                "reason": "missing endpoint or model",
                "endpoint": endpoint,
                "model": model,
                "scenario_summary": scenario_summary,
                "sanitized_transcript": judge_transcript,
                "termination_reason": termination_reason,
                "bank_signal": bank_signal,
                "result": result,
            },
        )
        append_debug_csv(
            debug_csv_path,
            {
                "scenario_id": scenario_id,
                "event_type": "REWARD_JUDGE",
                "role": "reward",
                "backend": "rule_fallback_no_endpoint",
                "endpoint": endpoint,
                "model": model,
                "content": judge_transcript,
                "prompt": _payload_to_readable_prompt(reward_debug_payload),
                "response": result,
                "raw_response": "",
                "reward_score": result.get("score"),
                "metadata": {"reason": "missing endpoint or model", "scenario_summary": scenario_summary, "termination_reason": termination_reason, "bank_signal": bank_signal, "result": result},
            },
        )
        return _stable_reward_result(result)

    try:
        judged, raw_response, payload = _call_local_judge(
            endpoint=endpoint,
            model=model,
            system_prompt=system_prompt,
            transcript=judge_transcript,
            scenario_summary=scenario_summary,
            termination_reason=termination_reason,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        score = float(judged.get("score", 0.0))
        score = max(-1.0, min(1.0, score))
        result = {
            "score": score,
            "reward_backend": "local_model",
            "judge_safety": float(judged.get("safety", 0.0)),
            "judge_task_success": float(judged.get("task_success", 0.0)),
            "judge_tool_use": float(judged.get("tool_use", 0.0)),
            "judge_customer_helpfulness": float(judged.get("customer_helpfulness", 0.0)),
            "judge_reason": str(judged.get("reason", ""))[:512],
        }
        result = _attach_bank_metadata(result, bank_signal)
        result = apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)
        _append_jsonl(
            log_path,
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "backend": "local_model",
                "endpoint": endpoint,
                "model": model,
                "scenario_summary": scenario_summary,
                "sanitized_transcript": judge_transcript,
                "termination_reason": termination_reason,
                "bank_signal": bank_signal,
                "payload": payload,
                "raw_response": raw_response,
                "parsed_response": judged,
                "result": result,
            },
        )
        append_debug_csv(
            debug_csv_path,
            {
                "scenario_id": scenario_id,
                "event_type": "REWARD_JUDGE",
                "role": "reward",
                "backend": "local_model",
                "endpoint": endpoint,
                "model": model,
                "content": raw_response,
                "prompt": _payload_to_readable_prompt(payload),
                "response": judged,
                "raw_response": raw_response,
                "reward_score": score,
                "metadata": {
                    "scenario_summary": scenario_summary,
                    "termination_reason": termination_reason,
                    "sanitized_transcript": judge_transcript,
                    "bank_signal": bank_signal,
                    "result": result,
                },
            },
        )
        return _stable_reward_result(result)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
        if not fallback_to_rule:
            result = {
                "score": -1.0,
                "reward_backend": "judge_error",
                "judge_error": str(exc)[:512],
            }
            result = _attach_bank_metadata(result, bank_signal)
            result = apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)
            reward_debug_payload = _reward_debug_payload(system_prompt, scenario_summary, judge_transcript, termination_reason)
            _append_jsonl(
                log_path,
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "backend": "judge_error",
                    "endpoint": endpoint,
                    "model": model,
                    "scenario_summary": scenario_summary,
                    "sanitized_transcript": judge_transcript,
                    "solution_chars": len(judge_solution_str),
                    "termination_reason": termination_reason,
                    "bank_signal": bank_signal,
                    "error": str(exc),
                    "result": result,
                },
            )
            append_debug_csv(
                debug_csv_path,
                {
                    "scenario_id": scenario_id,
                    "event_type": "REWARD_JUDGE",
                    "role": "reward",
                    "backend": "judge_error",
                    "endpoint": endpoint,
                    "model": model,
                    "content": judge_transcript,
                    "prompt": _payload_to_readable_prompt(reward_debug_payload),
                    "response": result,
                    "raw_response": "",
                    "reward_score": result.get("score"),
                    "metadata": {"scenario_summary": scenario_summary, "error": str(exc), "result": result},
                },
            )
            return _stable_reward_result(result)
        result = rule_compute_score(solution_str=raw_solution_str, ground_truth=ground_truth, extra_info=extra_info)
        result["reward_backend"] = "rule_fallback_judge_error"
        result["judge_error"] = str(exc)[:512]
        result = _attach_bank_metadata(result, bank_signal)
        result = apply_provenance_reward(result, raw_solution_str, ground_truth, extra_info)
        reward_debug_payload = _reward_debug_payload(system_prompt, scenario_summary, judge_transcript, termination_reason)
        _append_jsonl(
            log_path,
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "backend": "rule_fallback_judge_error",
                "endpoint": endpoint,
                "model": model,
                "scenario_summary": scenario_summary,
                "sanitized_transcript": judge_transcript,
                "solution_chars": len(judge_solution_str),
                "termination_reason": termination_reason,
                "bank_signal": bank_signal,
                "error": str(exc),
                "result": result,
            },
        )
        append_debug_csv(
            debug_csv_path,
            {
                "scenario_id": scenario_id,
                "event_type": "REWARD_JUDGE",
                "role": "reward",
                "backend": "rule_fallback_judge_error",
                "endpoint": endpoint,
                "model": model,
                "content": judge_transcript,
                "prompt": _payload_to_readable_prompt(reward_debug_payload),
                "response": result,
                "raw_response": "",
                "reward_score": result.get("score"),
                "metadata": {"scenario_summary": scenario_summary, "error": str(exc), "result": result},
            },
        )
        return _stable_reward_result(result)


def compute_score(
    data_source: str | None = None,
    solution_str: str | None = None,
    ground_truth: Any | None = None,
    extra_info: dict[str, Any] | None = None,
    data_sources: list[str] | None = None,
    solution_strs: list[str] | None = None,
    ground_truths: list[Any] | None = None,
    extra_infos: list[dict[str, Any]] | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    system_prompt: str = DEFAULT_JUDGE_PROMPT,
    timeout: float = 120.0,
    temperature: float = 0.0,
    max_tokens: int = 512,
    fallback_to_rule: bool = True,
    **kwargs,
):
    endpoint = endpoint or os.environ.get("DIGITAL_ONBOARDING_REWARD_ENDPOINT") or os.environ.get("REWARD_ENDPOINT")
    model = model or os.environ.get("DIGITAL_ONBOARDING_REWARD_MODEL") or os.environ.get("REWARD_MODEL")
    fallback_to_rule = _env_bool("DIGITAL_ONBOARDING_REWARD_FALLBACK_TO_RULE", fallback_to_rule)
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
