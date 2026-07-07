"""Optional provenance checks for digital-onboarding service rewards.

The bank/tool environment can tell us that an application reached a terminal
state, but it cannot tell us whether the service model grounded the submitted
fields in user-provided data. This module adds an opt-in reward penalty for
that provenance gap.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

MARKER = "ONBOARDING_TOOL_RESULT"
UPLOADED_IMAGE_MARKER = "[[UPLOADED_IMAGE]]"

CHAT_BLOCK_RE = re.compile(
    r"<\|im_start\|>\s*(system|user|assistant|service|tool)\s*\n(.*?)(?:<\|im_end\|>|$)",
    re.IGNORECASE | re.DOTALL,
)
ROLE_PREFIX_RE = re.compile(r"(?=(?:^|\n)(?:user|assistant|service|tool)\s*:)", re.IGNORECASE)
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.IGNORECASE | re.DOTALL)

IGNORED_FIELDS = {
    "application_source",
    "file_id",
    "min_file_id",
}
DOCUMENT_FILE_FIELDS = {
    "drivers_license",
    "drivers_licence_front",
    "drivers_licence_back",
    "drivers_license_front",
    "drivers_license_back",
    "passport_photo",
    "government_issued_id",
    "card_photo",
}
DOCUMENT_REVIEW_FIELDS = {
    "given_name",
    "gvie_name",
    "family_name",
    "date_of_birth",
    "gender",
    "home_address",
    "address",
}
FIELD_KEYWORDS = {
    "account_type": ("account", "cash", "margin"),
    "employment_status": ("employment", "employed", "retired", "student", "unemployed"),
    "funding_source": ("funding", "source", "funds", "savings", "inheritance", "pension"),
    "investment_experience": ("experience", "investment", "trading"),
    "investment_objective": ("objective", "goal", "growth", "income", "speculation"),
    "risk_tolerance": ("risk", "tolerance"),
    "time_horizon": ("time", "horizon"),
    "liquidity_needs": ("liquidity",),
    "is_control_person": ("control", "person"),
    "is_affiliated_exchangeorfinra": ("finra", "exchange", "affiliation"),
    "is_politically_exposed": ("politically", "exposed"),
    "is_trade_authorization": ("trade", "authorization"),
    "agreements_accepted": ("agreement", "accept"),
    "is_identify": ("identity", "identify"),
    "is_open_crypto": ("crypto",),
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def provenance_reward_enabled() -> bool:
    return _env_bool("DIGITAL_ONBOARDING_PROVENANCE_REWARD_ENABLED", False)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


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
    if MARKER in (content or ""):
        return "tool"
    role = (role or "").lower()
    return "service" if role == "assistant" else role


def _normalize_role_boundaries(text: str) -> str:
    text = text or ""
    text = re.sub(r"(?im)(^|\n)(user|assistant|service|tool)\s*(?=\n|$)", r"\1\2: ", text)
    text = re.sub(r"(?<!^)(?<!\n)(user|assistant|service|tool)\s*[:\n]", r"\n\1: ", text)
    text = re.sub(r"\n(user|assistant|service|tool)\s*\n", r"\n\1: ", text, flags=re.IGNORECASE)
    return text


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
            if cleaned:
                messages.append(("service", cleaned))
            continue
        role = _normalize_reward_role(match.group(1).lower(), match.group(2))
        content = match.group(2).strip()
        if role == "service":
            content = _strip_think(content)
        content = _strip_chat_artifacts(content).strip()
        if content:
            messages.append((role, content))
    return messages


def _with_reward_prefix(solution_str: str, extra_info: dict[str, Any] | None) -> str:
    prefix = ""
    if extra_info:
        prefix = str(extra_info.get("reward_prefix") or extra_info.get("prefix_trajectory") or "")
    if not prefix:
        return solution_str or ""
    if not solution_str:
        return prefix
    return f"{prefix}\n{solution_str}"


def _transcript_for_audit(solution_str: str, extra_info: dict[str, Any] | None) -> str:
    if extra_info:
        structured = str(extra_info.get("service_transcript_for_reward") or "")
        if structured:
            return structured
    return _with_reward_prefix(solution_str, extra_info)


def _parse_tool_result(text: str) -> dict[str, Any]:
    stripped = (text or "").strip()
    if not stripped.startswith(MARKER):
        return {}
    payload = stripped[len(MARKER) :].strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for raw in TOOL_CALL_RE.findall(text or ""):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            calls.append(parsed)
    return calls


def _pop_pending_call(pending_calls: list[dict[str, Any]], tool_name: str) -> dict[str, Any]:
    for i, call in enumerate(pending_calls):
        if call.get("name") == tool_name:
            return pending_calls.pop(i)
    return {}


def _canonical_field(field: str) -> str:
    aliases = {
        "gvie_name": "given_name",
        "address": "home_address",
        "drivers_license_front": "drivers_licence_front",
        "drivers_license_back": "drivers_licence_back",
        "email": "email_address",
    }
    return aliases.get(str(field or ""), str(field or ""))


def _normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _numeric_variants(value: Any) -> set[str]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return set()
    if number < 1:
        return set()
    integer = int(number)
    variants = {str(integer), f"{integer:,}"}
    if integer % 1000 == 0 and integer < 1_000_000:
        variants.add(f"{integer // 1000}k")
        variants.add(f"{integer // 1000} k")
    if integer % 1_000_000 == 0:
        variants.add(f"{integer // 1_000_000}m")
        variants.add(f"{integer // 1_000_000} m")
    return {_normalize_text(item) for item in variants}


def _range_answer_present(text: str) -> bool:
    normalized = _normalize_text(text)
    patterns = (
        "0 100k",
        "100k 200k",
        "200k 500k",
        "500k 1m",
        "1m 5m",
        "greater than 5m",
        "more than 5m",
    )
    return any(pattern in normalized for pattern in patterns)


def _value_in_text(value: Any, text: str) -> bool:
    normalized_text = _normalize_text(text)
    if value in (None, ""):
        return True
    if isinstance(value, dict):
        significant = [
            str(value.get(key) or "")
            for key in ("street_address1", "city", "state", "postal_code", "country")
            if value.get(key)
        ]
        return bool(significant) and any(_normalize_text(part) in normalized_text for part in significant if _normalize_text(part))
    if isinstance(value, bool):
        return ("yes" in normalized_text or "true" in normalized_text) if value else ("no" in normalized_text or "false" in normalized_text)

    value_text = _normalize_text(value)
    if value_text and value_text in normalized_text:
        return True
    value_digits = _digits(value)
    if len(value_digits) >= 4 and value_digits in _digits(text):
        return True
    return any(variant and variant in normalized_text for variant in _numeric_variants(value))


def _document_value_matches_extraction(field: str, value: Any, extracted: dict[str, Any]) -> bool:
    field = _canonical_field(field)
    if field == "given_name":
        expected = extracted.get("given_name") or extracted.get("gvie_name")
    elif field == "home_address":
        expected = extracted.get("home_address") or extracted.get("address")
    else:
        expected = extracted.get(field)
    if expected in (None, ""):
        return False
    return _normalize_text(expected) == _normalize_text(value) or _value_in_text(value, json.dumps(expected, ensure_ascii=False))


def _bool_field_grounded(field: str, value: bool, user_text: str) -> bool:
    normalized = _normalize_text(user_text)
    keywords = FIELD_KEYWORDS.get(field, (field.replace("_", " "),))
    window_hit = any(keyword in normalized for keyword in keywords)
    if field == "agreements_accepted" and value:
        return "accept" in normalized or "agree" in normalized
    if field.startswith("is_") and not value:
        return "no to" in normalized or ("no" in normalized and window_hit)
    if field.startswith("is_") and value:
        return "yes" in normalized and window_hit
    return _value_in_text(value, user_text)


def _field_grounding_reason(
    *,
    field: str,
    value: Any,
    user_text: str,
    initial_fields: set[str],
    verified_upload_seen: bool,
    document_extracted_fields: dict[str, Any],
    document_review_user_seen: bool,
    user_text_since_document_extract: str,
) -> tuple[bool, str]:
    field = _canonical_field(field)
    if field in IGNORED_FIELDS:
        return True, "ignored_system_field"
    if field in initial_fields:
        return True, "initial_collected"
    if field in DOCUMENT_FILE_FIELDS:
        return (True, "verified_upload") if verified_upload_seen else (False, "missing_verified_upload")
    if isinstance(value, dict) and {"file_id", "min_file_id"} <= set(value):
        return (True, "verified_upload_file_ids") if verified_upload_seen else (False, "file_ids_without_verified_upload")
    if field in DOCUMENT_REVIEW_FIELDS:
        if _value_in_text(value, user_text_since_document_extract):
            return True, "document_review_correction_from_user"
        if (
            verified_upload_seen
            and document_review_user_seen
            and _document_value_matches_extraction(field, value, document_extracted_fields)
        ):
            return True, "reviewed_verified_document_extraction"
        if _value_in_text(value, user_text):
            return True, "direct_user_text"
        return False, "document_field_without_reviewed_user_signal"
    if isinstance(value, bool):
        grounded = _bool_field_grounded(field, value, user_text)
        return grounded, "direct_user_text" if grounded else "boolean_not_grounded"
    if field.endswith("_min") or field.endswith("_max"):
        if _range_answer_present(user_text) or _value_in_text(value, user_text):
            return True, "range_from_user_text"
        return False, "range_not_grounded"
    if _value_in_text(value, user_text):
        return True, "direct_user_text"
    return False, "value_not_found_in_user_text"


def _scenario_initial_fields(ground_truth: Any, extra_info: dict[str, Any] | None) -> set[str]:
    scenario = _as_dict(ground_truth)
    if extra_info:
        scenario = _as_dict(extra_info.get("scenario_json") or extra_info.get("scenario")) or scenario
    initial = scenario.get("initial_collected") or {}
    if isinstance(initial, dict):
        return {_canonical_field(key) for key in initial}
    if isinstance(initial, list):
        return {_canonical_field(key) for key in initial}
    return set()


def _payload_from_collect_result(result: dict[str, Any], pending_call: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("bank_payload")
    if isinstance(payload, dict) and payload:
        return payload
    payload = result.get("payload")
    if isinstance(payload, dict) and payload:
        return payload
    arguments = pending_call.get("arguments") if isinstance(pending_call, dict) else {}
    if isinstance(arguments, dict):
        data = arguments.get("data")
        if isinstance(data, dict):
            return data
    return {}


def _iter_payload_items(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in (payload or {}).items():
        field = _canonical_field(key)
        if field in IGNORED_FIELDS:
            continue
        items.append((field, value))
    return items


def audit_provenance(solution_str: str, ground_truth: Any, extra_info: dict[str, Any] | None) -> dict[str, Any]:
    """Return grounding diagnostics without mutating the reward score."""

    extra_info = extra_info or {}
    transcript = _transcript_for_audit(solution_str, extra_info)
    messages = _messages_from_raw_text(transcript)
    initial_fields = _scenario_initial_fields(ground_truth, extra_info)

    user_text = ""
    user_text_since_document_extract = ""
    verified_upload_seen = False
    document_extracted_fields: dict[str, Any] = {}
    document_review_user_seen = False
    pending_calls: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    submitted = False

    for role, content in messages:
        if role == "service":
            pending_calls.extend(_extract_tool_calls(content))
            continue
        if role == "user":
            user_text += "\n" + content
            if document_extracted_fields and "CAPTURE_RESULT" not in content:
                document_review_user_seen = True
                user_text_since_document_extract += "\n" + content
            if "CAPTURE_RESULT" in content and UPLOADED_IMAGE_MARKER in content:
                verified_upload_seen = True
            continue
        if role != "tool":
            continue

        result = _parse_tool_result(content)
        tool_name = str(result.get("tool") or "")
        pending_call = _pop_pending_call(pending_calls, tool_name)
        if tool_name == "extract_document_info" and result.get("status") == "success":
            extracted = result.get("extracted_fields")
            document_extracted_fields = extracted if isinstance(extracted, dict) else {}
            document_review_user_seen = False
            user_text_since_document_extract = ""
            continue
        if tool_name == "submit_application" and result.get("status") == "success":
            submitted = True
            continue
        if tool_name != "collect_information" or result.get("status") != "success":
            continue

        payload = _payload_from_collect_result(result, pending_call)
        for field, value in _iter_payload_items(payload):
            grounded, reason = _field_grounding_reason(
                field=field,
                value=value,
                user_text=user_text,
                initial_fields=initial_fields,
                verified_upload_seen=verified_upload_seen,
                document_extracted_fields=document_extracted_fields,
                document_review_user_seen=document_review_user_seen,
                user_text_since_document_extract=user_text_since_document_extract,
            )
            records.append(
                {
                    "field": field,
                    "grounded": grounded,
                    "reason": reason,
                }
            )

    total = len(records)
    grounded_count = sum(1 for record in records if record["grounded"])
    ungrounded = [record for record in records if not record["grounded"]]
    score = grounded_count / total if total else 1.0
    return {
        "provenance_enabled": True,
        "provenance_score": score,
        "provenance_total_fields": total,
        "provenance_grounded_fields": grounded_count,
        "provenance_ungrounded_fields": len(ungrounded),
        "provenance_ungrounded_field_names": ",".join(record["field"] for record in ungrounded[:20]),
        "provenance_submitted": bool(submitted),
    }


def apply_provenance_reward(
    result: dict[str, Any],
    solution_str: str,
    ground_truth: Any,
    extra_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """Optionally penalize rewards for ungrounded collected/submitted fields."""

    if not provenance_reward_enabled():
        return result

    audit = audit_provenance(solution_str, ground_truth, extra_info)
    original_score = float(result.get("score", 0.0))
    provenance_score = float(audit.get("provenance_score", 1.0))
    ungrounded = int(audit.get("provenance_ungrounded_fields", 0))
    total = int(audit.get("provenance_total_fields", 0))

    weight = max(0.0, _env_float("DIGITAL_ONBOARDING_PROVENANCE_REWARD_WEIGHT", 0.7))
    max_penalty = max(0.0, _env_float("DIGITAL_ONBOARDING_PROVENANCE_MAX_PENALTY", 0.8))
    penalty = min(max_penalty, weight * max(0.0, 1.0 - provenance_score)) if total else 0.0
    next_score = original_score - penalty

    cap = _env_float("DIGITAL_ONBOARDING_PROVENANCE_UNGROUNDED_SUBMIT_MAX_SCORE", 0.35)
    if bool(audit.get("provenance_submitted")) and ungrounded > 0:
        next_score = min(next_score, cap)

    updated = dict(result)
    updated.update(audit)
    updated["score_before_provenance"] = original_score
    updated["provenance_penalty"] = penalty
    updated["score"] = max(-1.0, min(1.0, next_score))
    updated["reward_backend"] = f"{result.get('reward_backend', 'unknown')}+provenance"
    return updated

