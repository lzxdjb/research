"""Interactive web chat for the digital onboarding service model.

This module serves two roles:

1. Proxy user turns to an OpenAI-compatible service-model endpoint.
2. Execute the same onboarding tools used by the VERL multi-turn rollout loop.

The browser handles speech-to-text with the Web Speech API. That keeps the
server lightweight and avoids adding an audio transcription dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from recipe.digital_onboarding.choice_hints import choice_hint_widgets
from recipe.digital_onboarding.disclosure_terms import append_terms_if_needed
from recipe.digital_onboarding.image_verification import verify_image_upload
from recipe.digital_onboarding.prompts import SERVICE_SYSTEM_PROMPT
from recipe.digital_onboarding.real_bank import (
    prepare_real_bank_scenario,
    real_bank_fake_upload_wrapper_enabled,
    real_bank_upload_thumbnail_enabled,
)
from recipe.digital_onboarding.scenario import make_scenario
from recipe.digital_onboarding.tools import (
    MARKER,
    UPLOADED_IMAGE_MARKER,
    UPLOAD_RETRY_MESSAGE,
    assistant_requests_document_upload,
    clear_user_upload_in_progress,
    contains_verified_uploaded_image,
    document_upload_pending,
    mark_document_upload_requested,
    register_verified_upload,
    uploaded_image_user_content,
    uploaded_image_required,
)
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config


RECIPE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = RECIPE_DIR.parents[1]
STATIC_DIR = RECIPE_DIR / "static"
TOOL_CONFIG_PATH = RECIPE_DIR / "config" / "tool_config.yaml"

DEFAULT_ENDPOINT = "http://127.0.0.1:8010/v1/chat/completions"
DEFAULT_MODEL = str(PROJECT_ROOT / "checkpoints/formal_train/global_step_20/actor/huggingface")

TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL | re.IGNORECASE)
THINK_RE = re.compile(r"<think>.*?</think>|<think>.*$", re.DOTALL | re.IGNORECASE)
IM_TOKEN_RE = re.compile(r"<\|im_(?:start|end)\|>")

WIDGET_TOOLS = {
    "present_options",
    "present_date_input",
    "present_phone_input",
    "present_email_input",
    "present_drivers_license_review",
    "present_disclosure",
    "present_country_select",
    "present_address_input",
    "present_us_address_input",
    "present_ssn_input",
    "present_tax_id_input",
    "present_personal_info_input",
    "present_employment_input",
    "present_agreements",
    "present_progress_indicator",
    "present_passport_input",
    "present_visa_input",
    "present_green_card_input",
    "present_id_card_input",
    "present_address_proof_upload",
    "present_financial_range_input",
    "present_investment_profile_input",
}

DOCUMENT_WIDGET_DOC_TYPES = {
    "present_passport_input": "passport",
    "present_visa_input": "visa",
    "present_green_card_input": "permanent_resident_card",
    "present_id_card_input": "id_card",
    "present_address_proof_upload": "bank_statement",
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _service_endpoint() -> str:
    return os.environ.get("DIGITAL_ONBOARDING_SERVICE_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def _service_model() -> str:
    return os.environ.get("DIGITAL_ONBOARDING_SERVICE_MODEL", DEFAULT_MODEL)


def _tool_backend() -> str:
    return os.environ.get("DIGITAL_ONBOARDING_TOOL_BACKEND", "real_bank")


def _real_upload_enabled() -> bool:
    return _tool_backend().lower() in {"real_bank", "bank", "open_account", "open-account"} and not real_bank_fake_upload_wrapper_enabled()


def _max_tokens() -> int:
    return int(os.environ.get("INTERACTIVE_MAX_TOKENS", os.environ.get("MAX_RESPONSE_LENGTH", "4096")))


def _min_response_tokens() -> int:
    return int(os.environ.get("INTERACTIVE_MIN_RESPONSE_TOKENS", "512"))


def _context_window() -> int:
    return int(os.environ.get("INTERACTIVE_CONTEXT_WINDOW", os.environ.get("MAX_MODEL_LEN", "32768")))


def _context_margin_tokens() -> int:
    return int(os.environ.get("INTERACTIVE_CONTEXT_MARGIN_TOKENS", "256"))


def _temperature() -> float:
    return float(os.environ.get("INTERACTIVE_TEMPERATURE", "0.2"))


def _max_auto_steps() -> int:
    return int(os.environ.get("INTERACTIVE_MAX_AUTO_STEPS", "12"))


def _max_tool_calls_per_step() -> int:
    return int(os.environ.get("INTERACTIVE_MAX_TOOL_CALLS_PER_STEP", "1"))


def _timeout_seconds() -> int:
    return int(os.environ.get("INTERACTIVE_REQUEST_TIMEOUT", "300"))


def _send_openai_tools() -> bool:
    return _env_bool("INTERACTIVE_SEND_OPENAI_TOOLS", False)


def _tools() -> dict[str, Any]:
    if not hasattr(_tools, "_cache"):
        with contextlib.redirect_stdout(io.StringIO()):
            tool_list = initialize_tools_from_config(os.fspath(TOOL_CONFIG_PATH))
        setattr(_tools, "_cache", {tool.name: tool for tool in tool_list})
    return getattr(_tools, "_cache")


def _tool_schemas() -> list[dict[str, Any]]:
    if not hasattr(_tool_schemas, "_cache"):
        setattr(
            _tool_schemas,
            "_cache",
            [tool.tool_schema.model_dump(exclude_unset=True, exclude_none=True) for tool in _tools().values()],
        )
    return getattr(_tool_schemas, "_cache")


def _tokenizer():
    if hasattr(_tokenizer, "_cache"):
        return getattr(_tokenizer, "_cache")
    tokenizer = None
    path = os.environ.get("INTERACTIVE_TOKENIZER_PATH") or _service_model()
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(path, trust_remote_code=True)
    except Exception:
        tokenizer = None
    setattr(_tokenizer, "_cache", tokenizer)
    return tokenizer


def _balance_truncated_json(text: str) -> str:
    stripped = text.strip()
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in stripped:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
            else:
                return stripped
    if in_string:
        return stripped
    return stripped + "".join(reversed(stack))


def _loads_tool_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = json.loads(_balance_truncated_json(text))
    if not isinstance(value, dict):
        raise ValueError("tool call JSON must be an object")
    return value


def _coerce_arguments(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("tool arguments must be a JSON object")


def _format_raw_tool_call(name: str, arguments: dict[str, Any]) -> str:
    payload = {"name": name, "arguments": arguments}
    return f"<tool_call>{json.dumps(payload, ensure_ascii=False)}</tool_call>"


def _manual_tool_prompt() -> str:
    tool_lines = "\n".join(json.dumps(tool, ensure_ascii=False) for tool in _tool_schemas())
    return (
        "\n\n# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        f"<tools>\n{tool_lines}\n</tools>\n\n"
        "For each function call, return a json object with function name and arguments within "
        "<tool_call></tool_call> XML tags:\n"
        "<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>"
    )


def _messages_with_manual_tools(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied = [dict(message) for message in messages]
    if copied and copied[0].get("role") == "system":
        copied[0]["content"] = str(copied[0].get("content") or "") + _manual_tool_prompt()
    else:
        copied.insert(0, {"role": "system", "content": SERVICE_SYSTEM_PROMPT + _manual_tool_prompt()})
    return copied


def _messages_for_prompt(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_messages: list[dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        if copied.get("role") == "tool":
            copied["content"] = _compact_tool_response_text(str(copied.get("content") or ""))
        prompt_messages.append(copied)
    return prompt_messages


def _token_estimate_content(content: Any) -> Any:
    if isinstance(content, list):
        parts: list[Any] = []
        for part in content:
            if not isinstance(part, dict):
                parts.append(part)
                continue
            copied = dict(part)
            part_type = str(copied.get("type") or "").lower()
            if part_type == "image_url" and isinstance(copied.get("image_url"), dict):
                image_url = dict(copied["image_url"])
                image_url["url"] = "<uploaded-image>"
                copied["image_url"] = image_url
            elif part_type in {"image", "input_image"} or any(key in copied for key in ("image", "input_image")):
                copied = {"type": part_type or "image", "image": "<uploaded-image>"}
            parts.append(copied)
        return parts
    return content


def _messages_for_token_estimate(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    estimate_messages: list[dict[str, Any]] = []
    for message in messages:
        copied = dict(message)
        copied["content"] = _token_estimate_content(copied.get("content"))
        estimate_messages.append(copied)
    return estimate_messages


def _estimate_prompt_tokens(messages: list[dict[str, Any]], *, send_tools: bool) -> int:
    prompt_messages = messages if send_tools else _messages_with_manual_tools(messages)
    estimate_messages = _messages_for_token_estimate(prompt_messages)
    tokenizer = _tokenizer()
    if tokenizer is not None:
        try:
            tokenized = tokenizer.apply_chat_template(
                estimate_messages,
                tokenize=True,
                add_generation_prompt=True,
                tools=_tool_schemas() if send_tools else None,
                enable_thinking=_env_bool("ENABLE_THINKING", True),
            )
            if tokenized and isinstance(tokenized[0], list):
                tokenized = tokenized[0]
            return len(tokenized)
        except Exception:
            pass
    rendered = json.dumps(estimate_messages, ensure_ascii=False)
    if send_tools:
        rendered += json.dumps(_tool_schemas(), ensure_ascii=False)
    return max(1, len(rendered) // 3)


def _fit_messages_and_max_tokens(
    messages: list[dict[str, Any]],
    *,
    send_tools: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    context_window = _context_window()
    margin = _context_margin_tokens()
    requested_max = _max_tokens()
    min_response = min(_min_response_tokens(), requested_max)

    prompt_messages = _messages_for_prompt(messages)
    system = prompt_messages[:1] if prompt_messages and prompt_messages[0].get("role") == "system" else []
    tail = prompt_messages[len(system) :]
    notice = {
        "role": "system",
        "content": (
            "Some older conversation turns were omitted to fit the model context window. "
            "The onboarding tool state is still preserved; call query_progress when current status is needed."
        ),
    }

    candidate = prompt_messages
    prompt_tokens = _estimate_prompt_tokens(candidate, send_tools=send_tools)
    while prompt_tokens + min_response + margin > context_window and len(tail) > 1:
        tail = tail[1:]
        candidate = [*system, notice, *tail]
        prompt_tokens = _estimate_prompt_tokens(candidate, send_tools=send_tools)

    available = context_window - prompt_tokens - margin
    max_tokens = max(64, min(requested_max, available))
    return candidate, max_tokens, prompt_tokens


def _extract_tool_calls(content: str, openai_tool_calls: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for tool_call in openai_tool_calls or []:
        function = tool_call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        try:
            arguments = _coerce_arguments(function.get("arguments"))
        except Exception:
            arguments = {}
        calls.append({"name": name, "arguments": arguments})

    if calls:
        return calls

    for match in TOOL_CALL_RE.findall(content or ""):
        try:
            data = _loads_tool_json(match)
            name = data["name"]
            arguments = _coerce_arguments(data.get("arguments", {}))
        except Exception:
            continue
        calls.append({"name": name, "arguments": arguments})
    return calls


def _strip_model_artifacts(content: str) -> str:
    text = THINK_RE.sub("", content or "")
    text = TOOL_CALL_RE.sub("", text)
    text = IM_TOKEN_RE.sub("", text)
    text = re.sub(r"\bassistant\s*$", "", text.strip(), flags=re.IGNORECASE)
    return text.strip()


def _parse_tool_result(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    stripped = text.strip()
    if not stripped.startswith(MARKER):
        return {"message": stripped}
    payload = stripped[len(MARKER) :].strip()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {"message": stripped}
    return value if isinstance(value, dict) else {"message": str(value)}


def _compact_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    keep = [
        "tool",
        "status",
        "message",
        "error_code",
        "missing_fields",
        "collected_fields",
        "accepted_fields",
        "rejected_fields",
        "file_id",
        "min_file_id",
        "doc_type",
        "document_field",
        "file_url",
        "extracted_fields",
        "image_available",
        "document_image_in_context",
        "model_should_extract_from_image",
        "review_required",
        "extraction_source",
        "mobile_bound",
        "email_bound",
        "bank_submit_success",
    ]
    compact = {key: result.get(key) for key in keep if key in result}
    if isinstance(result.get("state"), dict):
        compact["state"] = _public_state(result["state"])
    return compact


def _compact_tool_response_text(text: str | None) -> str:
    result = _parse_tool_result(text)
    if not result:
        return text or ""
    compact = _compact_tool_result(result)
    if not compact:
        return text or ""
    return f"{MARKER} {json.dumps(compact, ensure_ascii=False, sort_keys=True)}"


def _public_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    keys = [
        "backend",
        "trajectory_id",
        "authenticated",
        "verification_sent",
        "submitted",
        "submission_attempted",
        "missing_fields",
        "collected_fields",
        "document_captured",
        "document_upload_verified",
        "document_extracted",
        "awaiting_document_upload",
        "expected_doc_type",
        "expected_document_field",
        "document_review_pending",
        "document_review_confirmed",
        "document_review_rejected",
        "used_widgets",
        "bank_status",
        "bank_missing_fields",
        "bank_collected_fields",
        "bank_completion_percentage",
        "bank_query_ok",
        "bank_submit_success",
    ]
    return {key: state.get(key) for key in keys if key in state}


def _public_profile(scenario: dict[str, Any]) -> dict[str, Any]:
    profile = scenario.get("profile") or {}
    return {
        "branch": scenario.get("branch"),
        "residency_category": scenario.get("residency_category"),
        "contact_type": profile.get("contact_type"),
        "contact": profile.get("contact"),
        "mobile": profile.get("mobile"),
        "email": profile.get("email"),
        "area_code": profile.get("area_code"),
        "verification_code": profile.get("verification_code"),
        "given_name": profile.get("given_name", profile.get("gvie_name")),
        "family_name": profile.get("family_name"),
        "date_of_birth": profile.get("date_of_birth"),
        "citizenship_country": profile.get("citizenship_country"),
        "birth_country": profile.get("birth_country"),
        "permanent_resident": profile.get("permanent_resident"),
        "account_type": profile.get("account_type"),
        "home_address": profile.get("home_address"),
        "employment_status": profile.get("employment_status"),
        "funding_source": profile.get("funding_source"),
        "investment_objective": profile.get("investment_objective"),
        "risk_tolerance": profile.get("risk_tolerance"),
        "agreements_accepted": profile.get("agreements_accepted"),
    }


def _widget_from_tool(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    if name not in WIDGET_TOOLS:
        return None
    widget = {
        "kind": "generic",
        "tool": name,
        "question": arguments.get("question") or result.get("message") or "",
        "arguments": arguments,
    }
    if name == "present_options":
        widget.update(
            {
                "kind": "options",
                "options": arguments.get("options") or [],
                "choice_type": arguments.get("type") or "single",
                "layout": arguments.get("layout") or "buttons",
            }
        )
    elif name == "present_date_input":
        widget.update({"kind": "date", "format": arguments.get("format") or "date"})
    elif name == "present_phone_input":
        widget.update({"kind": "phone"})
    elif name == "present_email_input":
        widget.update({"kind": "email"})
    elif name == "present_disclosure":
        widget.update({"kind": "disclosure", "questions": arguments.get("questions") or []})
    elif name == "present_drivers_license_review":
        widget.update({"kind": "review", "fields": arguments.get("fields") or {}})
    elif name == "present_progress_indicator":
        widget.update({"kind": "progress", "status": arguments.get("status"), "percentage": arguments.get("percentage"), "sections": arguments.get("sections") or []})
    elif name in DOCUMENT_WIDGET_DOC_TYPES:
        widget.update({"kind": "document", "doc_type": DOCUMENT_WIDGET_DOC_TYPES[name]})
    else:
        widget.update({"kind": "generic"})
    return widget


@dataclass
class InteractiveAgentData:
    request_id: str
    messages: list[dict[str, Any]]
    tools_kwargs: dict[str, Any]
    extra_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class InteractiveSession:
    session_id: str
    scenario: dict[str, Any]
    messages: list[dict[str, Any]]
    agent_data: InteractiveAgentData
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None


class UploadRequest(BaseModel):
    filename: str = Field(..., min_length=1)
    file_data: str = Field(..., min_length=1)
    session_id: str | None = None
    doc_type: str = "drivers_license_front"
    message: str | None = None


class ResetRequest(BaseModel):
    session_id: str | None = None
    scenario_index: int | None = None


SESSIONS: dict[str, InteractiveSession] = {}


def _new_session(session_id: str | None = None, scenario_index: int | None = None) -> InteractiveSession:
    session_id = session_id or uuid4().hex
    index = scenario_index if scenario_index is not None else int(os.environ.get("INTERACTIVE_SCENARIO_INDEX", "0"))
    behavior_mode = os.environ.get("INTERACTIVE_BEHAVIOR_MODE", os.environ.get("BEHAVIOR_MODE", "phase1"))
    branch_mode = os.environ.get("INTERACTIVE_BRANCH_MODE", os.environ.get("DIGITAL_ONBOARDING_BRANCH_MODE", "us_market"))
    scenario = make_scenario(
        index,
        split="interactive",
        seed=int(os.environ.get("INTERACTIVE_SCENARIO_SEED", "17")),
        behavior_mode=behavior_mode,
        branch_mode=branch_mode,
    )
    if _tool_backend().lower() in {"real_bank", "bank", "open_account", "open-account"}:
        scenario = prepare_real_bank_scenario(scenario, request_id=session_id)

    messages = [{"role": "system", "content": SERVICE_SYSTEM_PROMPT}]
    tools_kwargs = {
        "__onboarding_scenario_json__": json.dumps(scenario, ensure_ascii=False, sort_keys=True),
        "__onboarding_tool_backend__": _tool_backend(),
        "tool_backend": _tool_backend(),
    }
    agent_data = InteractiveAgentData(request_id=session_id, messages=messages, tools_kwargs=tools_kwargs)
    session = InteractiveSession(session_id=session_id, scenario=scenario, messages=messages, agent_data=agent_data)
    SESSIONS[session_id] = session
    return session


def _get_session(session_id: str | None) -> InteractiveSession:
    if not session_id:
        return _new_session()
    return SESSIONS.get(session_id) or _new_session(session_id=session_id)


def _chat_payload(
    messages: list[dict[str, Any]],
    *,
    send_tools: bool,
    send_chat_template_kwargs: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    fitted_messages, max_tokens, prompt_tokens = _fit_messages_and_max_tokens(messages, send_tools=send_tools)
    payload: dict[str, Any] = {
        "model": _service_model(),
        "messages": fitted_messages if send_tools else _messages_with_manual_tools(fitted_messages),
        "temperature": _temperature(),
        "max_tokens": max_tokens,
    }
    if send_tools:
        payload["tools"] = _tool_schemas()
    if send_chat_template_kwargs:
        payload["chat_template_kwargs"] = {"enable_thinking": _env_bool("ENABLE_THINKING", True)}
    metadata = {
        "send_tools": send_tools,
        "max_tokens": max_tokens,
        "prompt_tokens_estimate": prompt_tokens,
        "messages_sent": len(fitted_messages),
        "messages_total": len(messages),
        "context_window": _context_window(),
    }
    return payload, metadata


def _response_error(response: requests.Response) -> str:
    text = response.text
    if len(text) > 2000:
        text = text[:2000] + "...(truncated)"
    return f"{response.status_code} {response.reason}: {text}"


def _post_chat_completion(messages: list[dict[str, Any]]) -> dict[str, Any]:
    endpoint = _service_endpoint()
    attempts = []
    if _send_openai_tools():
        attempts.append(("openai_tools_with_chat_template_kwargs", True, _env_bool("INTERACTIVE_SEND_CHAT_TEMPLATE_KWARGS", True)))
        attempts.append(("openai_tools", True, False))
    attempts.append(("manual_tool_prompt", False, False))

    errors: list[str] = []
    data: dict[str, Any] | None = None
    selected_metadata: dict[str, Any] = {}
    for label, send_tools, send_chat_template_kwargs in attempts:
        payload, metadata = _chat_payload(
            messages,
            send_tools=send_tools,
            send_chat_template_kwargs=send_chat_template_kwargs,
        )
        try:
            response = requests.post(endpoint, json=payload, timeout=_timeout_seconds())
            if response.status_code >= 400:
                errors.append(f"{label}: {_response_error(response)}")
                continue
            data = response.json()
            selected_metadata = {"request_shape": label, **metadata}
            break
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            continue
    if data is None:
        detail = "Model endpoint request failed. Attempts:\n" + "\n".join(errors)
        raise HTTPException(status_code=502, detail=detail)
    if not isinstance(data, dict) or not data.get("choices"):
        raise HTTPException(status_code=502, detail="Model endpoint returned no choices.")
    data["_interactive_metadata"] = selected_metadata
    return data


async def _generate_assistant(session: InteractiveSession) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    data = await asyncio.to_thread(_post_chat_completion, session.messages)
    message = data["choices"][0].get("message") or {}
    content = message.get("content") or ""
    tool_calls = _extract_tool_calls(content, message.get("tool_calls"))
    if tool_calls and not TOOL_CALL_RE.search(content):
        content = "\n".join([content, *[_format_raw_tool_call(c["name"], c["arguments"]) for c in tool_calls]]).strip()
    return content, tool_calls, {
        "usage": data.get("usage"),
        "finish_reason": data["choices"][0].get("finish_reason"),
        **data.get("_interactive_metadata", {}),
    }


async def _execute_tool(session: InteractiveSession, call: dict[str, Any]) -> dict[str, Any]:
    name = str(call.get("name") or "")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    tool = _tools().get(name)
    if tool is None:
        result = {
            "tool": name,
            "status": "error",
            "message": f"Unsupported tool: {name}",
            "error_code": "unknown_tool",
            "state": _public_state(session.agent_data.extra_fields.get("onboarding_state")),
        }
        raw_response = f"{MARKER} {json.dumps(result, ensure_ascii=False, sort_keys=True)}"
        return {"tool": name, "arguments": arguments, "raw_response": raw_response, "result": result}

    instance_id = None
    tool_response = ToolResponse(text="")
    try:
        instance_id, _ = await tool.create(create_kwargs={})
        tool_response, reward, metrics = await tool.execute(instance_id, arguments, agent_data=session.agent_data)
    except Exception as exc:
        result = {
            "tool": name,
            "status": "error",
            "message": f"Tool execution failed: {exc}",
            "error_code": "tool_execution_failed",
            "state": _public_state(session.agent_data.extra_fields.get("onboarding_state")),
        }
        raw_response = f"{MARKER} {json.dumps(result, ensure_ascii=False, sort_keys=True)}"
        return {"tool": name, "arguments": arguments, "raw_response": raw_response, "result": result}
    finally:
        if instance_id is not None:
            try:
                await tool.release(instance_id)
            except Exception:
                pass

    raw_response = tool_response.text or ""
    result = _parse_tool_result(raw_response)
    event = {
        "tool": name,
        "arguments": arguments,
        "raw_response": raw_response,
        "result": result,
        "reward": reward,
        "metrics": metrics,
        "widget": _widget_from_tool(name, arguments, result),
    }
    return event


def _append_tool_message(session: InteractiveSession, event: dict[str, Any]) -> None:
    session.messages.append({"role": "tool", "content": _compact_tool_response_text(event.get("raw_response") or "")})


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


def _last_user_text(session: InteractiveSession) -> str:
    for message in reversed(session.messages):
        if message.get("role") == "user":
            return _message_text(message)
    return ""


def _last_assistant_text(session: InteractiveSession) -> str:
    for message in reversed(session.messages):
        if message.get("role") == "assistant":
            return _strip_model_artifacts(_message_text(message)).lower()
    return ""


def _document_type_collected(state: dict[str, Any], doc_type: str) -> bool:
    captured = state.get("captured_documents") or {}
    collected = state.get("collected_fields") or {}
    doc_type = (doc_type or "").lower()
    if doc_type in {"drivers_license_front", "drivers_licence_front"}:
        driver = captured.get("drivers_license") if isinstance(captured, dict) else None
        return bool(
            (isinstance(driver, dict) and isinstance(driver.get("front"), dict))
            or "drivers_licence_front" in collected
            or "drivers_license_front" in collected
        )
    if doc_type in {"drivers_license_back", "drivers_licence_back"}:
        driver = captured.get("drivers_license") if isinstance(captured, dict) else None
        return bool(
            (isinstance(driver, dict) and isinstance(driver.get("back"), dict))
            or "drivers_licence_back" in collected
            or "drivers_license_back" in collected
        )
    field_map = {
        "passport": "passport_photo",
        "visa": "visa",
        "permanent_resident_card": "card_photo",
        "green_card": "card_photo",
        "id_card": "card_photo",
        "bank_statement": "address_proof",
        "utility_bill": "address_proof",
        "credit_card_statement": "address_proof",
    }
    field = field_map.get(doc_type, doc_type)
    return bool(field in captured or field in collected or doc_type in collected)


def _infer_doc_type_from_text(text: str, state: dict[str, Any] | None = None) -> str:
    lower = (text or "").lower()
    state = state if isinstance(state, dict) else {}
    mentions_passport = "passport" in lower
    mentions_visa = "visa" in lower
    mentions_green_card = "green card" in lower or "permanent resident" in lower
    mentions_driver = "driver" in lower or "license" in lower or "licence" in lower
    if "address proof" in lower or "utility bill" in lower or "bank statement" in lower or "credit card statement" in lower:
        return "bank_statement"
    if mentions_driver:
        if "back" in lower:
            return "drivers_license_back"
        if "front" in lower:
            return "drivers_license_front"
        if not _document_type_collected(state, "drivers_license_front"):
            return "drivers_license_front"
        if not _document_type_collected(state, "drivers_license_back"):
            return "drivers_license_back"
        return "drivers_license_front"
    if mentions_passport and (mentions_visa or mentions_green_card):
        if not _document_type_collected(state, "passport"):
            return "passport"
        return "permanent_resident_card" if mentions_green_card else "visa"
    if mentions_visa:
        return "visa"
    if mentions_green_card:
        return "permanent_resident_card"
    if "id card" in lower or "government" in lower:
        return "id_card"
    if mentions_passport:
        return "passport"
    return "drivers_license_front"


def _expected_upload_doc_type(session: InteractiveSession, requested_doc_type: str | None = None) -> str:
    requested = (requested_doc_type or "").strip() or "drivers_license_front"
    state = session.agent_data.extra_fields.get("onboarding_state")
    state = state if isinstance(state, dict) else {}
    gate = session.agent_data.extra_fields.get("_digital_onboarding_upload_gate")
    if isinstance(gate, dict) and gate.get("awaiting_document_upload") and gate.get("expected_doc_type"):
        return str(gate["expected_doc_type"])
    if state.get("awaiting_document_upload") and state.get("expected_doc_type"):
        return str(state["expected_doc_type"])
    inferred = _infer_doc_type_from_text(_last_assistant_text(session), state)
    if requested != "drivers_license_front" and inferred == "drivers_license_front":
        return requested
    return inferred or requested


def _normalize_review_fields(fields: Any) -> dict[str, Any]:
    if not isinstance(fields, dict):
        return {}
    normalized = dict(fields)
    if "address" in normalized and "home_address" not in normalized:
        normalized["home_address"] = normalized.pop("address")
    return normalized


def _format_review_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    except ValueError:
        return text


def _format_review_enum(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("_", " ").title()


def _format_review_address(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value or "").strip()
    street = str(value.get("street_address1") or "").strip()
    street2 = str(value.get("street_address2") or "").strip()
    city = str(value.get("city") or "").strip()
    state = str(value.get("state") or "").strip()
    postal = str(value.get("postal_code") or "").strip()
    country = str(value.get("country") or "").strip()
    region = " ".join(part for part in [state, postal] if part)
    return ", ".join(part for part in [street, street2, city, region, country] if part)


def _review_display_items(fields: Any) -> list[tuple[str, str]]:
    normalized = _normalize_review_fields(fields)
    if not normalized:
        return []
    items: list[tuple[str, str]] = []
    given = str(normalized.get("given_name") or normalized.get("gvie_name") or "").strip()
    family = str(normalized.get("family_name") or "").strip()
    name = " ".join(part for part in [given, family] if part)
    if name:
        items.append(("Name", name))
    date_of_birth = _format_review_date(normalized.get("date_of_birth"))
    if date_of_birth:
        items.append(("Date of Birth", date_of_birth))
    gender = _format_review_enum(normalized.get("gender"))
    if gender:
        items.append(("Gender", gender))
    address = _format_review_address(normalized.get("home_address"))
    if address:
        items.append(("Home Address", address))
    displayed_keys = {"given_name", "gvie_name", "family_name", "date_of_birth", "gender", "home_address"}
    for key, value in normalized.items():
        if key in displayed_keys or value in (None, ""):
            continue
        items.append((str(key).replace("_", " ").title(), _format_review_address(value) if isinstance(value, dict) else str(value)))
    return items


def _format_drivers_license_review_text(fields: Any) -> str:
    items = _review_display_items(fields)
    if not items:
        return "Please review the extracted information from your driver's license and tell me whether it is correct."
    lines = ["The extracted information from your driver's license is as follows:", ""]
    lines.extend(f"{label}: {value}" for label, value in items)
    lines.extend(["", "Please review it and tell me whether it is correct, or tell me which field should be corrected."])
    return "\n".join(lines)


def _strip_redundant_review_json(text: str, fields: Any) -> str:
    if not text or not any(key in text for key in ("given_name", "family_name", "date_of_birth", "home_address")):
        return text
    stripped = text.rstrip()
    decoder = json.JSONDecoder()
    review_keys = {"given_name", "gvie_name", "family_name", "date_of_birth", "gender", "home_address", "address"}
    fenced = re.search(r"(?is)\n*```(?:json)?\s*(\{.*\})\s*```\s*$", stripped)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and set(parsed) & review_keys:
            prefix = stripped[: fenced.start()].rstrip()
            return prefix or _format_drivers_license_review_text(fields or parsed)
    for match in reversed(list(re.finditer(r"\{", stripped))):
        start = match.start()
        try:
            parsed, end = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            continue
        if start + end != len(stripped) or not isinstance(parsed, dict):
            continue
        if not (set(parsed) & review_keys):
            continue
        prefix = stripped[:start].rstrip()
        prefix = re.sub(r"```(?:json)?\s*$", "", prefix, flags=re.IGNORECASE).rstrip()
        return prefix or _format_drivers_license_review_text(fields or parsed)
    return text


def _document_review_text_input_allowed(session: InteractiveSession) -> bool:
    state = session.agent_data.extra_fields.get("onboarding_state")
    return bool(
        isinstance(state, dict)
        and state.get("document_upload_verified")
        and state.get("document_extracted")
        and state.get("document_review_pending")
        and not state.get("document_review_rejected")
    )


def _document_flow_visible_guard(session: InteractiveSession, visible_text: str) -> str:
    state = session.agent_data.extra_fields.get("onboarding_state")
    if not isinstance(state, dict) or not uploaded_image_required():
        return visible_text
    visible_text = _strip_redundant_review_json(
        visible_text,
        state.get("last_presented_document_fields") or state.get("last_extracted_document_fields") or {},
    )
    if not state.get("document_upload_verified"):
        return visible_text
    lower = visible_text.lower()
    suspicious_progress = bool(
        re.search(r"\b(collected|proceed|next|employment|funding|income|investment|risk|submit)\b", lower)
    )
    if state.get("document_captured") and not state.get("document_extracted") and suspicious_progress:
        return (
            "Thanks, I received the image. I need to extract the document information and show it for your review "
            "before we continue."
        )
    if state.get("document_review_pending") and not state.get("document_review_confirmed") and suspicious_progress:
        if "CAPTURE_RESULT" not in _last_user_text(session):
            return visible_text
        fields = _normalize_review_fields(state.get("last_presented_document_fields") or state.get("last_extracted_document_fields") or {})
        if isinstance(fields, dict) and fields:
            return _format_drivers_license_review_text(fields)
        return "Please review the extracted information from your driver's license and tell me whether it is correct."
    return visible_text


def _capture_result_user_message(event: dict[str, Any], *, filename: str, doc_type: str) -> str:
    result = event.get("result") if isinstance(event, dict) else {}
    result = result if isinstance(result, dict) else {}
    return (
        "CAPTURE_RESULT: document uploaded.\n"
        f"{UPLOADED_IMAGE_MARKER}\n"
        f"File Name: {filename}\n"
        f"Doc Type: {result.get('doc_type') or doc_type}\n"
        f"Document Field: {result.get('document_field') or 'drivers_license'}\n"
        f"File URL: {result.get('file_url') or ('uploaded://' + filename)}\n"
        f"File ID: {result.get('file_id') or ''}\n"
        f"Min File ID: {result.get('min_file_id') or ''}\n"
        f"Verification ID: {result.get('verification_id') or ''}"
    )


async def _continue_session(session: InteractiveSession) -> dict[str, Any]:
    assistant_messages: list[dict[str, str]] = []
    tool_events: list[dict[str, Any]] = []
    widgets: list[dict[str, Any]] = []
    model_events: list[dict[str, Any]] = []
    stopped_by_limit = False

    for _ in range(_max_auto_steps()):
        raw_content, calls, metadata = await _generate_assistant(session)
        session.messages.append({"role": "assistant", "content": raw_content})
        model_events.append({"raw_content": raw_content, "metadata": metadata, "tool_calls": calls})

        visible_text = _strip_model_artifacts(raw_content)
        if visible_text:
            visible_text = _document_flow_visible_guard(session, visible_text)
            visible_text_for_user = append_terms_if_needed(visible_text)
            if visible_text_for_user != visible_text and not calls:
                session.messages[-1]["content"] = visible_text_for_user
            assistant_messages.append({"role": "assistant", "content": visible_text_for_user})
            if not calls and not any(widget.get("kind") == "options" for widget in widgets):
                widgets.extend(choice_hint_widgets(visible_text))
            if uploaded_image_required() and assistant_requests_document_upload(visible_text_for_user):
                state = session.agent_data.extra_fields.get("onboarding_state")
                doc_type = _infer_doc_type_from_text(visible_text_for_user, state if isinstance(state, dict) else {})
                mark_document_upload_requested(session.agent_data, doc_type=doc_type)

        if not calls:
            break

        for call in calls[: _max_tool_calls_per_step()]:
            event = await _execute_tool(session, call)
            tool_events.append(event)
            if event.get("widget"):
                widgets.append(event["widget"])
            _append_tool_message(session, event)
    else:
        stopped_by_limit = True

    if not assistant_messages and stopped_by_limit:
        assistant_messages.append(
            {
                "role": "assistant",
                "content": "I reached the local auto-step limit while using tools. Please send your next answer or increase INTERACTIVE_MAX_AUTO_STEPS.",
            }
        )
    elif not assistant_messages and not tool_events:
        assistant_messages.append({"role": "assistant", "content": "The model returned an empty response."})

    if assistant_messages:
        for widget in widgets:
            if widget.get("kind") == "options":
                widget["hide_title"] = True

    return {
        "messages": assistant_messages,
        "tool_events": tool_events,
        "widgets": widgets,
        "model_events": model_events if _env_bool("INTERACTIVE_RETURN_RAW_MODEL_EVENTS", False) else [],
        "stopped_by_limit": stopped_by_limit,
    }


def _session_response(session: InteractiveSession, **extra: Any) -> dict[str, Any]:
    state = _public_state(session.agent_data.extra_fields.get("onboarding_state"))
    return {
        "session_id": session.session_id,
        "endpoint": _service_endpoint(),
        "model": _service_model(),
        "tool_backend": _tool_backend(),
        "real_upload_enabled": _real_upload_enabled(),
        "scenario": {
            "scenario_id": session.scenario.get("scenario_id"),
            "user_behavior": session.scenario.get("user_behavior"),
            "goal": session.scenario.get("goal"),
            "test_profile": _public_profile(session.scenario),
        },
        "state": state,
        **extra,
    }


app = FastAPI(title="Digital Onboarding Interactive Chat")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = STATIC_DIR / "interactive.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/config")
async def config() -> dict[str, Any]:
    session = _new_session()
    return _session_response(
        session,
        max_auto_steps=_max_auto_steps(),
        speech_hint="Use http://localhost through SSH port forwarding, or HTTPS, for browser microphone access.",
    )


@app.post("/api/reset")
async def reset(request: ResetRequest) -> dict[str, Any]:
    session = _new_session(session_id=request.session_id, scenario_index=request.scenario_index)
    return _session_response(session, messages=[], tool_events=[], widgets=[])


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    session = _get_session(request.session_id)
    user_text = request.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="Empty message.")

    if (
        uploaded_image_required()
        and document_upload_pending(session.agent_data)
        and not _document_review_text_input_allowed(session)
        and not contains_verified_uploaded_image(session.agent_data, user_text)
    ):
        return _session_response(
            session,
            messages=[{"role": "assistant", "content": UPLOAD_RETRY_MESSAGE}],
            tool_events=[],
            widgets=[],
            model_events=[],
            stopped_by_limit=False,
        )

    session.messages.append({"role": "user", "content": user_text})
    session.updated_at = time.time()
    return _session_response(session, **await _continue_session(session))

@app.post("/api/upload")
async def upload(request: UploadRequest) -> dict[str, Any]:
    session = _get_session(request.session_id)
    filename = request.filename.strip()
    file_data = request.file_data.strip()
    doc_type = _expected_upload_doc_type(session, request.doc_type)
    if not filename or not file_data:
        raise HTTPException(status_code=400, detail="filename and file_data are required.")

    try:
        verification = verify_image_upload(
            file_data=file_data,
            filename=filename,
            trajectory_id=session.session_id,
            doc_type=doc_type,
        )
        register_verified_upload(session.agent_data, verification)
    except Exception as exc:
        return _session_response(
            session,
            messages=[{"role": "assistant", "content": UPLOAD_RETRY_MESSAGE}],
            tool_events=[
                {
                    "tool": "verify_image_upload",
                    "arguments": {"filename": filename, "doc_type": doc_type},
                    "raw_response": str(exc),
                    "result": {"status": "error", "message": str(exc), "error_code": "image_verification_failed"},
                }
            ],
            widgets=[],
            model_events=[],
            stopped_by_limit=False,
        )
    try:
        upload_event = await _execute_tool(
            session,
            {
                "name": "upload_file",
                "arguments": {
                    "filename": filename,
                    "file_data": file_data,
                    "is_need_min": real_bank_upload_thumbnail_enabled(),
                    "doc_type": doc_type,
                },
            },
        )
    finally:
        clear_user_upload_in_progress(session.agent_data)
    result = upload_event.get("result") if isinstance(upload_event, dict) else {}
    result = result if isinstance(result, dict) else {}
    if result.get("status") != "success":
        return _session_response(
            session,
            messages=[{"role": "assistant", "content": UPLOAD_RETRY_MESSAGE}],
            tool_events=[upload_event],
            widgets=[],
            model_events=[],
            stopped_by_limit=False,
        )

    upload_proof = _capture_result_user_message(
        upload_event,
        filename=filename,
        doc_type=doc_type,
    )
    user_note = (request.message or "").strip()
    user_message = f"{user_note}\n\n{upload_proof}" if user_note else upload_proof
    image_part = {"type": "image_url", "image_url": {"url": file_data}}
    session.messages.append({"role": "user", "content": uploaded_image_user_content(user_message, image_part)})
    session.updated_at = time.time()
    continuation = await _continue_session(session)
    continuation["tool_events"] = [upload_event, *continuation.get("tool_events", [])]
    continuation["upload_message"] = user_message
    return _session_response(session, **continuation)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    endpoint = _service_endpoint()
    models_url = endpoint.rsplit("/v1/chat/completions", 1)[0] + "/v1/models"
    model_ready = False
    error = ""
    try:
        response = await asyncio.to_thread(requests.get, models_url, timeout=5)
        model_ready = response.ok
        if not response.ok:
            error = response.text[:500]
    except Exception as exc:
        error = str(exc)
    return {
        "ok": True,
        "model_ready": model_ready,
        "model_error": error,
        "endpoint": endpoint,
        "model": _service_model(),
        "tool_backend": _tool_backend(),
    }
