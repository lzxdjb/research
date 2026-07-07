"""Simulated users for onboarding multi-turn RL."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from verl.interactions.base import BaseInteraction

from recipe.digital_onboarding.debug_logging import append_debug_csv
from recipe.digital_onboarding.image_verification import real_bank_upload_image_path, verify_sample_image_upload
from recipe.digital_onboarding.prompts import CUSTOMER_SIMULATOR_SYSTEM_PROMPT
from recipe.digital_onboarding.real_bank import (
    prepare_real_bank_scenario,
    real_bank_enabled,
    real_bank_fake_upload_wrapper_enabled,
    real_bank_upload_thumbnail_enabled,
)
from recipe.digital_onboarding.tools import (
    MARKER,
    UPLOADED_IMAGE_MARKER,
    assistant_requests_document_upload,
    clear_user_upload_in_progress,
    document_upload_pending,
    register_verified_upload,
)
from recipe.digital_onboarding.tools import OnboardingTool
from verl.tools.schemas import OpenAIFunctionToolSchema

TERMINATION_SIGNAL = "[[TERMINATE CHAT]]"
FINAL_CLOSING_RE = re.compile(
    r"\bno\s+problem(?:\s+at\s+all)?\s*!?\s+feel\s+free\s+to\s+chat\s+with\s+me\s+again\s+next\s+time\s*!?",
    re.IGNORECASE,
)
FINAL_CLOSING_INTENT_RE = re.compile(
    r"\b(?:goodbye|thank(?:\s+you)?|thanks|take\s+care|have\s+(?:a\s+)?(?:great|wonderful)\s+day)\b"
    r".{0,240}\bfeel\s+free\s+to\s+(?:chat|reach\s+out|contact)(?:\s+with\s+(?:me|us))?\b",
    re.IGNORECASE | re.DOTALL,
)
GENERIC_FINAL_CLOSING_RE = re.compile(
    r"\b(?:goodbye|bye\b|take\s+care|have\s+(?:a\s+)?(?:great|wonderful|fantastic|nice)\s+day)\b",
    re.IGNORECASE,
)
FIELD_GUARD_RE = re.compile(
    r"\b("
    r"account type|crypto|first name|given name|last name|family name|date of birth|birth date|gender|"
    r"citizenship|birth country|country of birth|tax id|tax identifier|address|social security|ssn|marital|dependents|employment|employer|"
    r"position|industry|funding|annual income|liquid net worth|total net worth|investment experience|"
    r"investment objective|objective|time horizon|risk tolerance|liquidity|control person|finra|exchange|"
    r"politically exposed|trade authorization|identity|agreement|disclosure|license|document|passport|address proof|visa|id card|green card|"
    r"phone|mobile|email|verification code|otp|confirm|submit|review"
    r")\b",
    re.IGNORECASE,
)
CUSTOMER_META_RESPONSE_MARKERS = (
    "according to the profile",
    "according to the scenario",
    "as the customer",
    "as a customer",
    "the customer is being asked",
    "the customer should",
    "the customer needs",
    "the service is asking",
    "the service agent",
    "the assistant is asking",
    "the assistant asked",
    "hidden scenario",
    "private customer scenario",
    "provided scenario",
    "my job is",
    "service-model",
    "service model",
    "the next step",
    "the workflow",
    "i should answer",
    "i should provide",
    "i need to answer",
    "i need to provide",
    "i will respond",
    "return exactly one compact json",
)

INTERRUPTION_MESSAGES: tuple[dict[str, str], ...] = (
    {"category": "information_change", "text": "I want to change my first name to Jack."},
    {"category": "information_change", "text": "I think my last name is incorrect. Can I update it?"},
    {"category": "information_change", "text": "I moved recently. Can I change my address?"},
    {"category": "information_change", "text": "I entered the wrong date of birth earlier."},
    {"category": "information_change", "text": "Can I update my phone number?"},
    {"category": "progress_status", "text": "What information have you already collected?"},
    {"category": "progress_status", "text": "Which onboarding step are we currently on?"},
    {"category": "progress_status", "text": "How many steps are left?"},
    {"category": "progress_status", "text": "Can you summarize everything I have provided so far?"},
    {"category": "progress_status", "text": "What information are you still missing?"},
    {"category": "process_question", "text": "Why do you need this information?"},
    {"category": "process_question", "text": "How will my data be used?"},
    {"category": "process_question", "text": "Is my information stored securely?"},
    {"category": "process_question", "text": "Who can access my information?"},
    {"category": "process_question", "text": "How long does the verification process take?"},
    {"category": "eligibility_product", "text": "Am I eligible to open this account?"},
    {"category": "eligibility_product", "text": "What are the account fees?"},
    {"category": "eligibility_product", "text": "What investment products do you support?"},
    {"category": "eligibility_product", "text": "Can I open multiple accounts?"},
    {"category": "eligibility_product", "text": "Do you support international customers?"},
    {"category": "off_topic", "text": "What is the weather like today?"},
    {"category": "off_topic", "text": "Can you tell me a joke?"},
    {"category": "off_topic", "text": "What is the capital of France?"},
    {"category": "off_topic", "text": "Who are you?"},
    {"category": "off_topic", "text": "What model are you using?"},
    {"category": "workflow_manipulation", "text": "Can we skip the remaining steps?"},
    {"category": "workflow_manipulation", "text": "Can you approve my application now?"},
    {"category": "workflow_manipulation", "text": "I don't want to upload my ID. Can we continue anyway?"},
    {"category": "workflow_manipulation", "text": "Can you just fill in the missing information for me?"},
    {"category": "workflow_manipulation", "text": "Let's finish the verification later and continue now."},
)


def _loads_maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _boolish(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    return _boolish(os.environ.get(name), default)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return _content_to_text(message.get("content", "")).lower()
    return ""


def _strip_model_artifacts(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    dangling_think_closes = list(re.finditer(r"</think>", text, flags=re.IGNORECASE))
    if dangling_think_closes:
        suffix = text[dangling_think_closes[-1].end() :].strip()
        text = suffix if suffix else re.sub(r"</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<\|im_(?:start|end)\|>", "", text)
    text = re.sub(r"\bassistant\s*$", "", text.strip(), flags=re.IGNORECASE)
    return text.strip()


def _first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _looks_like_customer_meta_response(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", _strip_model_artifacts(text or "")).strip().lower()
    if not cleaned:
        return False
    head = cleaned[:900]
    if "<think" in head or "</think>" in head:
        return True
    return any(marker in head for marker in CUSTOMER_META_RESPONSE_MARKERS)


def _assistant_has_final_closing_signal(text: str) -> bool:
    cleaned = _strip_model_artifacts(text or "")
    return bool(
        FINAL_CLOSING_RE.search(cleaned)
        or FINAL_CLOSING_INTENT_RE.search(cleaned)
        or GENERIC_FINAL_CLOSING_RE.search(cleaned)
    )


def _should_use_rule_field_guard(text: str) -> bool:
    enabled = os.environ.get("DIGITAL_ONBOARDING_RULE_GUARD_FIELDS", "1").strip().lower()
    if enabled not in {"1", "true", "yes", "y", "on"}:
        return False
    cleaned = _strip_model_artifacts(text or "")
    lower = cleaned.lower()
    return bool(
        FIELD_GUARD_RE.search(cleaned)
        or "from the options" in lower
        or "select your" in lower
        or "choose your" in lower
    )


def _visible_chat_line(message: dict[str, Any]) -> str | None:
    role = message.get("role", "unknown")
    if role not in {"assistant", "user"}:
        return None
    text = _content_to_text(message.get("content", ""))
    if MARKER in text:
        return None
    if role == "assistant":
        text = _strip_model_artifacts(text)
        label = "service"
    else:
        label = "user"
    if not text:
        return None
    return f"{label}: {text}"


def _capture_result(upload: dict[str, Any], doc_key: str = "drivers_license", doc_type: str = "drivers_license_front") -> str:
    filename = upload.get("filename") or f"{doc_key}.png"
    return (
        "CAPTURE_RESULT: document uploaded.\n"
        f"{UPLOADED_IMAGE_MARKER}\n"
        f"File Name: {filename}\n"
        f"Doc Type: {doc_type}\n"
        f"Document Field: {doc_key}\n"
        f"File URL: {upload.get('file_url') or ''}\n"
        f"File ID: {upload.get('file_id') or ''}\n"
        f"Min File ID: {upload.get('min_file_id') or ''}\n"
        f"Verification ID: {upload.get('verification_id') or ''}"
    )


def _upload_tool_schema() -> OpenAIFunctionToolSchema:
    return OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": "upload_file",
                "description": "Backend upload helper.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_data": {"type": "string"},
                        "filename": {"type": "string"},
                        "is_need_min": {"type": "boolean"},
                        "doc_type": {"type": "string"},
                    },
                    "required": ["filename"],
                },
            },
        }
    )


def _parse_tool_result(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    stripped = text.strip()
    if not stripped.startswith(MARKER):
        return {"message": stripped}
    try:
        parsed = json.loads(stripped[len(MARKER) :].strip())
    except json.JSONDecodeError:
        return {"message": stripped}
    return parsed if isinstance(parsed, dict) else {"message": str(parsed)}


class RuleBasedOnboardingUserInteraction(BaseInteraction):
    """A deterministic user simulator for bootstrapping RL.

    This is intentionally simple. Its job is not to be a perfect human; its job
    is to produce stable rollouts so the assistant learns the tool contract.
    Later, train a model-based simulator from the SFT data generated by
    ``scripts/build_sim_user_sft_data.py``.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.termination_signal = config.get("termination_signal", TERMINATION_SIGNAL)
        max_user_turns = os.environ.get("CUSTOMER_MAX_USER_TURNS", config.get("max_user_turns"))
        self.max_user_turns = int(max_user_turns) if max_user_turns not in (None, "", "null", "None") else 0
        self.interruption_enabled = _env_bool(
            "DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_ENABLED",
            _boolish(config.get("interruption_enabled"), False),
        )
        self.interruption_min_interval = max(
            1,
            _env_int(
                "DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MIN_TURNS",
                int(config.get("interruption_min_turns", 3)),
            ),
        )
        self.interruption_max_interval = max(
            self.interruption_min_interval,
            _env_int(
                "DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MAX_TURNS",
                int(config.get("interruption_max_turns", 5)),
            ),
        )
        self.interruption_seed = str(
            os.environ.get("DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_SEED", config.get("interruption_seed", "17"))
        )
        self._instance_dict: dict[str, dict[str, Any]] = {}

    async def start_interaction(self, instance_id: Optional[str] = None, **kwargs) -> str:
        if instance_id is None:
            instance_id = str(uuid4())
        scenario = _loads_maybe_json(kwargs.get("scenario_json") or kwargs.get("scenario") or {})
        if real_bank_enabled(kwargs, self.config):
            scenario = prepare_real_bank_scenario(scenario, request_id=instance_id)
        self._instance_dict[instance_id] = {
            "instance_id": instance_id,
            "scenario": scenario,
            "turns": 0,
            "answered_fields": set(),
            "uploaded_doc_types": set(),
            "uploaded_doc_fields": set(),
            "agent_data": None,
            "interruption_rng": random.Random(f"{self.interruption_seed}:{instance_id}"),
            "next_interruption_turn": None,
            "interruption_count": 0,
        }
        return instance_id

    async def generate_response(
        self, instance_id: str, messages: list[dict[str, Any]], **kwargs
    ) -> tuple[bool, str, float, dict]:
        state = self._instance_dict[instance_id]
        if kwargs.get("agent_data") is not None:
            state["agent_data"] = kwargs.get("agent_data")
        state["turns"] += 1
        scenario = state["scenario"]
        profile = scenario.get("profile", {})

        transcript = "\n".join(f"{m.get('role')}: {_content_to_text(m.get('content'))}" for m in messages[-6:])
        lower = _last_assistant_text(messages)
        if _assistant_has_final_closing_signal(lower):
            return True, self.termination_signal, 0.5, {
                "sim_user_done": True,
                "reason": "assistant_final_closing_signal",
            }

        if '"submitted": true' in transcript.lower() or "application submitted successfully" in transcript.lower():
            if not state.get("submitted_ack_sent"):
                state["submitted_ack_sent"] = True
                return False, "Great, thank you.", 0.0, {"reason": "submit_success_ack"}
            return True, self.termination_signal, 1.0, {"sim_user_done": True}
        if state.get("defer_ack_sent") and re.search(
            r"(come back|return|later|cannot continue|can't continue|unable to continue|need.{0,40}(mobile|phone|authentication|verification))",
            lower,
        ):
            return True, self.termination_signal, 0.5, {"sim_user_done": True, "reason": "auth_deferred"}
        if self.max_user_turns and state["turns"] >= self.max_user_turns:
            return True, self.termination_signal, -0.2, {"sim_user_done": False, "reason": "max_user_turns"}

        if assistant_requests_document_upload(lower):
            response = await self._answer_async(lower, profile, state)
            return False, response, 0.0, {"sim_user_backend": "rule_upload_guard"}
        interruption = self._maybe_interruption(state, lower)
        if interruption:
            return False, interruption["text"], 0.0, {
                "sim_user_backend": "interruption",
                "interruption": True,
                "interruption_category": interruption["category"],
                "interruption_count": state.get("interruption_count", 0),
            }

        response = await self._answer_async(lower, profile, state)
        if self._is_deferred_auth_response(state, response):
            return True, response, 0.5, {"sim_user_done": True, "reason": "auth_deferred"}
        return False, response, 0.0, {}

    async def finalize_interaction(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)

    async def _upload_document_image(self, instance_id: str, doc_key: str, doc_type: str) -> str:
        upload = verify_sample_image_upload(trajectory_id=instance_id, doc_type=doc_type)
        state = self._instance_dict[instance_id]
        agent_data = self._instance_dict[instance_id].get("agent_data")
        if agent_data is not None:
            register_verified_upload(agent_data, upload)
            if real_bank_enabled(getattr(agent_data, "tools_kwargs", {}), self.config) and not real_bank_fake_upload_wrapper_enabled():
                upload_result = await self._execute_real_upload_tool(agent_data, upload, doc_type=doc_type)
                if upload_result.get("status") != "success":
                    clear_user_upload_in_progress(agent_data)
                    return "I tried to upload the document image, but the upload did not complete successfully."
                self._remember_uploaded_document(state, doc_key, doc_type)
                return _capture_result(
                    {
                        "filename": upload.get("filename"),
                        "verification_id": upload.get("verification_id"),
                        "file_url": upload_result.get("file_url") or upload.get("file_url"),
                        "file_id": upload_result.get("file_id") or "",
                        "min_file_id": upload_result.get("min_file_id") or "",
                        "doc_type": upload_result.get("doc_type") or doc_type,
                    },
                    doc_key,
                    doc_type,
                )
        self._remember_uploaded_document(state, doc_key, doc_type)
        return _capture_result(upload, doc_key, doc_type)

    def _remember_uploaded_document(self, state: dict[str, Any], doc_key: str, doc_type: str) -> None:
        doc_types = state.setdefault("uploaded_doc_types", set())
        doc_fields = state.setdefault("uploaded_doc_fields", set())
        if isinstance(doc_types, set):
            doc_types.add(doc_type)
        if isinstance(doc_fields, set):
            doc_fields.add(doc_key)

    def _has_uploaded_doc_type(self, state: dict[str, Any], doc_type: str) -> bool:
        doc_types = state.get("uploaded_doc_types")
        return isinstance(doc_types, set) and doc_type in doc_types

    async def _execute_real_upload_tool(self, agent_data: Any, upload: dict[str, Any], *, doc_type: str) -> dict[str, Any]:
        tool = OnboardingTool(config={"type": "native", "backend": "real_bank"}, tool_schema=_upload_tool_schema())
        instance_id, _ = await tool.create()
        try:
            path = real_bank_upload_image_path()
            with open(path, "rb") as f:
                file_data = base64.b64encode(f.read()).decode("ascii")
            response, _reward, result = await tool.execute(
                instance_id,
                {
                    "filename": str(upload.get("filename") or os.path.basename(path)),
                    "file_data": file_data,
                    "is_need_min": real_bank_upload_thumbnail_enabled(),
                    "doc_type": doc_type,
                },
                agent_data=agent_data,
            )
            parsed = result if isinstance(result, dict) else _parse_tool_result(response.text)
            return parsed if isinstance(parsed, dict) else {}
        finally:
            clear_user_upload_in_progress(agent_data)
            await tool.release(instance_id)

    async def _answer_async(self, lower: str, profile: dict[str, Any], state: dict[str, Any]) -> str:
        behavior = state["scenario"].get("user_behavior", "cooperative")
        if assistant_requests_document_upload(lower) or re.search(r"\b(upload|image|photo|picture|scan)\b", lower):
            doc_key, doc_type = self._document_upload_target(lower, profile, state)
            if behavior == "passport_only" and profile.get("branch") != "FOREIGNER":
                if "passport" in lower or "document" in lower:
                    return await self._upload_document_image(state["instance_id"], "passport_photo", "passport")
                return "I don't have my driver's license, but I do have my passport."
            return await self._upload_document_image(state["instance_id"], doc_key, doc_type)
        return self._answer(lower, profile, state)

    def _document_upload_target(self, lower: str, profile: dict[str, Any], state: dict[str, Any]) -> tuple[str, str]:
        mentions_passport = "passport" in lower
        mentions_visa = "visa" in lower
        mentions_green_card = "green card" in lower or "permanent resident" in lower
        mentions_driver = "driver" in lower or "license" in lower or "licence" in lower
        if "address proof" in lower or "utility bill" in lower or "bank statement" in lower or "credit card statement" in lower:
            return "address_proof", "bank_statement"
        if mentions_driver:
            if "front" in lower and not self._has_uploaded_doc_type(state, "drivers_license_front"):
                return "drivers_license", "drivers_license_front"
            if "back" in lower and not self._has_uploaded_doc_type(state, "drivers_license_back"):
                return "drivers_license", "drivers_license_back"
            if not self._has_uploaded_doc_type(state, "drivers_license_front"):
                return "drivers_license", "drivers_license_front"
            if not self._has_uploaded_doc_type(state, "drivers_license_back"):
                return "drivers_license", "drivers_license_back"
            return "drivers_license", "drivers_license_front"
        if mentions_passport and (mentions_visa or mentions_green_card):
            if not self._has_uploaded_doc_type(state, "passport"):
                return "passport_photo", "passport"
            if mentions_green_card:
                return "card_photo", "permanent_resident_card"
            return "visa", "visa"
        if mentions_visa:
            return "visa", "visa"
        if mentions_green_card:
            return "card_photo", "permanent_resident_card"
        if "id card" in lower or "government" in lower:
            return "card_photo", "id_card"
        if mentions_passport or profile.get("branch") == "FOREIGNER":
            return "passport_photo", "passport"
        return "drivers_license", "drivers_license_front"

    def _maybe_interruption(self, state: dict[str, Any], lower: str) -> dict[str, str] | None:
        if not self.interruption_enabled:
            return None
        if assistant_requests_document_upload(lower):
            return None
        agent_data = state.get("agent_data")
        if agent_data is not None and document_upload_pending(agent_data):
            return None
        rng = state.get("interruption_rng")
        if not isinstance(rng, random.Random):
            rng = random.Random(f"{self.interruption_seed}:{state.get('instance_id', '')}")
            state["interruption_rng"] = rng
        current_turn = int(state.get("turns", 0))
        next_turn = state.get("next_interruption_turn")
        if not isinstance(next_turn, int):
            state["next_interruption_turn"] = current_turn + rng.randint(
                self.interruption_min_interval,
                self.interruption_max_interval,
            ) - 1
            next_turn = state["next_interruption_turn"]
        if current_turn < next_turn:
            return None
        interruption = dict(rng.choice(INTERRUPTION_MESSAGES))
        state["interruption_count"] = int(state.get("interruption_count", 0)) + 1
        state["next_interruption_turn"] = current_turn + rng.randint(
            self.interruption_min_interval,
            self.interruption_max_interval,
        )
        return interruption

    def _answer(self, lower: str, profile: dict[str, Any], state: dict[str, Any]) -> str:
        behavior = state["scenario"].get("user_behavior", "cooperative")
        available = set(profile.get("available_auth_methods", ["MOBILE", "EMAIL"]))
        required_auth_stop = bool(
            re.search(r"(required|must|need|necessary|can't|cannot|unable).{0,80}(phone|mobile|email|contact|auth|verification)", lower)
            or re.search(r"(phone|mobile|email|contact|auth|verification).{0,80}(required|must|need|necessary|can't|cannot|unable)", lower)
        )
        if required_auth_stop and behavior == "mobile_required_user_will_return":
            state["defer_ack_sent"] = True
            return "Okay, I will provide my phone number later."
        if required_auth_stop and behavior == "no_auth_contact":
            state["defer_ack_sent"] = True
            return "Okay, I will come back when I have a valid phone number or email."
        if "mobile or email" in lower or "phone or email" in lower:
            if not available:
                return "I don't have access to my phone number or email right now."
            if behavior == "mobile_required_user_will_return":
                state["defer_ack_sent"] = True
                return "My mobile number is required for this account, but I don't have it right now. I'll return when it's available."
            if behavior == "forgot_mobile_use_email":
                return "I forgot my mobile number. Can I use email instead?"
            if behavior == "forgot_email_use_mobile":
                return "I can't access my email. Can I use my mobile number instead?"
            return "I'd like to use mobile." if profile.get("contact_type") == "MOBILE" else "I'd like to use email."
        if "verification code" in lower or re.search(r"\b(code|otp)\b", lower):
            if behavior == "wrong_code_once" and not state.get("wrong_code_given"):
                state["wrong_code_given"] = True
                return "The code is 000000."
            return f"The verification code is {profile.get('verification_code', '123456')}."
        if "phone" in lower or "mobile" in lower:
            if behavior == "mobile_required_user_will_return":
                return "I don't have the mobile number with me right now. I can provide it later."
            if "MOBILE" not in available:
                if "EMAIL" in available:
                    return f"I don't remember my mobile number. My email is {profile.get('email')}."
                return "I don't remember my mobile number and cannot access it right now."
            if profile.get("contact_type") == "EMAIL" and behavior == "cooperative":
                return f"I prefer to use email. My email is {profile.get('contact')}."
            return f"My mobile number is {profile.get('mobile')} and the country code is {profile.get('area_code', '1')}."
        if "email" in lower:
            if behavior == "mobile_required_user_will_return":
                state["defer_ack_sent"] = True
                return "I can't use email for this account. My required mobile number is unavailable right now, so I'll return when I have it."
            if "EMAIL" not in available:
                if "MOBILE" in available:
                    return f"I can't access my email. My mobile number is {profile.get('mobile')} and country code {profile.get('area_code', '1')}."
                return "I don't have access to my email right now."
            if profile.get("contact_type") == "MOBILE" and behavior == "cooperative":
                return f"I prefer to use mobile. My mobile number is {profile.get('contact')} and country code {profile.get('area_code', '1')}."
            return f"My email is {profile.get('email')}."
        if (
            ("review" in lower or "extracted" in lower or "details are correct" in lower or "information is correct" in lower)
            and ("driver" in lower or "license" in lower or "licence" in lower or "document" in lower)
        ):
            return "Yes, the extracted document details are correct."
        if "objective" in lower and "risk" in lower:
            return (
                f"My investment objective is {profile.get('investment_objective')} "
                f"and my risk tolerance is {profile.get('risk_tolerance')}."
            )
        if "financial profile" in lower or ("annual income" in lower and "net worth" in lower):
            return (
                f"My annual income range is {profile.get('annual_income_usd_min')} to {profile.get('annual_income_usd_max')} USD, "
                f"my liquid net worth range is {profile.get('liquid_net_worth_usd_min')} to {profile.get('liquid_net_worth_usd_max')} USD, "
                f"and my total net worth range is {profile.get('total_net_worth_usd_min')} to {profile.get('total_net_worth_usd_max')} USD."
            )
        if "investment profile" in lower or "suitability" in lower or "finra" in lower:
            return (
                f"My investment experience is {profile.get('investment_experience')}, objective is {profile.get('investment_objective')}, "
                f"time horizon is {profile.get('time_horizon')}, risk tolerance is {profile.get('risk_tolerance')}, "
                f"and liquidity needs are {profile.get('liquidity_needs')}."
            )
        if profile.get("funding_source") == "Other" and (
            "other source" in lower
            or re.search(r"\b(describe|detail|explain)\b.{0,80}\b(source|funds|funding)\b", lower)
            or re.search(r"\b(source|funds|funding)\b.{0,80}\b(describe|detail|explain)\b", lower)
        ):
            return f"The details are: {profile.get('other_source')}."

        field_patterns = [
            ("account type", "account_type", "I want a {value} account."),
            ("crypto", "is_open_crypto", "For crypto access, {value}."),
            ("full name", "given_name", "My full name is {given} {family}."),
            ("first name", "gvie_name", "My first name is {value}."),
            ("given name", "given_name", "My given name is {value}."),
            ("last name", "family_name", "My last name is {value}."),
            ("family name", "family_name", "My family name is {value}."),
            ("date of birth", "date_of_birth", "My date of birth is {value}."),
            ("birth date", "date_of_birth", "My date of birth is {value}."),
            ("gender", "gender", "My gender is {value}."),
            ("tax id country", "tax_id_country", "My tax ID country is {value}."),
            ("tax identifier", "tax_id", "My tax identifier is {value} and the issuing country is {country}."),
            ("tax id", "tax_id", "My tax ID is {value} and the issuing country is {country}."),
            ("citizenship", "citizenship_country", "My citizenship country is {value}."),
            ("birth country", "birth_country", "My birth country is {value}."),
            ("country of birth", "birth_country", "My birth country is {value}."),
            ("address", "home_address", "My home address is {value}."),
            ("social security", "social_security_number", "My Social Security number is {value}."),
            ("ssn", "social_security_number", "My Social Security number is {value}."),
            ("marital", "marital_status", "My marital status is {value}."),
            ("dependents", "num_dependents", "I have {value} dependents."),
            ("permanent resident", "permanent_resident", "For permanent resident status, {value}."),
            ("passport number", "passport_no", "My passport number is {value} and it expires on {expiry}."),
            ("passport details", "passport_no", "My passport number is {value} and it expires on {expiry}."),
            ("passport", "passport_no", "My passport number is {value} and it expires on {expiry}."),
            ("visa type", "visa_type", "My visa type is {value}."),
            ("visa expiration", "visa_expiration_date", "My visa expires on {value}."),
            ("employment", "employment_status", "My employment status is {value}."),
            ("employer", "employer", "My employer is {value}."),
            ("position", "position_employed", "My position is {value}."),
            ("industry", "industry", "My industry is {value}."),
            ("funding", "funding_source", "My funding source is {value}."),
            ("source of funds", "funding_source", "My funding source is {value}."),
            ("source of my funds", "funding_source", "My funding source is {value}."),
            ("source of your funds", "funding_source", "My funding source is {value}."),
            ("source of income", "funding_source", "My funding source is {value}."),
            ("other source", "other_source", "The details are: {value}."),
            ("annual income", "annual_income_usd_min", "My annual income range is {minv} to {maxv} USD."),
            ("liquid net worth", "liquid_net_worth_usd_min", "My liquid net worth range is {minv} to {maxv} USD."),
            ("total net worth", "total_net_worth_usd_min", "My total net worth range is {minv} to {maxv} USD."),
            ("investment experience", "investment_experience", "My investment experience is {value}."),
            ("objective", "investment_objective", "My investment objective is {value}."),
            ("time horizon", "time_horizon", "My time horizon is {value}."),
            ("risk tolerance", "risk_tolerance", "My risk tolerance is {value}."),
            ("liquidity", "liquidity_needs", "My liquidity needs are {value}."),
            ("control person", "is_control_person", "{value}."),
            ("finra", "is_affiliated_exchangeorfinra", "{value}."),
            ("exchange", "is_affiliated_exchangeorfinra", "{value}."),
            ("politically exposed", "is_politically_exposed", "{value}."),
            ("trade authorization", "is_trade_authorization", "{value}."),
            ("identity", "is_identify", "{value}."),
            ("agreement", "agreements_accepted", "{value}, I accept the agreements."),
            ("disclosure", "is_control_person", "No to the control person, FINRA affiliation, political exposure, and trade authorization questions."),
        ]
        for pattern, field, template in field_patterns:
            if pattern in lower:
                return self._format_field_answer(field, template, profile)

        if "confirm" in lower or "submit" in lower or "review" in lower:
            return "Yes, I confirm the information is correct. Please submit the application."

        # Low-effort human behavior: give only a small bundle if the assistant is vague.
        return (
            f"My name is {profile.get('gvie_name')} {profile.get('family_name')}, "
            f"and I want a {profile.get('account_type')} account."
        )

    def _is_deferred_auth_response(self, state: dict[str, Any], response: str) -> bool:
        if state["scenario"].get("user_behavior") != "mobile_required_user_will_return":
            return False
        if not state.get("defer_ack_sent"):
            return False
        return bool(re.search(r"(later|return|come back|unavailable|don't have|do not have)", response, re.IGNORECASE))

    def _format_field_answer(self, field: str, template: str, profile: dict[str, Any]) -> str:
        if field.endswith("_min"):
            prefix = field[: -len("_min")]
            return template.format(minv=profile.get(f"{prefix}_min"), maxv=profile.get(f"{prefix}_max"))
        value = profile.get(field)
        if field == "given_name" and "{family}" in template:
            return template.format(given=profile.get("given_name") or profile.get("gvie_name"), family=profile.get("family_name"))
        if field == "tax_id":
            return template.format(value=value, country=profile.get("tax_id_country", profile.get("citizenship_country", "")))
        if field == "passport_no":
            return template.format(value=value, expiry=profile.get("passport_expire_date", "2032-12-31"))
        if isinstance(value, bool):
            value = "yes" if value else "no"
        if isinstance(value, dict):
            value = ", ".join(str(v) for v in value.values() if v)
        return template.format(value=value)


class OpenAICompatibleOnboardingUserInteraction(RuleBasedOnboardingUserInteraction):
    """A model-based simulator served from a local OpenAI-compatible endpoint.

    Use this after SFT-training a simulator. The model sees the hidden scenario
    and recent transcript, then returns the next user utterance. No external API
    is required; point ``endpoint`` at a local vLLM/SGLang/transformers server.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.endpoint = os.environ.get(
            "CUSTOMER_ENDPOINT",
            config.get("endpoint", "http://127.0.0.1:8000/v1/chat/completions"),
        )
        self.model = os.environ.get("CUSTOMER_MODEL", config.get("model", "onboarding-sim-user"))
        self.timeout = float(config.get("timeout", 60))
        self.temperature = float(config.get("temperature", 0.4))
        self.max_tokens = int(os.environ.get("CUSTOMER_MAX_TOKENS", config.get("max_tokens", 2048)))
        self.fallback_to_rule = bool(config.get("fallback_to_rule", True))
        self.model_retry_attempts = max(
            1,
            int(os.environ.get("CUSTOMER_MODEL_RETRY_ATTEMPTS", config.get("model_retry_attempts", 3))),
        )
        self.customer_enable_thinking = _env_bool(
            "CUSTOMER_ENABLE_THINKING",
            _boolish(config.get("enable_thinking"), False),
        )
        self.send_chat_template_kwargs = _env_bool(
            "CUSTOMER_SEND_CHAT_TEMPLATE_KWARGS",
            _boolish(config.get("send_chat_template_kwargs"), True),
        )
        self.system_prompt = config.get("system_prompt", CUSTOMER_SIMULATOR_SYSTEM_PROMPT)
        self.turn_log_path = os.environ.get("CUSTOMER_ROLLOUT_LOG", config.get("turn_log_path", ""))
        self.debug_csv_path = os.environ.get("DIGITAL_ONBOARDING_DEBUG_CSV", config.get("debug_csv_path", ""))

    async def generate_response(
        self, instance_id: str, messages: list[dict[str, Any]], **kwargs
    ) -> tuple[bool, str, float, dict]:
        state = self._instance_dict[instance_id]
        if kwargs.get("agent_data") is not None:
            state["agent_data"] = kwargs.get("agent_data")
        state["turns"] += 1

        transcript = "\n".join(_content_to_text(m.get("content", "")) for m in messages[-6:]).lower()
        lower = _last_assistant_text(messages)
        if _assistant_has_final_closing_signal(lower):
            return True, self.termination_signal, 0.5, {
                "sim_user_done": True,
                "reason": "assistant_final_closing_signal",
            }
        if '"submitted": true' in transcript or "application submitted successfully" in transcript:
            if not state.get("submitted_ack_sent"):
                state["submitted_ack_sent"] = True
                response = "Great, thank you."
                self._log_customer_turn(instance_id, messages, response, "rule_submit_ack")
                return False, response, 0.0, {"sim_user_backend": "rule_submit_ack", "reason": "submit_success_ack"}
            return True, self.termination_signal, 1.0, {"sim_user_done": True}
        if state.get("defer_ack_sent") and re.search(
            r"(come back|return|later|cannot continue|can't continue|unable to continue|need.{0,40}(mobile|phone|authentication|verification))",
            lower,
        ):
            return True, self.termination_signal, 0.5, {"sim_user_done": True, "reason": "auth_deferred"}
        if self.max_user_turns and state["turns"] >= self.max_user_turns:
            return True, self.termination_signal, -0.2, {"sim_user_done": False, "reason": "max_user_turns"}

        scenario = state["scenario"]
        profile = scenario.get("profile", {})
        behavior = scenario.get("user_behavior", "cooperative")
        if behavior == "mobile_required_user_will_return" and re.search(
            r"(email|mobile|phone|contact|auth|verification|code|otp)", lower
        ):
            response = await self._answer_async(lower, profile, state)
            self._log_customer_turn(instance_id, messages, response, "rule_auth_guard")
            if self._is_deferred_auth_response(state, response):
                return True, response, 0.5, {"sim_user_done": True, "reason": "auth_deferred", "sim_user_backend": "rule_auth_guard"}
            return False, response, 0.0, {"sim_user_backend": "rule_auth_guard"}
        if assistant_requests_document_upload(lower):
            response = await self._answer_async(lower, profile, state)
            self._log_customer_turn(instance_id, messages, response, "rule_upload_guard")
            return False, response, 0.0, {"sim_user_backend": "rule_upload_guard"}
        interruption = self._maybe_interruption(state, lower)
        if interruption:
            response = interruption["text"]
            backend = f"interruption_{interruption['category']}"
            self._log_customer_turn(instance_id, messages, response, backend)
            return False, response, 0.0, {
                "sim_user_backend": "interruption",
                "interruption": True,
                "interruption_category": interruption["category"],
                "interruption_count": state.get("interruption_count", 0),
            }
        if _should_use_rule_field_guard(lower):
            response = await self._answer_async(lower, profile, state)
            self._log_customer_turn(instance_id, messages, response, "rule_field_guard")
            if self._is_deferred_auth_response(state, response):
                return True, response, 0.5, {
                    "sim_user_done": True,
                    "reason": "auth_deferred",
                    "sim_user_backend": "rule_field_guard",
                }
            return False, response, 0.0, {"sim_user_backend": "rule_field_guard"}

        prompt = self._build_model_prompt(state["scenario"], messages)
        loop = asyncio.get_running_loop()
        last_error = ""
        last_raw_response = ""
        for attempt in range(1, self.model_retry_attempts + 1):
            try:
                text = await loop.run_in_executor(None, lambda: self._call_local_model(prompt))
                last_raw_response = text
                response = self._extract_response(text)
                if not response:
                    raise ValueError("local simulator returned an empty response")
                self._log_customer_turn(
                    instance_id,
                    messages,
                    response,
                    "local_model",
                    prompt=prompt,
                    raw_response=text,
                )
                return False, response, 0.0, {"sim_user_backend": "local_model", "sim_user_attempt": attempt}
            except Exception as exc:
                last_error = str(exc)

        if self.fallback_to_rule:
            response = await self._answer_async(lower, profile, state)
            if response:
                self._log_customer_turn(
                    instance_id,
                    messages,
                    response,
                    "rule_fallback_after_local_model_error",
                    error=last_error,
                    prompt=prompt,
                    raw_response=last_raw_response,
                )
                if self._is_deferred_auth_response(state, response):
                    return True, response, 0.5, {
                        "sim_user_done": True,
                        "reason": "auth_deferred",
                        "sim_user_backend": "rule_fallback_after_local_model_error",
                    }
                return False, response, 0.0, {
                    "sim_user_backend": "rule_fallback_after_local_model_error",
                    "sim_user_error": last_error,
                    "sim_user_attempts": self.model_retry_attempts,
                }

        self._log_customer_turn(
            instance_id,
            messages,
            self.termination_signal,
            "local_model_error",
            error=last_error,
            prompt=prompt,
            raw_response=last_raw_response,
        )
        return True, self.termination_signal, -0.5, {
            "sim_user_error": last_error,
            "sim_user_backend": "local_model_error",
            "sim_user_attempts": self.model_retry_attempts,
        }

    def _build_model_prompt(self, scenario: dict[str, Any], messages: list[dict[str, Any]]) -> str:
        recent = []
        for message in messages[-10:]:
            line = _visible_chat_line(message)
            if line:
                recent.append(line)
        scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
        profile = scenario.get("profile", {})
        preferred_contact_type = profile.get("contact_type", "MOBILE")
        preferred_contact = profile.get("contact")
        available_auth = profile.get("available_auth_methods", ["MOBILE", "EMAIL"])
        required_auth = profile.get("required_auth_methods", [])
        return (
            "You are simulating the customer in a brokerage account-opening conversation.\n"
            "The service agent is trying to help you open the account. You should answer as the customer only.\n"
            "Use the private customer information below when the service asks for it. Do not reveal that it is hidden.\n"
            "When asked for login contact, use the profile's contact_type/contact and available_auth_methods exactly. "
            "Do not choose an unavailable authentication method.\n"
            "For U.S.-market profiles, use U.S. mobile area code 1 unless the profile explicitly says otherwise. "
            "Stay consistent with residency_category: US_CITIZEN, US_PERMANENT_RESIDENT, or US_VISA.\n"
            "When asked to upload, show, capture, or provide a document image, perform the upload action. "
            f"In this simulator, the upload action is represented by a CAPTURE_RESULT response containing the exact marker {UPLOADED_IMAGE_MARKER}. "
            "Do not answer with a text-only claim like 'I uploaded it', and do not describe or simulate the image in prose.\n"
            "When the service shows extracted document fields and asks you to review or confirm them, do not upload again. "
            "Confirm briefly if they match the hidden profile, or provide the correction if a value is wrong. "
            "Do not ask for final application submission during a document-review confirmation turn.\n"
            "Upload one document image per response. U.S. citizens provide driver's-license front and back as two separate uploads; "
            "U.S. permanent residents provide passport plus green card/permanent resident card; "
            "U.S. visa holders provide passport plus one visa page/image.\n"
            "For EMPLOYED or SELF_EMPLOYED, provide employer/position/years/industry when asked. "
            "For UNEMPLOYED, RETIRED, or STUDENT, provide funding_source; if it is Other, provide other_source when asked.\n"
            f"Preferred login method: {preferred_contact_type}; preferred contact: {preferred_contact}; "
            f"available auth methods: {available_auth}; required auth methods: {required_auth}.\n"
            "Return exactly one compact JSON object and no other text: {\"response\": \"...\"}\n\n"
            "Private customer scenario JSON:\n"
            f"{scenario_json}\n\n"
            "Visible chat history. `service` is the onboarding agent; `user` is you, the customer:\n"
            + "\n".join(recent)
            + '\n\nNow produce the next customer reply as JSON only.'
        )

    def _call_local_model(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        if self.send_chat_template_kwargs:
            payload["chat_template_kwargs"] = {"enable_thinking": self.customer_enable_thinking}

        def post(request_payload: dict[str, Any]) -> dict[str, Any]:
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(request_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            data = post(payload)
        except urllib.error.HTTPError as exc:
            if self.send_chat_template_kwargs and exc.code in {400, 422}:
                retry_payload = dict(payload)
                retry_payload.pop("chat_template_kwargs", None)
                try:
                    data = post(retry_payload)
                    self.send_chat_template_kwargs = False
                    return data["choices"][0]["message"]["content"]
                except urllib.error.HTTPError as retry_exc:
                    body = retry_exc.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"local simulator HTTP {retry_exc.code}: {body}") from retry_exc
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"local simulator HTTP {exc.code}: {body}") from exc
        return data["choices"][0]["message"]["content"]

    def _extract_response(self, text: str) -> str:
        text = text.strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            clean_text = _strip_model_artifacts(text)
            parsed = _first_json_object(clean_text)
            if parsed is not None:
                pass
            else:
                if not clean_text or _looks_like_customer_meta_response(clean_text):
                    return ""
                parsed = {"response": clean_text}
        if isinstance(parsed, dict):
            response = parsed.get("response") or parsed.get("utterance") or parsed.get("content")
            cleaned_response = _strip_model_artifacts(str(response)).strip() if response else ""
            return "" if _looks_like_customer_meta_response(cleaned_response) else cleaned_response
        cleaned = _strip_model_artifacts(str(parsed)).strip()
        return "" if _looks_like_customer_meta_response(cleaned) else cleaned

    def _log_customer_turn(
        self,
        instance_id: str,
        messages: list[dict[str, Any]],
        response: str,
        backend: str,
        error: str | None = None,
        prompt: str = "",
        raw_response: str = "",
    ) -> None:
        state = self._instance_dict.get(instance_id, {})
        scenario = state.get("scenario", {})
        recent = [
            {"role": message.get("role", "unknown"), "content": _content_to_text(message.get("content", ""))}
            for message in messages[-10:]
        ]
        row = {
            "time": datetime.now(timezone.utc).isoformat(),
            "instance_id": instance_id,
            "backend": backend,
            "scenario_id": scenario.get("scenario_id"),
            "scenario_json": json.dumps(scenario, ensure_ascii=False, sort_keys=True),
            "assistant_request": _last_assistant_text(messages),
            "customer_response": response,
            "prompt": prompt,
            "raw_response": raw_response,
            "recent_messages": recent,
        }
        if error:
            row["error"] = error[:512]
        if self.turn_log_path:
            path = Path(self.turn_log_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        append_debug_csv(
            self.debug_csv_path,
            {
                "request_id": instance_id,
                "scenario_id": scenario.get("scenario_id"),
                "turn": state.get("turns"),
                "event_type": "USER_SIMULATOR",
                "role": "user",
                "backend": backend,
                "model": self.model,
                "endpoint": self.endpoint,
                "content": raw_response or response,
                "prompt": prompt,
                "response": response,
                "raw_response": raw_response,
                "metadata": {
                    "assistant_request": _last_assistant_text(messages),
                    "error": error[:512] if error else "",
                    "recent_messages": recent,
                },
            },
        )
