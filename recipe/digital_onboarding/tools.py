"""Native verl tools for the digital onboarding environment."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from typing import Any

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse

from recipe.digital_onboarding.real_bank import (
    _bank_send_rate_limit,
    bank_progress_summary,
    bank_response_ok,
    make_trade_api,
    normalize_collect_payload,
    normalize_document_value,
    normalize_file_result,
    prepare_real_bank_scenario,
    real_bank_api_scripts_dir,
    real_bank_enabled,
    real_bank_fake_upload_wrapper_enabled,
    real_bank_fake_verification_wrapper_enabled,
    real_bank_session_root,
    real_bank_strict_production_execution_enabled,
    real_bank_upload_thumbnail_enabled,
    real_bank_verification_code,
    sanitize_bank_response,
    trajectory_id_for,
    write_tool_upload_file,
)
from recipe.digital_onboarding.scenario import DEFAULT_REQUIRED_FIELDS

MARKER = "ONBOARDING_TOOL_RESULT"
UPLOADED_IMAGE_MARKER = "[[UPLOADED_IMAGE]]"
UPLOAD_RETRY_MESSAGE = "Sorry, but it seems that no image was uploaded successfully. Could you please upload the image again?"
DOCUMENT_UPLOAD_REQUIRED_FOR_COLLECTION_MESSAGE = (
    "Phone/email verification is complete. Next, ask the user to upload a driver's license or government-issued ID "
    "before collecting document information."
)
DOCUMENT_REVIEW_REQUIRED_MESSAGE = (
    "The document image was received, but document review is not complete. Next action: call extract_document_info. "
    "After that tool succeeds, do not call another tool in the same turn; show only the readable document fields in a "
    "normal assistant message and ask the user to confirm or correct them before submit_documents."
)
DOCUMENT_REVIEW_PENDING_MESSAGE = (
    "Document review is pending. Do not call submit_documents or collect unrelated onboarding fields yet. Next action: "
    "send a normal assistant message listing the readable extracted document fields and ask the user to confirm or "
    "correct them. After the user's confirmation/correction, call submit_documents with the reviewed metadata."
)
DOCUMENT_COLLECT_SCOPE_MESSAGE = (
    "Only submit reviewed document metadata after the user responds to the document review. Ask remaining KYC questions "
    "one at a time after the document submission succeeds."
)
DOCUMENT_REVIEW_REJECTED_MESSAGE = (
    "The user indicated that the extracted document information does not belong to them. Ask them to upload the "
    "correct document image again before continuing."
)
USER_UPLOAD_IN_PROGRESS_KEY = "_digital_onboarding_user_upload_in_progress"
VERIFIED_UPLOADS_KEY = "_digital_onboarding_verified_uploads"
UPLOAD_GATE_KEY = "_digital_onboarding_upload_gate"
DOCUMENT_FIELDS = {
    "drivers_license",
    "drivers_licence_front",
    "drivers_licence_back",
    "drivers_license_front",
    "drivers_license_back",
    "passport_photo",
    "address_proof",
    "documents",
    "visa",
    "id_card",
    "permanent_resident_card",
    "card_photo",
    "government_issued_id",
}
DOCUMENT_REVIEW_COLLECT_FIELDS = {
    "drivers_license",
    "drivers_licence_front",
    "drivers_licence_back",
    "drivers_license_front",
    "drivers_license_back",
    "passport_photo",
    "address_proof",
    "documents",
    "visa",
    "id_card",
    "permanent_resident_card",
    "card_photo",
    "government_issued_id",
    "file_id",
    "min_file_id",
    "file_type",
    "filename",
    "passport_number",
    "passport_no",
    "passport_expire_date",
    "expiration_date",
    "issuing_country",
    "id_number",
    "card_number",
    "visa_type",
    "issue_date",
    "given_name",
    "gvie_name",
    "family_name",
    "date_of_birth",
    "gender",
    "home_address",
    "address",
}


def _boolish(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


_REAL_BANK_API_CACHE: dict[str, Any] = {}


def _loads_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def _last_user_text(agent_data: Any) -> str:
    for message in reversed(agent_data.messages):
        if message.get("role") == "user":
            return _message_text(message).lower()
    return ""


def _last_user_raw_text(agent_data: Any) -> str:
    for message in reversed(agent_data.messages):
        if message.get("role") == "user":
            return _message_text(message)
    return ""


def assistant_requests_document_upload(text: str) -> bool:
    cleaned = re.sub(r"<think>.*?</think>|<think>.*$", "", text or "", flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<tool_call>.*?</tool_call>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = cleaned.lower()
    if not cleaned:
        return False
    fixed_retry = (
        "no image was uploaded successfully" in cleaned
        or "didn't upload an image" in cleaned
        or "please upload the requested document image" in cleaned
    )
    if fixed_retry:
        return True

    correction_context = bool(
        re.search(
            r"\b(correct|correction|incorrect|wrong|fix|update|review|field|details?|value|information)\b",
            cleaned,
        )
    )
    explicit_upload_language = bool(
        re.search(r"\b(upload(?:ed|ing)?|attach(?:ed|ing)?|scan|capture|take\s+a\s+photo|photo|image|picture|file)\b", cleaned)
    )
    if correction_context and not explicit_upload_language:
        return False

    return bool(
        re.search(
            r"\b(upload(?:ed|ing)?|show|capture|send|attach(?:ed|ing)?|scan)\b.{0,100}\b(image|photo|picture|file|document|id|license|licence|passport|visa|green card|permanent resident card|address proof)\b",
            cleaned,
        )
        or re.search(
            r"\b(image|photo|picture|file|document|id|license|licence|passport|visa|green card|permanent resident card|address proof)\b.{0,100}\b(upload(?:ed|ing)?|show|capture|send|attach(?:ed|ing)?|scan)\b",
            cleaned,
        )
        or re.search(r"\bprovide\b.{0,80}\b(image|photo|picture|copy|scan)\b", cleaned)
        or re.search(
            r"\bprovide\b.{0,80}\b(document|id|license|licence|passport|visa|green card|address proof)\b.{0,40}\b(file|copy|scan|image|photo)\b",
            cleaned,
        )
        or re.search(r"\bprovide\b.{0,60}\b(driver'?s license|driver'?s licence|government-issued id|passport|visa|green card|permanent resident card|address proof)\b", cleaned)
        or re.search(r"\b(need|needs|required|require|requires)\b.{0,100}\b(image|photo|picture|file|document image|id image|license image|licence image|passport image)\b", cleaned)
    )


def _extra_fields(agent_data: Any) -> dict[str, Any]:
    extra_fields = getattr(agent_data, "extra_fields", {})
    return extra_fields if isinstance(extra_fields, dict) else {}


def _document_field_for_doc_type(doc_type: str) -> str:
    lowered = (doc_type or "").lower()
    if "passport" in lowered:
        return "passport_photo"
    if "utility" in lowered or "statement" in lowered or "address" in lowered or "bill" in lowered:
        return "address_proof"
    if "visa" in lowered:
        return "visa"
    if "green" in lowered or "permanent_resident" in lowered:
        return "card_photo"
    if "id_card" in lowered or lowered == "id" or "government" in lowered or "card" in lowered:
        return "card_photo"
    return "drivers_license"


def mark_document_upload_requested(agent_data: Any, doc_type: str = "drivers_license_front") -> None:
    extra_fields = _extra_fields(agent_data)
    if not extra_fields and not hasattr(agent_data, "extra_fields"):
        return
    doc_type = doc_type or "drivers_license_front"
    doc_key = _document_field_for_doc_type(doc_type)
    gate = extra_fields.setdefault(UPLOAD_GATE_KEY, {})
    if isinstance(gate, dict):
        gate["awaiting_document_upload"] = True
        gate["expected_doc_type"] = doc_type
        gate["expected_document_field"] = doc_key
    state = extra_fields.get("onboarding_state")
    if isinstance(state, dict):
        state["awaiting_document_upload"] = True
        state["expected_doc_type"] = doc_type
        state["expected_document_field"] = doc_key


def document_upload_pending(agent_data: Any) -> bool:
    extra_fields = _extra_fields(agent_data)
    gate = extra_fields.get(UPLOAD_GATE_KEY)
    if isinstance(gate, dict) and gate.get("awaiting_document_upload"):
        return True
    state = extra_fields.get("onboarding_state")
    return bool(isinstance(state, dict) and state.get("awaiting_document_upload"))


def mark_document_upload_satisfied(agent_data: Any, state: dict[str, Any] | None = None) -> None:
    extra_fields = _extra_fields(agent_data)
    gate = extra_fields.get(UPLOAD_GATE_KEY)
    if isinstance(gate, dict):
        gate["awaiting_document_upload"] = False
    if state is None:
        state = extra_fields.get("onboarding_state")
    if isinstance(state, dict):
        state["document_upload_verified"] = True
        state["awaiting_document_upload"] = False


def _capture_result_from_last_user(agent_data: Any) -> dict[str, Any]:
    text = _last_user_raw_text(agent_data)
    if "CAPTURE_RESULT" not in text:
        return {}
    if not contains_uploaded_image(text):
        return {}

    def find_value(label: str) -> str:
        match = re.search(rf"{re.escape(label)}\s*:\s*([^\n]+)", text, re.IGNORECASE)
        return match.group(1).strip() if match else ""

    doc_type = find_value("Doc Type") or find_value("Document Type") or "drivers_license_front"
    doc_field = find_value("Document Field")
    if not doc_field:
        doc_field = _document_field_for_doc_type(doc_type)
    file_id = find_value("File ID")
    min_file_id = find_value("Min File ID")
    verification_id = find_value("Verification ID") or find_value("Image Verification ID")
    if not file_id or not min_file_id:
        return {}
    if uploaded_image_required() and not _verified_upload_exists(agent_data, verification_id):
        return {}
    return {
        "doc_type": doc_type,
        "document_field": doc_field,
        "file_url": find_value("File URL"),
        "filename": find_value("File Name") or find_value("Filename"),
        "file_id": file_id,
        "min_file_id": min_file_id,
        "verification_id": verification_id,
    }


def _verification_id_from_message(user_message: Any) -> str:
    if isinstance(user_message, str):
        text = user_message
    elif isinstance(user_message, dict):
        text = _message_text(user_message)
    else:
        text = _message_text({"content": user_message})
    match = re.search(r"(?:Verification ID|Image Verification ID)\s*:\s*([^\n]+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def verified_upload_for_message(agent_data: Any, user_message: Any) -> dict[str, Any]:
    """Return verified upload metadata referenced by a CAPTURE_RESULT message."""

    if not contains_uploaded_image(user_message):
        return {}
    verification_id = _verification_id_from_message(user_message)
    if not verification_id:
        return {}
    upload = _verified_uploads(agent_data).get(verification_id, {})
    return dict(upload) if isinstance(upload, dict) else {}


def uploaded_image_user_content(text: str, image_part: dict[str, Any] | None = None) -> str | list[dict[str, Any]]:
    """Build a multimodal user message while preserving the text CAPTURE_RESULT."""

    text = text or ""
    if not image_part:
        return text
    return [{"type": "text", "text": text}, image_part]


def contains_uploaded_image(user_message: Any) -> bool:
    """Return True for messages that carry an upload proof marker or image part.

    In the browser path, /api/upload executes upload_file and then appends a
    CAPTURE_RESULT containing UPLOADED_IMAGE_MARKER. In RL simulation, the user
    interaction emits the same marker when it performs the upload action.
    Plain text like "I uploaded it" intentionally does not pass.
    """

    if isinstance(user_message, str):
        return UPLOADED_IMAGE_MARKER in user_message
    if isinstance(user_message, list):
        return any(contains_uploaded_image(part) for part in user_message)
    if isinstance(user_message, dict):
        part_type = str(user_message.get("type") or "").lower()
        if part_type in {"image", "image_url", "input_image"}:
            return True
        if any(key in user_message for key in ("image", "image_url", "input_image")):
            return True
        if UPLOADED_IMAGE_MARKER in str(user_message.get("text") or ""):
            return True
        for key in ("content", "attachments", "images"):
            if key in user_message and contains_uploaded_image(user_message[key]):
                return True
        return UPLOADED_IMAGE_MARKER in _message_text(user_message)
    return False


def contains_verified_uploaded_image(agent_data: Any, user_message: Any) -> bool:
    if isinstance(user_message, str):
        text = user_message
    elif isinstance(user_message, dict):
        text = _message_text(user_message)
    else:
        text = _message_text({"content": user_message})
    if "CAPTURE_RESULT" not in text or not contains_uploaded_image(user_message):
        return False
    verification_id = _verification_id_from_message(user_message)
    return _verified_upload_exists(agent_data, verification_id)


def uploaded_image_required() -> bool:
    return os.environ.get("DIGITAL_ONBOARDING_REQUIRE_UPLOADED_IMAGE", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _document_payload_present(data: dict[str, Any]) -> bool:
    return any(key in DOCUMENT_FIELDS for key in data)


def _document_value_has_file(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("file_id") and value.get("min_file_id"):
        return True
    return any(isinstance(value.get(side), dict) and value[side].get("file_id") for side in ("front", "back"))


def _single_file_document_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get("front"), dict):
        value = value["front"]
    return {key: value[key] for key in ("file_id", "min_file_id") if value.get(key)}


def _profile_file_for_doc_type(value: Any, doc_type: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    lowered = (doc_type or "").lower()
    if "back" in lowered and isinstance(value.get("back"), dict):
        return _single_file_document_value(value["back"])
    if "front" in lowered and isinstance(value.get("front"), dict):
        return _single_file_document_value(value["front"])
    return _single_file_document_value(value)


def _uploaded_document_value(existing: Any, file_obj: dict[str, Any], doc_type: str) -> dict[str, Any]:
    file_obj = {key: file_obj[key] for key in ("file_id", "min_file_id") if file_obj.get(key)}
    if not file_obj:
        return copy.deepcopy(existing) if isinstance(existing, dict) else {}
    lowered = (doc_type or "").lower()
    if "licence" in lowered or "license" in lowered:
        document = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        side = "back" if "back" in lowered else "front"
        side_obj = copy.deepcopy(file_obj)
        if side == "front":
            side_obj.setdefault("expire_date", os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_DOCUMENT_EXPIRE_DATE", "2030-12-31"))
        document[side] = side_obj
        return document
    return copy.deepcopy(file_obj)


def _document_review_collect_payload_present(data: dict[str, Any]) -> bool:
    return any(key in DOCUMENT_REVIEW_COLLECT_FIELDS for key in data)


def _last_user_confirms_document_review(agent_data: Any) -> bool:
    text = _last_user_text(agent_data)
    if not text:
        return False
    if re.search(r"\b(no|not|incorrect|wrong|inaccurate|change|update)\b", text):
        return False
    short = re.sub(r"[^a-z]+", " ", text).strip()
    if short in {"confirm", "confirmed", "ok", "okay", "yes", "y", "looks good"}:
        return True
    return bool(re.search(r"\b(confirm|confirmed|correct|yes|ok|okay|looks good|accurate|right)\b", text))


def _last_user_rejects_document_identity(agent_data: Any) -> bool:
    text = _last_user_text(agent_data)
    if not text:
        return False
    return bool(
        re.search(r"\b(this|that|it)\s+is\s+not\s+(my|me|mine)\b", text)
        or re.search(r"\bnot\s+my\s+(information|info|document|id|license|licence)\b", text)
        or re.search(r"\b(wrong|different)\s+(person|customer|identity|id)\b", text)
        or re.search(r"\bbelongs\s+to\s+someone\s+else\b", text)
    )


def _verified_uploads(agent_data: Any) -> dict[str, dict[str, Any]]:
    extra_fields = _extra_fields(agent_data)
    if not extra_fields and not hasattr(agent_data, "extra_fields"):
        return {}
    uploads = extra_fields.setdefault(VERIFIED_UPLOADS_KEY, {})
    return uploads if isinstance(uploads, dict) else {}


def register_verified_upload(agent_data: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    verification_id = str(metadata.get("verification_id") or "").strip()
    if not verification_id:
        raise ValueError("Verified upload metadata is missing verification_id.")
    uploads = _verified_uploads(agent_data)
    uploads[verification_id] = dict(metadata)
    extra_fields = _extra_fields(agent_data)
    if extra_fields or hasattr(agent_data, "extra_fields"):
        extra_fields[USER_UPLOAD_IN_PROGRESS_KEY] = verification_id
    return uploads[verification_id]


def clear_user_upload_in_progress(agent_data: Any) -> None:
    extra_fields = _extra_fields(agent_data)
    if extra_fields or hasattr(agent_data, "extra_fields"):
        extra_fields.pop(USER_UPLOAD_IN_PROGRESS_KEY, None)


def _verified_upload_exists(agent_data: Any, verification_id: str) -> bool:
    return bool(verification_id and verification_id in _verified_uploads(agent_data))


def _active_verified_upload(agent_data: Any) -> dict[str, Any]:
    extra_fields = _extra_fields(agent_data)
    if not extra_fields and not hasattr(agent_data, "extra_fields"):
        return {}
    verification_id = str(extra_fields.get(USER_UPLOAD_IN_PROGRESS_KEY) or "")
    if not verification_id:
        return {}
    return _verified_uploads(agent_data).get(verification_id, {})


def _upload_gate_debug(agent_data: Any) -> dict[str, Any]:
    extra_fields = _extra_fields(agent_data)
    uploads = _verified_uploads(agent_data)
    active_verification_id = str(extra_fields.get(USER_UPLOAD_IN_PROGRESS_KEY) or "")
    last_user = _last_user_raw_text(agent_data)
    last_user_verification_id = _verification_id_from_message(last_user)
    active_upload = uploads.get(active_verification_id, {}) if active_verification_id else {}
    return {
        "active_verification_id": active_verification_id,
        "active_upload_found": bool(active_upload),
        "active_upload_doc_type": active_upload.get("doc_type") if isinstance(active_upload, dict) else None,
        "document_upload_pending": document_upload_pending(agent_data),
        "last_user_has_capture_result": "CAPTURE_RESULT" in last_user,
        "last_user_has_uploaded_image_marker": contains_uploaded_image(last_user),
        "last_user_verification_id": last_user_verification_id,
        "last_user_upload_verified": _verified_upload_exists(agent_data, last_user_verification_id),
        "verified_upload_count": len(uploads),
        "verified_upload_ids_tail": list(uploads.keys())[-5:],
    }


def _apply_capture_result_from_last_user(state: dict[str, Any], agent_data: Any) -> dict[str, Any]:
    capture = _capture_result_from_last_user(agent_data)
    if not capture:
        return {}
    capture_signature = json.dumps(
        {
            "doc_type": capture.get("doc_type"),
            "document_field": capture.get("document_field"),
            "file_id": capture.get("file_id"),
            "min_file_id": capture.get("min_file_id"),
            "verification_id": capture.get("verification_id"),
        },
        sort_keys=True,
    )
    if state.get("last_capture_result_signature") == capture_signature:
        return capture
    doc_key = _document_field_for_doc_type(capture.get("doc_type") or capture.get("document_field") or "")
    if capture.get("document_field") in {
        "passport_photo",
        "drivers_license",
        "government_issued_id",
        "card_photo",
        "address_proof",
        "visa",
        "permanent_resident_card",
    }:
        doc_key = "card_photo" if capture["document_field"] == "permanent_resident_card" else capture["document_field"]
    file_obj = {"file_id": capture["file_id"], "min_file_id": capture["min_file_id"]}
    state["document_captured"] = True
    mark_document_upload_satisfied(agent_data, state)
    state["document_extracted"] = False
    state["document_review_pending"] = False
    state["document_review_confirmed"] = False
    state["document_review_presented"] = False
    state["last_capture_result_signature"] = capture_signature
    state.pop("last_extracted_document_fields", None)
    state.pop("last_presented_document_fields", None)
    captured = state.setdefault("captured_documents", {})
    captured[doc_key] = _uploaded_document_value(captured.get(doc_key), file_obj, str(capture.get("doc_type") or doc_key))
    return capture


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items() if v not in ("", None)}
    if isinstance(value, str):
        return value.strip()
    return value


def _digits_only(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_mobile_contact(contact: Any, expected_area_code: Any = "1") -> tuple[str, str]:
    digits = _digits_only(contact)
    country_code = _digits_only(expected_area_code) or "1"
    if country_code and digits.startswith(country_code) and len(digits) > 10:
        digits = digits[len(country_code) :]
    return digits, country_code


def _bank_error_code(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    return str(response.get("errorCode") or response.get("s") or response.get("i18nMsg") or "").upper()


def _bank_error_message(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    for key in ("errmsg", "errorMsg", "error_message", "message", "msg"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    nested = response.get("d")
    if isinstance(nested, dict):
        for key in ("errmsg", "errorMsg", "error_message", "message", "msg"):
            value = nested.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _bank_rejection_reason(response: Any) -> str:
    code = _bank_error_code(response)
    message = _bank_error_message(response)
    if code and message:
        return f"{code}: {message}"
    if code:
        return code
    if message:
        return message
    return "bank_rejected_or_unconfirmed"


def _retryable_send_code_response(response: Any) -> bool:
    code = _bank_error_code(response)
    return code in {"INVALID_PHONE", "INVALID PHONE"} or "INVALID_PHONE" in code


def _state_from_snapshot(snapshot: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """Normalize a serialized prefix-state snapshot into the live tool state."""

    profile = copy.deepcopy(snapshot.get("profile") or scenario.get("profile", {}))
    required_fields = copy.deepcopy(
        snapshot.get("required_fields") or scenario.get("required_fields", DEFAULT_REQUIRED_FIELDS)
    )
    collected = copy.deepcopy(snapshot.get("collected_fields", scenario.get("initial_collected", {})))
    if isinstance(collected, list):
        collected = {key: copy.deepcopy(profile.get(key, True)) for key in collected}
    if not isinstance(collected, dict):
        collected = {}

    state = {
        "scenario_id": snapshot.get("scenario_id") or scenario.get("scenario_id", "unknown"),
        "profile": profile,
        "required_fields": required_fields,
        "collected_fields": collected,
        "authenticated": bool(snapshot.get("authenticated", False)),
        "verification_sent": bool(snapshot.get("verification_sent", False)),
        "verification_contact": snapshot.get("verification_contact"),
        "verification_contact_type": snapshot.get("verification_contact_type"),
        "trading_token": snapshot.get("trading_token"),
        "submitted": bool(snapshot.get("submitted", False)),
        "submission_attempted": bool(snapshot.get("submission_attempted", False)),
        "document_captured": bool(snapshot.get("document_captured", False)),
        "document_upload_verified": bool(snapshot.get("document_upload_verified", False)),
        "awaiting_document_upload": bool(snapshot.get("awaiting_document_upload", False)),
        "document_extracted": bool(snapshot.get("document_extracted", False)),
        "used_widgets": list(snapshot.get("used_widgets", [])),
        "events": list(snapshot.get("events", [])),
        "errors": list(snapshot.get("errors", [])),
    }
    if state["authenticated"] and not state["trading_token"]:
        state["trading_token"] = f"token_{state['scenario_id']}"
    return state


class OnboardingTool(BaseTool):
    """One implementation class for all onboarding function schemas.

    The concrete function name is taken from the YAML tool schema. Trajectory
    state lives in ``agent_data.extra_fields`` so separate tool calls share the
    same simulated backend state.
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)

    async def execute(self, instance_id: str, parameters: dict[str, Any], **kwargs) -> tuple[ToolResponse, float, dict]:
        agent_data = kwargs.get("agent_data")
        if agent_data is None:
            return ToolResponse(text="Error: missing agent_data"), -0.2, {"error": "missing_agent_data"}

        state = self._state(agent_data)
        handler_prefix = "_handle_real_" if state.get("backend") == "real_bank" else "_handle_"
        handler = getattr(self, f"{handler_prefix}{self.name}", None)
        if handler is None:
            handler = getattr(self, f"_handle_{self.name}", None)
        if handler is None and self.name.startswith("present_"):
            handler = self._handle_present_generic
        if handler is None and self.name.startswith("submit_") and self.name != "submit_application":
            handler = self._handle_real_submit_section if state.get("backend") == "real_bank" else self._handle_submit_section
        if handler is None:
            result = self._error(state, "unknown_tool", f"Unsupported tool: {self.name}")
            return ToolResponse(text=self._format(result)), -0.2, result

        result = handler(state, parameters, agent_data)
        reward = self._step_reward(result)
        return ToolResponse(text=self._format(result)), reward, result

    async def release(self, instance_id: str, **kwargs) -> None:
        return None

    def _state(self, agent_data: Any) -> dict[str, Any]:
        if "onboarding_state" in agent_data.extra_fields:
            return agent_data.extra_fields["onboarding_state"]

        raw = agent_data.tools_kwargs.get("__onboarding_scenario_json__")
        if raw is None:
            raw = agent_data.tools_kwargs.get("__onboarding_scenario__", {})
        scenario = _loads_maybe_json(raw) or {}
        raw_state = agent_data.tools_kwargs.get("__onboarding_state__")
        if real_bank_enabled(agent_data.tools_kwargs, self.config):
            state = self._real_bank_initial_state(agent_data, scenario, raw_state)
            agent_data.extra_fields["onboarding_state"] = state
            return state

        state_snapshot = _loads_maybe_json(raw_state) or {}
        if isinstance(state_snapshot, dict) and state_snapshot:
            state = _state_from_snapshot(state_snapshot, scenario)
            agent_data.extra_fields["onboarding_state"] = state
            return state

        profile = copy.deepcopy(scenario.get("profile", {}))
        required_fields = copy.deepcopy(scenario.get("required_fields", DEFAULT_REQUIRED_FIELDS))
        collected = copy.deepcopy(scenario.get("initial_collected", {}))

        state = {
            "scenario_id": scenario.get("scenario_id", "unknown"),
            "profile": profile,
            "required_fields": required_fields,
            "collected_fields": collected,
            "authenticated": False,
            "verification_sent": False,
            "verification_contact": None,
            "verification_contact_type": None,
            "trading_token": None,
            "submitted": False,
            "submission_attempted": False,
            "document_captured": False,
            "document_upload_verified": False,
            "awaiting_document_upload": False,
            "document_extracted": False,
            "document_review_pending": False,
            "document_review_confirmed": False,
            "document_review_presented": False,
            "used_widgets": [],
            "events": [],
            "errors": [],
        }
        agent_data.extra_fields["onboarding_state"] = state
        return state

    def _real_bank_initial_state(self, agent_data: Any, scenario: dict[str, Any], raw_state: Any) -> dict[str, Any]:
        scenario = prepare_real_bank_scenario(scenario, request_id=getattr(agent_data, "request_id", None))
        profile = copy.deepcopy(scenario.get("profile", {}))
        required_fields = copy.deepcopy(scenario.get("required_fields", DEFAULT_REQUIRED_FIELDS))
        collected = copy.deepcopy(scenario.get("initial_collected", {}))
        if isinstance(collected, list):
            collected = {key: copy.deepcopy(profile.get(key, True)) for key in collected}
        if not isinstance(collected, dict):
            collected = {}
        state_snapshot = _loads_maybe_json(raw_state) or {}
        if isinstance(state_snapshot, dict) and state_snapshot:
            collected.update(copy.deepcopy(state_snapshot.get("collected_fields", {})))

        fake_verification_wrapper = real_bank_fake_verification_wrapper_enabled()
        wrapper_code = (
            str(profile.get("verification_code") or real_bank_verification_code())
            if fake_verification_wrapper
            else None
        )
        if fake_verification_wrapper and wrapper_code:
            profile["verification_code"] = wrapper_code

        trajectory_id = trajectory_id_for(scenario, getattr(agent_data, "request_id", None))
        agent_data.extra_fields["real_bank_trajectory_id"] = trajectory_id
        agent_data.extra_fields["scenario_json"] = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
        return {
            "backend": "real_bank",
            "scenario_id": scenario.get("scenario_id", "unknown"),
            "trajectory_id": trajectory_id,
            "profile": profile,
            "required_fields": required_fields,
            "collected_fields": collected,
            "authenticated": bool(state_snapshot.get("authenticated", False)) if isinstance(state_snapshot, dict) else False,
            "verification_sent": bool(state_snapshot.get("verification_sent", False)) if isinstance(state_snapshot, dict) else False,
            "verification_contact": state_snapshot.get("verification_contact") if isinstance(state_snapshot, dict) else None,
            "verification_contact_type": state_snapshot.get("verification_contact_type") if isinstance(state_snapshot, dict) else None,
            "fake_verification_wrapper": fake_verification_wrapper,
            "fake_upload_wrapper": real_bank_fake_upload_wrapper_enabled(),
            "bank_real_authenticated": bool(state_snapshot.get("bank_real_authenticated", False))
            if isinstance(state_snapshot, dict)
            else False,
            "current_verification_code": state_snapshot.get("current_verification_code", wrapper_code)
            if isinstance(state_snapshot, dict)
            else wrapper_code,
            "trading_token": state_snapshot.get("trading_token") if isinstance(state_snapshot, dict) else None,
            "bank_send_rate_limit_bypass": bool(state_snapshot.get("bank_send_rate_limit_bypass", False))
            if isinstance(state_snapshot, dict)
            else False,
            "bank_auth_bypass": bool(state_snapshot.get("bank_auth_bypass", False))
            if isinstance(state_snapshot, dict)
            else False,
            "submitted": bool(state_snapshot.get("submitted", False)) if isinstance(state_snapshot, dict) else False,
            "submission_attempted": bool(state_snapshot.get("submission_attempted", False)) if isinstance(state_snapshot, dict) else False,
            "document_captured": bool(state_snapshot.get("document_captured", False)) if isinstance(state_snapshot, dict) else False,
            "document_upload_verified": bool(state_snapshot.get("document_upload_verified", False))
            if isinstance(state_snapshot, dict)
            else False,
            "awaiting_document_upload": bool(state_snapshot.get("awaiting_document_upload", False))
            if isinstance(state_snapshot, dict)
            else False,
            "document_extracted": bool(state_snapshot.get("document_extracted", False)) if isinstance(state_snapshot, dict) else False,
            "document_review_pending": bool(state_snapshot.get("document_review_pending", False))
            if isinstance(state_snapshot, dict)
            else False,
            "document_review_confirmed": bool(state_snapshot.get("document_review_confirmed", False))
            if isinstance(state_snapshot, dict)
            else False,
            "document_review_presented": bool(state_snapshot.get("document_review_presented", False))
            if isinstance(state_snapshot, dict)
            else False,
            "used_widgets": list(state_snapshot.get("used_widgets", [])) if isinstance(state_snapshot, dict) else [],
            "events": list(state_snapshot.get("events", [])) if isinstance(state_snapshot, dict) else [],
            "errors": list(state_snapshot.get("errors", [])) if isinstance(state_snapshot, dict) else [],
            "bank_status": None,
            "bank_missing_fields": [],
            "bank_collected_fields": [],
            "bank_completion_percentage": None,
            "bank_app_no": None,
            "bank_query_ok": False,
            "bank_submit_success": False,
            "session_file": os.path.join(real_bank_session_root(), f"{trajectory_id}.json"),
            "application_source_sent": bool(state_snapshot.get("application_source_sent", False))
            if isinstance(state_snapshot, dict)
            else False,
            "bank_initial_prefill_sent": bool(state_snapshot.get("bank_initial_prefill_sent", False))
            if isinstance(state_snapshot, dict)
            else False,
            "captured_documents": copy.deepcopy(state_snapshot.get("captured_documents", {}))
            if isinstance(state_snapshot, dict)
            else {},
        }

    def _real_bank_api(self, state: dict[str, Any], agent_data: Any):
        api_key = f"{os.getpid()}:{state['trajectory_id']}:{real_bank_api_scripts_dir()}"
        if api_key not in _REAL_BANK_API_CACHE:
            _REAL_BANK_API_CACHE[api_key] = make_trade_api(state["trajectory_id"])
        return _REAL_BANK_API_CACHE[api_key]

    def _reset_real_bank_api(self, state: dict[str, Any]) -> Any:
        api_key = f"{os.getpid()}:{state['trajectory_id']}:{real_bank_api_scripts_dir()}"
        _REAL_BANK_API_CACHE.pop(api_key, None)
        _REAL_BANK_API_CACHE[api_key] = make_trade_api(state["trajectory_id"])
        return _REAL_BANK_API_CACHE[api_key]

    def _field_is_collected(self, state: dict[str, Any], field: str) -> bool:
        aliases = {field}
        if field == "given_name":
            aliases.add("gvie_name")
        elif field == "gvie_name":
            aliases.add("given_name")
        elif field == "drivers_license":
            collected = state.get("collected_fields", {})
            document = collected.get("drivers_license")
            if isinstance(document, dict) and isinstance(document.get("front"), dict) and isinstance(document.get("back"), dict):
                return True
            return "drivers_licence_front" in collected and "drivers_licence_back" in collected
        return any(alias in state["collected_fields"] for alias in aliases)

    def _local_missing_fields(self, state: dict[str, Any]) -> list[str]:
        return [field for field in state["required_fields"] if not self._field_is_collected(state, field)]

    def _bank_progress_complete(self, state: dict[str, Any]) -> bool:
        if state.get("bank_submit_success"):
            return True
        completion = state.get("bank_completion_percentage")
        try:
            return float(completion) >= 100.0
        except (TypeError, ValueError):
            return False

    def _require_user_upload(self) -> bool:
        return uploaded_image_required()

    def _refresh_document_review_confirmation(self, state: dict[str, Any], agent_data: Any) -> None:
        if state.get("document_review_confirmed"):
            return
        if state.get("document_review_pending") and _last_user_confirms_document_review(agent_data):
            state["document_review_confirmed"] = True
            state["document_review_pending"] = False

    def _document_collection_error(
        self, state: dict[str, Any], payload: dict[str, Any], agent_data: Any
    ) -> tuple[str, str, dict[str, Any]] | None:
        require_upload = self._require_user_upload()
        self._refresh_document_review_confirmation(state, agent_data)
        last_user = _last_user_raw_text(agent_data)
        payload_keys = set(payload)
        if require_upload and _document_payload_present(payload) and not state.get("document_upload_verified"):
            return "document_upload_missing", DOCUMENT_UPLOAD_REQUIRED_FOR_COLLECTION_MESSAGE, {}
        if _document_payload_present(payload) and state.get("document_captured") and not state.get("document_extracted"):
            return "document_extract_required", DOCUMENT_REVIEW_REQUIRED_MESSAGE, {}
        if state.get("document_review_pending") and not state.get("document_review_confirmed"):
            if "CAPTURE_RESULT" in last_user:
                return "document_review_pending", DOCUMENT_REVIEW_PENDING_MESSAGE, {}
            if _last_user_rejects_document_identity(agent_data):
                state["document_review_rejected"] = True
                return "document_review_rejected", DOCUMENT_REVIEW_REJECTED_MESSAGE, {}
            if payload_keys - DOCUMENT_REVIEW_COLLECT_FIELDS:
                return (
                    "document_collect_scope",
                    DOCUMENT_COLLECT_SCOPE_MESSAGE,
                    {"rejected_fields": {key: "not_from_document_review_response" for key in sorted(payload_keys - DOCUMENT_REVIEW_COLLECT_FIELDS)}},
                )
            if _document_review_collect_payload_present(payload):
                state["document_review_confirmed"] = True
                state["document_review_pending"] = False
                if not _last_user_confirms_document_review(agent_data):
                    state["document_review_corrected"] = True
            else:
                return "document_review_pending", DOCUMENT_REVIEW_PENDING_MESSAGE, {}
        if "CAPTURE_RESULT" in last_user and payload_keys - DOCUMENT_REVIEW_COLLECT_FIELDS:
            return (
                "document_collect_scope",
                DOCUMENT_COLLECT_SCOPE_MESSAGE,
                {"rejected_fields": {key: "not_from_confirmed_document_review" for key in sorted(payload_keys - DOCUMENT_REVIEW_COLLECT_FIELDS)}},
            )
        if state.get("document_review_confirmed") and _document_payload_present(payload):
            extra_fields = payload_keys - DOCUMENT_REVIEW_COLLECT_FIELDS
            if extra_fields:
                return (
                    "document_collect_scope",
                    DOCUMENT_COLLECT_SCOPE_MESSAGE,
                    {"rejected_fields": {key: "not_from_confirmed_document_review" for key in sorted(extra_fields)}},
                )
        return None

    def _missing_fields(self, state: dict[str, Any]) -> list[str]:
        if state.get("backend") == "real_bank" and state.get("bank_query_ok"):
            bank_missing = list(state.get("bank_missing_fields") or [])
            if bank_missing or self._bank_progress_complete(state):
                return bank_missing
        return self._local_missing_fields(state)

    def _summary(self, state: dict[str, Any]) -> dict[str, Any]:
        collected_field_names = set(state["collected_fields"].keys())
        driver_doc = state["collected_fields"].get("drivers_license")
        if isinstance(driver_doc, dict) and "drivers_license" in collected_field_names:
            if not (isinstance(driver_doc.get("front"), dict) and isinstance(driver_doc.get("back"), dict)):
                collected_field_names.remove("drivers_license")
                if isinstance(driver_doc.get("front"), dict):
                    collected_field_names.add("drivers_licence_front")
                if isinstance(driver_doc.get("back"), dict):
                    collected_field_names.add("drivers_licence_back")
        summary = {
            "scenario_id": state["scenario_id"],
            "backend": state.get("backend", "simulator"),
            "trajectory_id": state.get("trajectory_id"),
            "authenticated": state["authenticated"],
            "verification_sent": state["verification_sent"],
            "submitted": state["submitted"],
            "submission_attempted": state["submission_attempted"],
            "missing_fields": self._missing_fields(state),
            "collected_fields": sorted(collected_field_names),
            "document_captured": state["document_captured"],
            "document_upload_verified": state.get("document_upload_verified", False),
            "awaiting_document_upload": state.get("awaiting_document_upload", False),
            "document_extracted": state["document_extracted"],
            "document_review_pending": bool(state.get("document_review_pending", False)),
            "document_review_confirmed": bool(state.get("document_review_confirmed", False)),
            "document_review_presented": bool(state.get("document_review_presented", False)),
            "used_widgets": state["used_widgets"],
            "errors": state["errors"][-5:],
        }
        if state.get("backend") == "real_bank":
            summary.update(
                {
                    "bank_status": state.get("bank_status"),
                    "bank_app_no": state.get("bank_app_no"),
                    "bank_missing_fields": state.get("bank_missing_fields", []),
                    "bank_collected_fields": state.get("bank_collected_fields", []),
                    "bank_completion_percentage": state.get("bank_completion_percentage"),
                    "bank_query_ok": state.get("bank_query_ok", False),
                    "bank_submit_success": state.get("bank_submit_success", False),
                    "bank_initial_prefill_sent": state.get("bank_initial_prefill_sent", False),
                    "bank_real_authenticated": state.get("bank_real_authenticated", False),
                }
            )
        return summary

    def _result(self, state: dict[str, Any], status: str, message: str, **extra: Any) -> dict[str, Any]:
        event = {"tool": self.name, "status": status, "message": message}
        event.update(extra)
        state["events"].append(event)
        result = {"tool": self.name, "status": status, "message": message, "state": self._summary(state)}
        result.update(extra)
        return result

    def _error(self, state: dict[str, Any], code: str, message: str, **extra: Any) -> dict[str, Any]:
        state["errors"].append({"tool": self.name, "code": code, "message": message})
        return self._result(state, "error", message, error_code=code, **extra)

    def _format(self, result: dict[str, Any]) -> str:
        return f"{MARKER} {json.dumps(result, ensure_ascii=False, sort_keys=True)}"

    def _step_reward(self, result: dict[str, Any]) -> float:
        if result["status"] == "success":
            if result.get("state", {}).get("backend") == "real_bank" and result["tool"] == "submit_application":
                return 1.0 if result.get("bank_submit_success") else 0.2
            if result["tool"] == "submit_application":
                return 1.0
            if result["tool"] in {"collect_information", "login_and_get_token", "query_progress"} or str(result["tool"]).startswith("submit_"):
                return 0.1
            return 0.03
        return -0.15

    def _doc_key_for_file_type(self, file_type: Any) -> str:
        return _document_field_for_doc_type(str(file_type or ""))

    def _profile_document_value(self, state: dict[str, Any], doc_key: str) -> dict[str, Any]:
        captured = state.get("captured_documents") or {}
        if isinstance(captured, dict) and isinstance(captured.get(doc_key), dict):
            return captured[doc_key]
        profile_value = state.get("profile", {}).get(doc_key)
        return profile_value if isinstance(profile_value, dict) else {}

    def _submit_documents_payload(self, state: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
        documents = parameters.get("documents")
        if not isinstance(documents, list):
            documents = []
        payload: dict[str, Any] = {"documents": copy.deepcopy(documents)}
        for document in documents:
            if not isinstance(document, dict):
                continue
            file_type = str(document.get("file_type") or document.get("document_type") or "").strip()
            doc_key = self._doc_key_for_file_type(file_type)
            file_obj = {
                "file_id": document.get("file_id") or document.get("fileId"),
                "min_file_id": document.get("min_file_id") or document.get("minFileId"),
            }
            file_obj = {key: value for key, value in file_obj.items() if value}
            if file_obj:
                if doc_key == "drivers_license":
                    payload[doc_key] = _uploaded_document_value(payload.get(doc_key), file_obj, file_type)
                else:
                    payload[doc_key] = file_obj
            else:
                profile_doc = self._profile_document_value(state, doc_key)
                if profile_doc:
                    if doc_key == "drivers_license":
                        profile_file = _profile_file_for_doc_type(profile_doc, file_type)
                        payload[doc_key] = _uploaded_document_value(payload.get(doc_key), profile_file, file_type)
                    else:
                        payload[doc_key] = _single_file_document_value(profile_doc)

            if file_type == "passport":
                if document.get("passport_number"):
                    payload["passport_no"] = document["passport_number"]
                if document.get("expiration_date"):
                    payload["passport_expire_date"] = document["expiration_date"]
            elif file_type == "id_card" and document.get("id_number"):
                payload.setdefault("tax_id", document["id_number"])
                payload.setdefault("tax_id_country", document.get("issuing_country") or "CHN")
            elif file_type == "visa":
                if document.get("visa_type"):
                    payload["visa_type"] = document["visa_type"]
                if document.get("expiration_date"):
                    payload["visa_expiration_date"] = document["expiration_date"]
        if not documents:
            for doc_key in ("drivers_license", "passport_photo", "card_photo", "address_proof"):
                profile_doc = self._profile_document_value(state, doc_key)
                if profile_doc:
                    payload[doc_key] = (
                        copy.deepcopy(profile_doc)
                        if doc_key == "drivers_license"
                        else _single_file_document_value(profile_doc)
                    )
        return payload

    def _section_submit_payload(self, state: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(parameters or {})
        if self.name == "submit_account_type":
            payload = {"account_type": payload.get("account_type")}
            if "is_open_crypto" in parameters:
                payload["is_open_crypto"] = parameters["is_open_crypto"]
        elif self.name == "submit_personal_identity":
            if "first_name" in payload and "given_name" not in payload:
                payload["given_name"] = payload.pop("first_name")
            if "last_name" in payload and "family_name" not in payload:
                payload["family_name"] = payload.pop("last_name")
            if payload.get("tax_id") and payload.get("tax_id_country"):
                payload.setdefault(
                    "weight_form",
                    {"tax_id": payload["tax_id"], "tax_id_country": payload["tax_id_country"]},
                )
        elif self.name == "submit_residency_status":
            if str(state.get("profile", {}).get("branch") or "").upper() == "FOREIGNER":
                payload.setdefault("permanent_resident", False)
        elif self.name == "submit_documents":
            payload = self._submit_documents_payload(state, payload)
        elif self.name == "submit_agreements":
            payload = {"agreements_accepted": bool(payload.get("agreements_accepted"))}
        return {key: value for key, value in payload.items() if value not in (None, "")}

    def _handle_submit_section(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._handle_collect_information(state, {"data": self._section_submit_payload(state, parameters)}, agent_data)

    def _handle_real_submit_section(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._handle_real_collect_information(state, {"data": self._section_submit_payload(state, parameters)}, agent_data)

    def _sync_bank_progress(self, state: dict[str, Any], response: dict[str, Any]) -> None:
        summary = bank_progress_summary(response)
        state["bank_status"] = summary.get("status")
        state["bank_app_no"] = summary.get("app_no")
        state["bank_missing_fields"] = summary.get("missing_fields", [])
        state["bank_collected_fields"] = summary.get("collected_fields", [])
        state["bank_completion_percentage"] = summary.get("completion_percentage")
        state["bank_query_ok"] = bank_response_ok(response)
        if state["bank_query_ok"]:
            collected = {}
            for field in state["bank_collected_fields"]:
                profile_key = "gvie_name" if field == "given_name" else field
                value = state["profile"].get(field, state["profile"].get(profile_key, True))
                collected[profile_key] = copy.deepcopy(value)
                collected[field] = copy.deepcopy(value)
            state["collected_fields"].update(collected)

    def _sync_local_bank_progress(self, state: dict[str, Any]) -> dict[str, Any]:
        missing = self._local_missing_fields(state)
        collected = sorted(state.get("collected_fields", {}).keys())
        required = list(state.get("required_fields") or [])
        complete_count = sum(1 for field in required if field not in missing)
        completion = int(100 * complete_count / max(1, len(required)))
        status = "AUDITING" if state.get("submitted") and not missing else "COLLECTING"
        state["bank_status"] = status
        state["bank_app_no"] = state.get("bank_app_no") or f"local_{state.get('trajectory_id') or state.get('scenario_id')}"
        state["bank_missing_fields"] = missing
        state["bank_collected_fields"] = collected
        state["bank_completion_percentage"] = completion
        state["bank_query_ok"] = True
        return {
            "s": "ok",
            "d": {
                "app_no": state["bank_app_no"],
                "status": status,
                "missing_fields": missing,
                "collected_fields": collected,
                "completion_percentage": completion,
            },
        }

    def _real_error_from_exception(self, state: dict[str, Any], exc: Exception) -> dict[str, Any]:
        return self._error(state, "bank_api_error", f"Bank API error: {exc}")

    def _augment_real_collect_payload(self, state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(payload)
        if not state.get("application_source_sent") and "application_source" not in payload:
            payload["application_source"] = {
                "source": "AI_OPEN_ACCOUNT_SKILL",
                "packageName": "open-account-skill",
                "appVersion": "1.0.0",
            }

        captured = state.get("captured_documents") or {}
        driver_doc = captured.get("drivers_license")
        needs_driver = any(
            field in set(state.get("bank_missing_fields") or []) or field in set(self._local_missing_fields(state))
            for field in ("drivers_license", "drivers_licence_front", "drivers_licence_back")
        )
        already_has_driver = any(
            key in payload for key in ("drivers_license", "drivers_licence_front", "drivers_licence_back")
        )
        if driver_doc and needs_driver and not already_has_driver:
            document = normalize_document_value(driver_doc)
            if document:
                payload["drivers_license"] = document
                if document.get("front"):
                    payload["drivers_licence_front"] = document["front"]
                if document.get("back"):
                    payload["drivers_licence_back"] = document["back"]
        return payload

    def _real_bank_initial_prefill_enabled(self) -> bool:
        value = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_PREFILL_INITIAL_COLLECTED", "1")
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    def _real_bank_document_path(self) -> str:
        path = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_DOCUMENT_PATH", "")
        return path or os.fspath(real_bank_api_scripts_dir() / "test.png")

    def _document_file_type(self, doc_type: str) -> str:
        lowered = (doc_type or "").lower()
        if "passport" in lowered:
            return "passport"
        if "utility" in lowered or "bank_statement" in lowered or "credit_card_statement" in lowered or "address" in lowered:
            return "bank_statement" if "bank" in lowered else "utility_bill"
        if "visa" in lowered:
            return "visa"
        if "green" in lowered or "permanent_resident" in lowered:
            return "permanent_resident_card"
        if "id_card" in lowered or "government" in lowered:
            return "id_card"
        if "back" in lowered:
            return "drivers_licence_back"
        return "drivers_licence_front"

    def _capture_request_result(self, state: dict[str, Any], agent_data: Any, *, doc_type: str, doc_key: str) -> dict[str, Any]:
        state["awaiting_document_upload"] = True
        state["expected_doc_type"] = doc_type
        state["expected_document_field"] = doc_key
        mark_document_upload_requested(agent_data, doc_type)
        return self._result(
            state,
            "success",
            "Please upload the requested document image before I continue."
            if state.get("fake_upload_wrapper")
            else "CAPTURE_REQUESTED",
            doc_type=doc_type,
            document_field=doc_key,
            awaiting_upload=True,
            capture_requested=True,
        )

    def _local_upload_file_result(
        self,
        state: dict[str, Any],
        agent_data: Any,
        *,
        filename: str,
        doc_type: str,
        file_path: str | None = None,
    ) -> dict[str, Any]:
        doc_key = _document_field_for_doc_type(doc_type)
        active_upload = _active_verified_upload(agent_data)
        if active_upload:
            file_obj = {
                "file_id": str(active_upload.get("file_id") or ""),
                "min_file_id": str(active_upload.get("min_file_id") or ""),
            }
            filename = str(active_upload.get("filename") or filename)
        else:
            seed = f"{state['trajectory_id']}:{filename}:{file_path or ''}"
            suffix = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
            file_obj = {"file_id": f"file_{suffix}", "min_file_id": f"min_{suffix}"}
        document = _uploaded_document_value(state.get("captured_documents", {}).get(doc_key), file_obj, doc_type)
        state["document_captured"] = True
        mark_document_upload_satisfied(agent_data, state)
        state.setdefault("captured_documents", {})[doc_key] = document
        return self._result(
            state,
            "success",
            "CAPTURE_RESULT: document uploaded.",
            doc_type=doc_type or "drivers_license_front",
            document_field=doc_key,
            file_url=active_upload.get("file_url") if active_upload else f"uploaded://{filename}",
            file_id=file_obj["file_id"],
            min_file_id=file_obj["min_file_id"],
            verification_id=active_upload.get("verification_id") if active_upload else None,
            verified_image=bool(active_upload),
            bank_response={"s": "ok", "d": {"fileId": file_obj["file_id"], "minFileId": file_obj["min_file_id"]}},
        )

    def _document_image_context_result(self, state: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        capture = _apply_capture_result_from_last_user(state, agent_data)
        if self._require_user_upload() and not state.get("document_upload_verified"):
            return self._error(state, "document_upload_missing", UPLOAD_RETRY_MESSAGE)
        if not state["document_captured"]:
            return self._error(state, "document_not_captured", UPLOAD_RETRY_MESSAGE)
        state["document_extracted"] = True
        state["document_review_pending"] = True
        state["document_review_confirmed"] = False
        state["last_extracted_document_fields"] = {}
        return self._result(
            state,
            "success",
            (
                "The uploaded document image is available in the model context. "
                "Inspect the image directly and extract only fields that are visibly present; "
                "this tool does not return OCR or profile-derived fields. Next action: stop tool calling, show the "
                "readable document fields in a normal assistant message, and ask the user to confirm or correct them."
            ),
            image_available=bool(capture or state.get("document_upload_verified")),
            document_image_in_context=bool(capture or state.get("document_upload_verified")),
            model_should_extract_from_image=True,
            review_required=True,
            next_action="show_document_fields_to_user_for_review",
            tool_call_allowed_next=False,
            doc_type=capture.get("doc_type"),
            document_field=capture.get("document_field"),
            file_url=capture.get("file_url"),
            file_id=capture.get("file_id"),
            min_file_id=capture.get("min_file_id"),
            verification_id=capture.get("verification_id"),
            extraction_source="service_model_vision_context",
        )

    def _upload_real_bank_document(
        self,
        state: dict[str, Any],
        agent_data: Any,
        *,
        doc_key: str,
        doc_type: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        path = self._real_bank_document_path()
        file_type = self._document_file_type(doc_type)
        api = self._real_bank_api(state, agent_data)
        try:
            response = api.upload_file(path, is_need_min=real_bank_upload_thumbnail_enabled(), file_type=file_type)
        except TypeError:
            response = api.upload_file(path, is_need_min=real_bank_upload_thumbnail_enabled())
        file_obj = normalize_file_result(response)
        ok = bank_response_ok(response)
        if ok:
            state["document_captured"] = True
            source_file = file_obj or _profile_file_for_doc_type(state["profile"].get(doc_key, {}), doc_type)
            document = _uploaded_document_value(state.get("captured_documents", {}).get(doc_key), source_file, doc_type)
            if document:
                state.setdefault("captured_documents", {})[doc_key] = document
        return response, file_obj, ok

    def _prefill_real_bank_initial_collected(self, state: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        """Mirror scenario-resume fields into the real bank after auth.

        Prefix/resume rows can start with some KYC fields already collected.
        The real bank starts each generated identity from an empty application,
        so this keeps bank state aligned with any explicitly resumed scenario
        state before the model continues the flow.
        """

        if (
            state.get("bank_initial_prefill_sent")
            or state.get("bank_auth_bypass")
            or not state.get("authenticated")
            or not self._real_bank_initial_prefill_enabled()
        ):
            return {}

        initial = copy.deepcopy(state.get("collected_fields") or {})
        if not initial:
            state["bank_initial_prefill_sent"] = True
            return {}

        doc_keys: set[str] = set()
        text_data: dict[str, Any] = {}
        for key, value in initial.items():
            normalized_key = "given_name" if key == "gvie_name" else key
            if normalized_key in {
                "drivers_license",
                "drivers_licence_front",
                "drivers_licence_back",
                "passport_photo",
                "government_issued_id",
            }:
                doc_keys.add("passport_photo" if normalized_key == "passport_photo" else "drivers_license")
                continue
            text_data[normalized_key] = value

        result: dict[str, Any] = {}
        try:
            api = self._real_bank_api(state, agent_data)
            if text_data:
                payload = self._augment_real_collect_payload(state, normalize_collect_payload(text_data))
                response = api.collect_information(payload)
                result["collect_response"] = sanitize_bank_response(response)
                if bank_response_ok(response):
                    state["collected_fields"].update(copy.deepcopy(payload))
                    if "application_source" in payload:
                        state["application_source_sent"] = True

            if doc_keys and os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_PREFILL_DOCUMENTS", "1").strip().lower() in {
                "1",
                "true",
                "yes",
                "y",
                "on",
            }:
                uploads = []
                for doc_key in sorted(doc_keys):
                    doc_types = ["passport"] if doc_key == "passport_photo" else ["drivers_license_front", "drivers_license_back"]
                    for doc_type in doc_types:
                        response, file_obj, ok = self._upload_real_bank_document(
                            state,
                            agent_data,
                            doc_key=doc_key,
                            doc_type=doc_type,
                        )
                        uploads.append(
                            {
                                "doc_type": doc_type,
                                "ok": ok,
                                "file_obj": file_obj,
                                "response": sanitize_bank_response(response),
                            }
                        )
                result["document_uploads"] = uploads

                captured_doc = state.get("captured_documents", {}).get("drivers_license")
                if captured_doc:
                    payload = normalize_collect_payload({"drivers_license": captured_doc})
                    response = api.collect_information(payload)
                    result["document_collect_response"] = sanitize_bank_response(response)
                    if bank_response_ok(response):
                        state["collected_fields"].update(copy.deepcopy(payload))

            progress = api.query_progress()
            self._sync_bank_progress(state, progress)
            result["progress_response"] = sanitize_bank_response(progress)
        except Exception as exc:
            result["error"] = str(exc)
            state["errors"].append(
                {"tool": self.name, "code": "bank_initial_prefill_failed", "message": str(exc)[:512]}
            )
        finally:
            state["bank_initial_prefill_sent"] = True
        return result

    def _handle_real_send_verification_code(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        contact = str(parameters.get("contact", "")).strip()
        contact_type = str(parameters.get("contact_type", "")).strip().upper()
        area_code = str(parameters.get("area_code", state["profile"].get("area_code", "1"))).strip().lstrip("+")
        if not contact or contact_type not in {"MOBILE", "EMAIL"}:
            return self._error(state, "invalid_auth_contact", "contact and contact_type are required.")
        available = set(state["profile"].get("available_auth_methods", ["MOBILE"]))
        expected = state["profile"].get("auth_contacts", {}).get(contact_type, {})
        if contact_type not in available:
            return self._error(state, "auth_method_unavailable", f"{contact_type} authentication is not available for this account.")
        if contact_type == "MOBILE":
            expected_area_code = str(expected.get("area_code", state["profile"].get("area_code", "1")))
            contact, normalized_area_code = _normalize_mobile_contact(contact, expected_area_code)
            if area_code != expected_area_code:
                if area_code and contact.startswith(area_code) and expected_area_code == "1":
                    area_code = expected_area_code
                elif normalized_area_code == expected_area_code:
                    area_code = expected_area_code
            expected_contact = _digits_only(expected.get("contact", ""))
        else:
            expected_area_code = ""
            expected_contact = str(expected.get("contact", "")).strip()
        if contact != expected_contact:
            return self._error(state, "wrong_contact", "Verification contact does not match this account.")
        if contact_type == "MOBILE" and area_code != expected_area_code:
            return self._error(state, "wrong_area_code", "Mobile country code is wrong. Use the numeric country code, for example 1 for US numbers.")
        if state.get("authenticated"):
            return self._result(
                state,
                "success",
                "Already authenticated; no new verification code was sent.",
                bank_response={"s": "ok", "d": {"already_authenticated": True}},
            )
        if state.get("verification_sent"):
            if contact == state.get("verification_contact") and contact_type == state.get("verification_contact_type"):
                return self._result(
                    state,
                    "success",
                    "Verification code was already sent for this contact; no new code was sent.",
                    bank_response={"s": "ok", "d": {"already_sent": True}},
                )
            return self._error(
                state,
                "verification_already_sent",
                "A verification code has already been sent. Use the same contact to log in.",
            )
        try:
            api = self._real_bank_api(state, agent_data)
            response = api.send_verification_code(contact, contact_type, area_code)
            if contact_type == "MOBILE" and not bank_response_ok(response) and _retryable_send_code_response(response):
                time.sleep(0.2)
                response = api.send_verification_code(contact, contact_type, expected_area_code)
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        ok = bank_response_ok(response)
        rate_limit_bypass = False
        bypass_send_limit = str(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT", "1")).strip().lower()
        if not ok and _bank_send_rate_limit(response) and bypass_send_limit in {"1", "true", "yes", "y", "on"}:
            ok = True
            rate_limit_bypass = True
        if ok:
            state["verification_sent"] = True
            state["verification_contact"] = contact
            state["verification_contact_type"] = contact_type
            state["bank_send_rate_limit_bypass"] = rate_limit_bypass
            if state.get("fake_verification_wrapper"):
                state["current_verification_code"] = str(
                    state.get("current_verification_code")
                    or state["profile"].get("verification_code")
                    or real_bank_verification_code()
                )
        return self._result(
            state,
            "success" if ok else "error",
            (
                "Bank verification code sent."
                if ok and not rate_limit_bypass
                else "Bank verification code sent."
            )
            if ok
            else "Bank rejected verification code request.",
            bank_response={"s": "ok", "d": True} if rate_limit_bypass else sanitize_bank_response(response),
        )

    def _handle_real_login_and_get_token(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        if not state["verification_sent"]:
            return self._error(state, "verification_not_sent", "Send a verification code first.")
        contact = str(parameters.get("contact", "")).strip()
        contact_type = str(parameters.get("contact_type", "")).strip().upper()
        area_code = str(parameters.get("area_code", state["profile"].get("area_code", "1"))).strip().lstrip("+")
        code = str(parameters.get("verification_code", "")).strip()
        expected_area_code = str(state["profile"].get("area_code", "1"))
        if contact_type == "MOBILE":
            contact, normalized_area_code = _normalize_mobile_contact(contact, expected_area_code)
            if area_code != expected_area_code:
                if area_code and contact.startswith(area_code) and expected_area_code == "1":
                    area_code = expected_area_code
                elif normalized_area_code == expected_area_code:
                    area_code = expected_area_code
        if contact != str(state.get("verification_contact") or ""):
            return self._error(state, "wrong_login_contact", "Login contact differs from verification contact.")
        if contact_type != str(state.get("verification_contact_type") or "").upper():
            return self._error(state, "wrong_contact_type", "Login contact_type differs from verification contact_type.")
        expected_code = str(
            state.get("current_verification_code")
            or state["profile"].get("verification_code")
            or real_bank_verification_code()
        )
        if state.get("fake_verification_wrapper") and code != expected_code:
            return self._error(state, "wrong_code", "Verification code is incorrect.")
        if state.get("authenticated"):
            if state.get("bank_auth_bypass"):
                progress_response = self._sync_local_bank_progress(state)
                prefill_response = {}
            else:
                try:
                    token_response = {}
                    if real_bank_strict_production_execution_enabled() and not state.get("bank_real_authenticated"):
                        token_response = self._reset_real_bank_api(state).get_trading_token()
                        state["bank_real_authenticated"] = bank_response_ok(token_response)
                    prefill_response = self._prefill_real_bank_initial_collected(state, agent_data)
                    progress_response = self._real_bank_api(state, agent_data).query_progress()
                    self._sync_bank_progress(state, progress_response)
                except Exception:
                    progress_response = {}
                    prefill_response = {}
                    token_response = {}
            return self._result(
                state,
                "success",
                "Already logged in; trading token is available.",
                bank_login_response={"i18nMsg": "success", "data": {"already_authenticated": True}},
                bank_token_response=sanitize_bank_response(token_response or {"s": "ok", "d": {"already_authenticated": True}}),
                bank_prefill_response=sanitize_bank_response(prefill_response),
                bank_progress_response=sanitize_bank_response(progress_response),
                missing_fields=self._missing_fields(state),
                collected_fields=state.get("bank_collected_fields", []),
            )
        bypass_login = (
            state.get("bank_send_rate_limit_bypass")
            and str(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS", "1")).strip().lower()
            in {"1", "true", "yes", "y", "on"}
        )
        if bypass_login:
            if code != expected_code:
                return self._error(state, "wrong_code", "Verification code is incorrect.")
            state["authenticated"] = True
            state["trading_token"] = f"auth_bypass_{state['trajectory_id']}"
            state["bank_auth_bypass"] = True
            state["bank_real_authenticated"] = False
            progress_response = self._sync_local_bank_progress(state)
            return self._result(
                state,
                "success",
                "Bank login succeeded and trading token issued.",
                bank_login_response={"i18nMsg": "success", "data": True},
                bank_token_response={"s": "ok", "d": True},
                bank_progress_response=sanitize_bank_response(progress_response),
                missing_fields=self._missing_fields(state),
                collected_fields=state.get("bank_collected_fields", []),
            )
        try:
            api = self._real_bank_api(state, agent_data)
            login_response = api.login(contact, code, contact_type=contact_type, area_code=area_code)
            if login_response.get("data") and real_bank_strict_production_execution_enabled():
                api = self._reset_real_bank_api(state)
                token_response = api.get_trading_token()
            else:
                token_response = api.get_trading_token() if login_response.get("data") else {}
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        ok = bool(login_response.get("data")) and bank_response_ok(token_response)
        progress_response = {}
        prefill_response = {}
        if ok:
            state["authenticated"] = True
            state["trading_token"] = True
            state["bank_real_authenticated"] = real_bank_strict_production_execution_enabled()
            prefill_response = self._prefill_real_bank_initial_collected(state, agent_data)
            try:
                progress_response = self._real_bank_api(state, agent_data).query_progress()
                self._sync_bank_progress(state, progress_response)
            except Exception as exc:
                return self._real_error_from_exception(state, exc)
        return self._result(
            state,
            "success" if ok else "error",
            "Bank login succeeded and trading token issued." if ok else "Bank login or token request failed.",
            bank_login_response=sanitize_bank_response(login_response),
            bank_token_response=sanitize_bank_response(token_response),
            bank_prefill_response=sanitize_bank_response(prefill_response),
            bank_progress_response=sanitize_bank_response(progress_response),
            missing_fields=self._missing_fields(state),
            collected_fields=state.get("bank_collected_fields", []),
        )

    def _handle_real_query_progress(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        if not state["authenticated"]:
            return self._error(state, "not_authenticated", "Login is required before querying progress.")
        if state.get("bank_auth_bypass"):
            response = self._sync_local_bank_progress(state)
            return self._result(
                state,
                "success",
                "Bank progress queried.",
                missing_fields=self._missing_fields(state),
                collected_fields=state.get("bank_collected_fields", []),
                bank_response=sanitize_bank_response(response),
            )
        try:
            prefill_response = self._prefill_real_bank_initial_collected(state, agent_data)
            response = self._real_bank_api(state, agent_data).query_progress()
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        self._sync_bank_progress(state, response)
        return self._result(
            state,
            "success" if bank_response_ok(response) else "error",
            "Bank progress queried." if bank_response_ok(response) else "Bank progress query failed.",
            missing_fields=self._missing_fields(state),
            collected_fields=state.get("bank_collected_fields", []),
            bank_prefill_response=sanitize_bank_response(prefill_response),
            bank_response=sanitize_bank_response(response),
        )

    def _handle_real_collect_information(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        if not state["authenticated"]:
            return self._error(state, "not_authenticated", "Login is required before collecting information.")
        data = parameters.get("data", {})
        if not isinstance(data, dict) or not data:
            return self._error(state, "empty_data", "collect_information requires a non-empty data object.")
        _apply_capture_result_from_last_user(state, agent_data)
        payload = normalize_collect_payload(data)
        document_error = self._document_collection_error(state, payload, agent_data)
        if document_error:
            code, message, extra = document_error
            return self._error(state, code, message, **extra)
        payload = self._augment_real_collect_payload(state, payload)
        if state.get("bank_auth_bypass"):
            for key, value in payload.items():
                state["collected_fields"][key] = copy.deepcopy(value)
            if "application_source" in payload:
                state["application_source_sent"] = True
            progress_response = self._sync_local_bank_progress(state)
            return self._result(
                state,
                "success",
                "Information collected by bank.",
                accepted_fields=sorted(payload),
                rejected_fields={},
                missing_fields=self._missing_fields(state),
                bank_payload=payload,
                bank_response={"s": "ok", "d": True},
                bank_progress_response=sanitize_bank_response(progress_response),
            )
        try:
            api = self._real_bank_api(state, agent_data)
            response = api.collect_information(payload)
            progress_response = api.query_progress() if bank_response_ok(response) else {}
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        if bank_response_ok(response):
            for key, value in payload.items():
                state["collected_fields"][key] = copy.deepcopy(value)
            if "application_source" in payload:
                state["application_source_sent"] = True
            if progress_response:
                self._sync_bank_progress(state, progress_response)
        rejection_reason = _bank_rejection_reason(response) if not bank_response_ok(response) else ""
        return self._result(
            state,
            "success" if bank_response_ok(response) else "error",
            "Information collected by bank." if bank_response_ok(response) else "Bank rejected collected information.",
            accepted_fields=sorted(payload) if bank_response_ok(response) else [],
            rejected_fields={} if bank_response_ok(response) else {key: rejection_reason for key in payload},
            missing_fields=self._missing_fields(state),
            bank_payload=payload,
            bank_response=sanitize_bank_response(response),
            bank_progress_response=sanitize_bank_response(progress_response),
            bank_error_code=_bank_error_code(response) if not bank_response_ok(response) else None,
            bank_error_message=_bank_error_message(response) if not bank_response_ok(response) else None,
            bank_rejection_reason=rejection_reason if not bank_response_ok(response) else None,
        )

    def _handle_real_submit_application(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        state["submission_attempted"] = True
        if not state["authenticated"]:
            return self._error(state, "not_authenticated", "Cannot submit before login.")
        if state.get("bank_auth_bypass"):
            self._sync_local_bank_progress(state)
            missing_fields = self._missing_fields(state)
            if missing_fields:
                return self._error(
                    state,
                    "missing_fields",
                    "Cannot submit while bank reports missing fields.",
                    missing_fields=missing_fields,
                    bank_response={"s": "ok", "d": {"missing_fields": missing_fields}},
                )
            state["bank_submit_success"] = True
            state["submitted"] = True
            progress_after = self._sync_local_bank_progress(state)
            return self._result(
                state,
                "success",
                "Bank application submitted successfully.",
                bank_submit_success=True,
                bank_response={"s": "ok", "d": True},
                bank_progress_response=sanitize_bank_response(progress_after),
            )
        try:
            api = self._real_bank_api(state, agent_data)
            self._prefill_real_bank_initial_collected(state, agent_data)
            progress_before = api.query_progress()
            self._sync_bank_progress(state, progress_before)
            missing_fields = self._missing_fields(state)
            if missing_fields:
                return self._error(
                    state,
                    "missing_fields",
                    "Cannot submit while bank reports missing fields.",
                    missing_fields=missing_fields,
                    bank_response=sanitize_bank_response(progress_before),
                )
            response = api.submit_application()
            progress_after = api.query_progress()
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        ok = bank_response_ok(response)
        state["bank_submit_success"] = ok
        state["submitted"] = ok
        if progress_after:
            self._sync_bank_progress(state, progress_after)
        return self._result(
            state,
            "success" if ok else "error",
            "Bank application submitted successfully." if ok else "Bank application submission failed.",
            bank_submit_success=ok,
            bank_response=sanitize_bank_response(response),
            bank_progress_response=sanitize_bank_response(progress_after),
            bank_error_code=_bank_error_code(response) if not ok else None,
            bank_error_message=_bank_error_message(response) if not ok else None,
            bank_rejection_reason=_bank_rejection_reason(response) if not ok else None,
        )

    def _handle_real_upload_file(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        state["fake_upload_wrapper"] = real_bank_fake_upload_wrapper_enabled()
        active_upload = _active_verified_upload(agent_data)
        if self._require_user_upload() and not active_upload:
            return self._error(
                state,
                "customer_upload_required",
                UPLOAD_RETRY_MESSAGE,
                upload_gate=_upload_gate_debug(agent_data),
            )
        filename = str(active_upload.get("filename") or parameters.get("filename") or "document.png")
        file_data = parameters.get("file_data")
        is_need_min = _boolish(parameters.get("is_need_min"), real_bank_upload_thumbnail_enabled())
        doc_type = str(active_upload.get("doc_type") or parameters.get("doc_type") or parameters.get("file_type") or "").strip().lower()
        try:
            path = write_tool_upload_file(
                trajectory_id=state["trajectory_id"],
                filename=filename,
                file_data=file_data if isinstance(file_data, str) else None,
            )
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        if state.get("fake_upload_wrapper"):
            return self._local_upload_file_result(state, agent_data, filename=filename, doc_type=doc_type, file_path=path)
        file_type = self._document_file_type(doc_type) if doc_type else None
        progress_response = {}
        try:
            try:
                response = self._real_bank_api(state, agent_data).upload_file(
                    path,
                    is_need_min=is_need_min,
                    file_type=file_type,
                )
            except TypeError:
                response = self._real_bank_api(state, agent_data).upload_file(path, is_need_min=is_need_min)
        except Exception as exc:
            if not state.get("authenticated"):
                return self._error(
                    state,
                    "not_authenticated",
                    "Login is required before uploading directly to the bank.",
                )
            return self._real_error_from_exception(state, exc)
        file_obj = normalize_file_result(response)
        ok = bank_response_ok(response) and (file_obj.get("file_id") or response.get("d") is True)
        doc_key = _document_field_for_doc_type(doc_type)
        if ok:
            source_file = file_obj or _profile_file_for_doc_type(state["profile"].get(doc_key, {}), doc_type)
            document = _uploaded_document_value(state.get("captured_documents", {}).get(doc_key), source_file, doc_type)
            state["document_captured"] = True
            mark_document_upload_satisfied(agent_data, state)
            state.setdefault("bank_uploaded_documents", {})[doc_key] = {
                "doc_type": doc_type,
                "file_type": file_type,
                "bank_file_collect": response.get("d") is True and not file_obj,
            }
            if file_type:
                state["collected_fields"][file_type] = copy.deepcopy(file_obj or True)
            if document and file_obj:
                mark_document_upload_satisfied(agent_data, state)
                state.setdefault("captured_documents", {})[doc_key] = document
                state["collected_fields"][doc_key] = copy.deepcopy(document)
            try:
                progress_response = self._real_bank_api(state, agent_data).query_progress()
                self._sync_bank_progress(state, progress_response)
            except Exception:
                progress_response = {}
        return self._result(
            state,
            "success" if ok else "error",
            "CAPTURE_RESULT: document uploaded to bank." if ok else "Bank file upload failed.",
            doc_type=doc_type or "drivers_license_front",
            document_field=doc_key,
            file_url=f"file://{path}" if ok else None,
            file_id=file_obj.get("file_id"),
            min_file_id=file_obj.get("min_file_id"),
            verification_id=active_upload.get("verification_id") if active_upload else None,
            verified_image=bool(active_upload),
            bank_response=sanitize_bank_response(response),
            bank_progress_response=sanitize_bank_response(progress_response),
        )

    def _handle_real_capture_document(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        state["fake_upload_wrapper"] = real_bank_fake_upload_wrapper_enabled()
        doc_type = str(parameters.get("doc_type", "drivers_license_front"))
        doc_key = _document_field_for_doc_type(doc_type)
        if self._require_user_upload():
            return self._capture_request_result(state, agent_data, doc_type=doc_type, doc_key=doc_key)
        if state.get("fake_upload_wrapper"):
            profile_doc = state["profile"].get(doc_key)
            file_obj = _profile_file_for_doc_type(profile_doc, doc_type)
            document = _uploaded_document_value(state.get("captured_documents", {}).get(doc_key), file_obj, doc_type)
            state["document_captured"] = True
            mark_document_upload_satisfied(agent_data, state)
            state.setdefault("captured_documents", {})[doc_key] = document
            return self._result(
                state,
                "success",
                "CAPTURE_RESULT: document available.",
                doc_type=doc_type,
                document_field=doc_key,
                file_url=f"file://{doc_key}",
                file_id=file_obj.get("file_id"),
                min_file_id=file_obj.get("min_file_id"),
                bank_response={"s": "ok", "d": file_obj},
            )
        try:
            response, file_obj, ok = self._upload_real_bank_document(
                state,
                agent_data,
                doc_key=doc_key,
                doc_type=doc_type,
            )
            path = self._real_bank_document_path()
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        if ok:
            state["document_captured"] = True
            mark_document_upload_satisfied(agent_data, state)
            source_file = file_obj or _profile_file_for_doc_type(state["profile"].get(doc_key, {}), doc_type)
            document = _uploaded_document_value(state.get("captured_documents", {}).get(doc_key), source_file, doc_type)
            if document:
                state.setdefault("captured_documents", {})[doc_key] = document
                state["collected_fields"][doc_key] = copy.deepcopy(document)
        return self._result(
            state,
            "success" if ok else "error",
            "CAPTURE_RESULT: document uploaded to bank.",
            doc_type=doc_type,
            document_field=doc_key,
            file_url=f"file://{path}",
            file_id=file_obj.get("file_id"),
            min_file_id=file_obj.get("min_file_id"),
            bank_response=sanitize_bank_response(response),
        )

    def _handle_real_extract_document_info(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._document_image_context_result(state, agent_data)

    def _handle_real_get_user_info(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        if not state["authenticated"]:
            return self._error(state, "not_authenticated", "Login is required before getting user info.")
        if state.get("bank_auth_bypass"):
            response = {
                "i18nMsg": "success",
                "data": {
                    "userId": f"user_{state['trajectory_id']}",
                    "phone": state["profile"].get("mobile"),
                    "email": state["profile"].get("email"),
                    "emailVerify": 1,
                },
            }
            return self._result(
                state,
                "success",
                "Bank user info returned.",
                mobile_bound=True,
                email_bound=True,
                bank_response=sanitize_bank_response(response),
            )
        try:
            response = self._real_bank_api(state, agent_data).get_user_info()
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        data = response.get("data") if isinstance(response, dict) else {}
        data = data if isinstance(data, dict) else {}
        return self._result(
            state,
            "success" if bank_response_ok(response) else "error",
            "Bank user info returned." if bank_response_ok(response) else "Bank user info request failed.",
            mobile_bound=bool(data.get("phone")),
            email_bound=data.get("emailVerify") == 1,
            bank_response=sanitize_bank_response(response),
        )

    def _handle_real_update_email(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        try:
            response = self._real_bank_api(state, agent_data).update_email(
                email=str(parameters.get("email", "")).strip(),
                auth_code=str(parameters.get("auth_code", "")).strip(),
            )
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        return self._result(
            state,
            "success" if bank_response_ok(response) else "error",
            "Bank email updated." if bank_response_ok(response) else "Bank email update failed.",
            bank_response=sanitize_bank_response(response),
        )

    def _handle_real_update_mobile(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        try:
            response = self._real_bank_api(state, agent_data).update_mobile(
                phone=str(parameters.get("phone", "")).strip(),
                area_code=str(parameters.get("area_code", "1")).strip(),
                auth_code=str(parameters.get("auth_code", "")).strip(),
            )
        except Exception as exc:
            return self._real_error_from_exception(state, exc)
        return self._result(
            state,
            "success" if bank_response_ok(response) else "error",
            "Bank mobile updated." if bank_response_ok(response) else "Bank mobile update failed.",
            bank_response=sanitize_bank_response(response),
        )

    def _handle_send_verification_code(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        contact = str(parameters.get("contact", "")).strip()
        contact_type = str(parameters.get("contact_type", "")).strip().upper()
        area_code = str(parameters.get("area_code", state["profile"].get("area_code", ""))).strip()
        profile = state["profile"]
        available = set(profile.get("available_auth_methods", []))
        required = set(profile.get("required_auth_methods", []))
        auth_contacts = profile.get("auth_contacts", {})
        expected = auth_contacts.get(contact_type, {})
        if required and contact_type not in required:
            return self._error(state, "required_auth_method", f"{', '.join(sorted(required))} authentication is required.")
        if contact_type not in available:
            return self._error(state, "auth_method_unavailable", f"{contact_type} authentication is not available.")
        if contact != str(expected.get("contact")):
            return self._error(state, "wrong_contact", "Verification contact does not match the user profile.")
        if contact_type == "MOBILE" and area_code != str(expected.get("area_code", profile.get("area_code"))):
            return self._error(state, "wrong_area_code", "Mobile area code is wrong.")
        state["verification_sent"] = True
        state["verification_contact"] = contact
        state["verification_contact_type"] = contact_type
        return self._result(state, "success", "Verification code sent.")

    def _handle_login_and_get_token(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        if not state["verification_sent"]:
            return self._error(state, "verification_not_sent", "Send a verification code first.")
        if str(parameters.get("verification_code", "")).strip() != str(state["profile"].get("verification_code")):
            return self._error(state, "wrong_code", "Verification code is incorrect.")
        if str(parameters.get("contact", "")).strip() != state["verification_contact"]:
            return self._error(state, "wrong_login_contact", "Login contact differs from verification contact.")
        if str(parameters.get("contact_type", "")).strip().upper() != state["verification_contact_type"]:
            return self._error(state, "wrong_contact_type", "Login contact_type differs from verification contact_type.")
        state["authenticated"] = True
        state["trading_token"] = f"token_{state['scenario_id']}"
        return self._result(
            state,
            "success",
            "Login succeeded and trading token issued.",
            missing_fields=self._missing_fields(state),
            collected_fields=copy.deepcopy(state["collected_fields"]),
        )

    def _handle_query_progress(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        if not state["authenticated"]:
            return self._error(state, "not_authenticated", "Login is required before querying progress.")
        return self._result(
            state,
            "success",
            "Progress queried.",
            missing_fields=self._missing_fields(state),
            collected_fields=copy.deepcopy(state["collected_fields"]),
        )

    def _handle_collect_information(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        if not state["authenticated"]:
            return self._error(state, "not_authenticated", "Login is required before collecting information.")
        data = parameters.get("data", {})
        if not isinstance(data, dict) or not data:
            return self._error(state, "empty_data", "collect_information requires a non-empty data object.")
        _apply_capture_result_from_last_user(state, agent_data)
        document_error = self._document_collection_error(state, normalize_collect_payload(data), agent_data)
        if document_error:
            code, message, extra = document_error
            return self._error(state, code, message, **extra)

        accepted = {}
        rejected = {}
        for key, value in data.items():
            if key not in state["required_fields"] and key not in state["profile"]:
                rejected[key] = "field_not_expected"
                continue
            expected = state["profile"].get(key)
            if key in {"drivers_license", "passport_photo", "card_photo", "government_issued_id", "address_proof", "visa"}:
                if _document_value_has_file(value):
                    accepted[key] = value
                else:
                    rejected[key] = "document_requires_file_id_and_min_file_id"
            elif expected is None or _canonical(value) == _canonical(expected):
                accepted[key] = value
            else:
                rejected[key] = "value_does_not_match_user_profile"

        state["collected_fields"].update(copy.deepcopy(accepted))
        if rejected:
            state["errors"].append({"tool": self.name, "code": "rejected_fields", "fields": rejected})
        status = "success" if accepted else "error"
        return self._result(
            state,
            status,
            "Information collected." if accepted else "No valid information was collected.",
            accepted_fields=sorted(accepted),
            rejected_fields=rejected,
            missing_fields=self._missing_fields(state),
        )

    def _handle_submit_application(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        state["submission_attempted"] = True
        if not state["authenticated"]:
            return self._error(state, "not_authenticated", "Cannot submit before login.")
        missing = self._missing_fields(state)
        if missing:
            return self._error(state, "missing_fields", "Cannot submit while fields are missing.", missing_fields=missing)
        last_user = _last_user_text(agent_data)
        if not re.search(r"\b(confirm|confirmed|yes|submit|go ahead|agree)\b", last_user):
            return self._error(state, "missing_user_confirmation", "User confirmation is required before submit.")
        state["submitted"] = True
        return self._result(state, "success", "Application submitted successfully.")

    def _handle_upload_file(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        active_upload = _active_verified_upload(agent_data)
        if self._require_user_upload() and not active_upload:
            return self._error(
                state,
                "customer_upload_required",
                UPLOAD_RETRY_MESSAGE,
                upload_gate=_upload_gate_debug(agent_data),
            )
        filename = str(active_upload.get("filename") or parameters.get("filename", "document.jpg"))
        doc_type = str(active_upload.get("doc_type") or parameters.get("doc_type") or parameters.get("file_type") or "drivers_license_front")
        doc_field = _document_field_for_doc_type(doc_type)
        if active_upload:
            file_obj = {
                "file_id": str(active_upload.get("file_id") or ""),
                "min_file_id": str(active_upload.get("min_file_id") or ""),
            }
        else:
            suffix = hashlib.sha1(filename.encode("utf-8")).hexdigest()[:10]
            file_id = f"file_{state['scenario_id']}_{suffix}"
            file_obj = {"file_id": file_id, "min_file_id": f"min_{file_id}"}
        state["document_captured"] = True
        mark_document_upload_satisfied(agent_data, state)
        captured = state.setdefault("captured_documents", {})
        captured[doc_field] = _uploaded_document_value(captured.get(doc_field), file_obj, doc_type)
        return self._result(
            state,
            "success",
            "CAPTURE_RESULT: document uploaded.",
            doc_type=doc_type,
            document_field=doc_field,
            file_url=active_upload.get("file_url") if active_upload else f"uploaded://{filename}",
            file_id=file_obj["file_id"],
            min_file_id=file_obj["min_file_id"],
            verification_id=active_upload.get("verification_id") if active_upload else None,
            verified_image=bool(active_upload),
        )

    def _handle_capture_document(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        doc_type = str(parameters.get("doc_type", "drivers_license_front"))
        doc_field = _document_field_for_doc_type(doc_type)
        if self._require_user_upload():
            state["awaiting_document_upload"] = True
            state["expected_doc_type"] = doc_type
            state["expected_document_field"] = doc_field
            return self._result(
                state,
                "success",
                "Please upload the requested document image before I continue.",
                doc_type=doc_type,
                document_field=doc_field,
                awaiting_upload=True,
            )
        state["document_captured"] = True
        mark_document_upload_satisfied(agent_data, state)
        doc = state["profile"].get(doc_field, {})
        file_obj = _profile_file_for_doc_type(doc, doc_type)
        captured = state.setdefault("captured_documents", {})
        captured[doc_field] = _uploaded_document_value(captured.get(doc_field), file_obj, doc_type)
        return self._result(
            state,
            "success",
            "CAPTURE_RESULT: document captured.",
            doc_type=doc_type,
            document_field=doc_field,
            file_url=f"synthetic://{state['scenario_id']}/{doc_field}.jpg",
            file_id=file_obj.get("file_id"),
            min_file_id=file_obj.get("min_file_id"),
        )

    def _handle_extract_document_info(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._document_image_context_result(state, agent_data)

    def _handle_get_user_info(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        return self._result(
            state,
            "success",
            "User info returned.",
            mobile_bound=state["profile"].get("contact_type") == "MOBILE",
            email_bound=state["profile"].get("contact_type") == "EMAIL",
        )

    def _handle_update_email(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        return self._result(state, "success", "Email updated.")

    def _handle_update_mobile(self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any) -> dict[str, Any]:
        return self._result(state, "success", "Mobile updated.")

    def _handle_classify_document_type(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        file_type = str(parameters.get("file_type") or parameters.get("doc_type") or "").strip()
        if file_type:
            state["last_classified_document_type"] = file_type
            state.setdefault("classified_documents", []).append(file_type)
        return self._result(state, "success", "Document type classified.", file_type=file_type)

    def _widget(self, state: dict[str, Any], widget_name: str) -> dict[str, Any]:
        if widget_name not in state["used_widgets"]:
            state["used_widgets"].append(widget_name)
        return self._result(state, "success", "Widget displayed.")

    def _handle_present_generic(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._widget(state, self.name)

    def _handle_present_options(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._widget(state, "present_options")

    def _handle_present_date_input(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._widget(state, "present_date_input")

    def _handle_present_phone_input(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._widget(state, "present_phone_input")

    def _handle_present_email_input(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._widget(state, "present_email_input")

    def _handle_present_drivers_license_review(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        state["document_review_pending"] = True
        state["document_review_presented"] = True
        if parameters.get("fields"):
            state["last_presented_document_fields"] = copy.deepcopy(parameters.get("fields"))
        return self._result(
            state,
            "success",
            "No separate review widget was displayed. Show the extracted document fields in a normal assistant message and ask the user to confirm or correct them before submit_documents.",
        )

    def _handle_present_disclosure(
        self, state: dict[str, Any], parameters: dict[str, Any], agent_data: Any
    ) -> dict[str, Any]:
        return self._widget(state, "present_disclosure")
