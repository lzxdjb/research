# Copyright 2025 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import json
import logging
import os
import re
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import torch
from PIL import Image

from verl.experimental.agent_loop.agent_loop import (
    AgentLoopBase,
    AgentLoopOutput,
    register,
)
from verl.experimental.agent_loop.tool_parser import FunctionCall, ToolParser
from verl.experimental.agent_loop.utils import build_gpt_oss_tool_response_text
from verl.interactions.base import BaseInteraction
from verl.interactions.utils.interaction_registry import initialize_interactions_from_config
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

try:
    from recipe.digital_onboarding.debug_logging import append_debug_csv
except Exception:  # pragma: no cover - keep generic agent loop import-safe
    append_debug_csv = None

try:
    from recipe.digital_onboarding.disclosure_terms import append_terms_if_needed
except Exception:  # pragma: no cover - keep generic agent loop import-safe
    append_terms_if_needed = None

try:
    from recipe.digital_onboarding.tools import (
        UPLOAD_RETRY_MESSAGE,
        assistant_requests_document_upload,
        contains_verified_uploaded_image,
        document_upload_pending,
        mark_document_upload_requested,
        uploaded_image_user_content,
        uploaded_image_required,
        verified_upload_for_message,
    )
except Exception:  # pragma: no cover - keep generic agent loop import-safe
    UPLOAD_RETRY_MESSAGE = "Sorry, but it seems that no image was uploaded successfully. Could you please upload the image again?"
    assistant_requests_document_upload = None
    contains_verified_uploaded_image = None
    document_upload_pending = None
    mark_document_upload_requested = None
    uploaded_image_user_content = None
    uploaded_image_required = None
    verified_upload_for_message = None

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _turn_debug_stdout_enabled() -> bool:
    return _env_flag("VERL_AGENT_LOOP_DEBUG_STDOUT", _env_flag("AGENT_LOOP_DEBUG_STDOUT", False))


def _digital_debug_csv_enabled() -> bool:
    if append_debug_csv is None or not os.environ.get("DIGITAL_ONBOARDING_DEBUG_CSV"):
        return False
    value = os.environ.get("DIGITAL_ONBOARDING_DEBUG_ENABLED")
    if value is None:
        value = os.environ.get("DIGITAL_ONBOARDING_DEBUG_LOGS")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


FINAL_CLOSING_RE = re.compile(
    r"\bno\s+problem(?:\s+at\s+all)?\s*!?\s+feel\s+free\s+to\s+chat\s+with\s+me\s+again\s+next\s+time\s*!?",
    re.IGNORECASE,
)
FINAL_CLOSING_INTENT_RE = re.compile(
    r"\b(?:goodbye|thank(?:\s+you)?|thanks|take\s+care|have\s+(?:a\s+)?(?:great|wonderful)\s+day)\b"
    r".{0,240}\bfeel\s+free\s+to\s+(?:chat|reach\s+out|contact)(?:\s+with\s+(?:me|us))?\b",
    re.IGNORECASE | re.DOTALL,
)
QUESTION_LIKE_FINAL_RE = re.compile(
    r"\?|(?:could|can|would)\s+you\b|please\s+(?:provide|share|upload|confirm|review)\b|"
    r"\bwhat(?:'s|\s+is)\b|\bnext\s+(?:question|step)\b",
    re.IGNORECASE,
)


class AgentState(Enum):
    PENDING = "pending"
    GENERATING = "generating"
    PROCESSING_TOOLS = "processing_tools"
    TERMINATED = "terminated"
    INTERACTING = "interacting"


class AgentData:
    """Encapsulates all state variables for the agent loop. AgentData is passed to tool calling in case that
    tool may need to access full history state. User can store any tool session data in `extra_fields`."""

    def __init__(
        self,
        messages: list[dict[str, Any]],
        image_data: list[Image.Image],
        video_data: list[tuple[torch.Tensor, dict[str, Any]]],
        metrics: dict[str, Any],
        request_id: str,
        tools_kwargs: dict[str, Any],
        interaction: Optional[BaseInteraction] = None,
        interaction_kwargs: Optional[dict[str, Any]] = None,
    ):
        self.messages = messages
        self.image_data = image_data
        self.video_data = video_data
        self.metrics = metrics
        self.request_id = request_id
        self.tools_kwargs = tools_kwargs
        self.interaction = interaction
        self.interaction_kwargs = interaction_kwargs or {}

        # State variables
        self.prompt_ids: list[int] = []
        self.response_ids: list[int] = []
        self.response_mask: list[int] = []
        self.response_logprobs: list[float] = []
        self.turn_scores: list[float] = []
        self.tool_rewards: list[float] = []
        self.user_turns = 0
        self.assistant_turns = 0

        # Temporary state for tool calls
        self.tool_calls: list[FunctionCall] = []

        self.routed_experts = None

        # Extra fields for dynamic addition, e.g., tool session data
        self.extra_fields: dict[str, Any] = {}


@register("tool_agent")
class ToolAgentLoop(AgentLoopBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize tools from config file
        self.max_user_turns = self.rollout_config.multi_turn.max_user_turns
        self.max_assistant_turns = self.rollout_config.multi_turn.max_assistant_turns
        self.max_parallel_calls = self.rollout_config.multi_turn.max_parallel_calls
        self.max_tool_response_length = self.rollout_config.multi_turn.max_tool_response_length
        self.tool_response_truncate_side = self.rollout_config.multi_turn.tool_response_truncate_side
        tool_config_path = self.rollout_config.multi_turn.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools = {tool.name: tool for tool in tool_list}
        self.tool_schemas = [tool.tool_schema.model_dump(exclude_unset=True, exclude_none=True) for tool in tool_list]
        self.tool_parser = ToolParser.get_tool_parser(self.rollout_config.multi_turn.format, self.tokenizer)
        self.tool_parser_name = self.rollout_config.multi_turn.format

        self.prompt_length = self.rollout_config.prompt_length
        self.response_length = self.rollout_config.response_length
        configured_sequence_length = self.prompt_length + self.response_length
        model_context_length = self._infer_model_context_length()
        self.sequence_length_limit = (
            min(configured_sequence_length, model_context_length)
            if model_context_length is not None
            else configured_sequence_length
        )

        # Initialize interactions from config file
        self.interaction_config_file = self.rollout_config.multi_turn.interaction_config_path
        if self.interaction_config_file:
            self.interaction_map: dict[str, BaseInteraction] = self._initialize_interactions(
                self.interaction_config_file
            )

    def _infer_model_context_length(self) -> int | None:
        rollout_max_model_len = self.rollout_config.get("max_model_len", None)
        if rollout_max_model_len not in (None, "", "null", "None"):
            return int(rollout_max_model_len)

        try:
            model_path = self.config.actor_rollout_ref.model.get("path", None)
        except Exception:
            model_path = None
        if not model_path:
            return None

        config_path = os.path.join(os.fspath(model_path), "config.json")
        try:
            with open(config_path, encoding="utf-8") as f:
                model_config = json.load(f)
        except Exception:
            return None
        max_len = self._find_max_position_embeddings(model_config)
        return int(max_len) if max_len is not None else None

    def _find_max_position_embeddings(self, config: Any) -> int | None:
        if isinstance(config, dict) and config.get("thinker_config") is not None:
            max_len = self._find_max_position_embeddings(config["thinker_config"])
            if max_len is not None:
                return max_len

        for field_name in (
            "max_position_embeddings",
            "max_sequence_length",
            "max_seq_len",
            "seq_length",
            "n_positions",
        ):
            if isinstance(config, dict) and config.get(field_name) is not None:
                return int(config[field_name])

        for field_name in (
            "text_config",
            "llm_config",
            "language_config",
            "talker_config",
            "decoder_config",
            "model_config",
        ):
            if isinstance(config, dict) and config.get(field_name) is not None:
                max_len = self._find_max_position_embeddings(config[field_name])
                if max_len is not None:
                    return max_len
        return None

    def _scenario_id(self, agent_data: AgentData) -> str:
        scenario_json = agent_data.interaction_kwargs.get("scenario_json") or agent_data.interaction_kwargs.get("scenario")
        if not scenario_json:
            return ""
        try:
            scenario = json.loads(scenario_json) if isinstance(scenario_json, str) else scenario_json
        except Exception:
            return ""
        return str(scenario.get("scenario_id", ""))

    def _tool_calls_for_log(self, tool_calls) -> list[dict[str, Any]]:
        rows = []
        for tool_call in tool_calls or []:
            rows.append({"name": getattr(tool_call, "name", ""), "arguments": getattr(tool_call, "arguments", "")})
        return rows

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            return "\n".join(part for part in parts if part)
        return str(content or "")

    def _uploaded_image_for_interaction_response(self, agent_data: AgentData, interaction_response: Any) -> Image.Image | None:
        if verified_upload_for_message is None:
            return None
        upload = verified_upload_for_message(agent_data, interaction_response)
        stored_path = str(upload.get("stored_path") or "")
        if not stored_path:
            return None
        try:
            with Image.open(stored_path) as image:
                return image.convert("RGB")
        except Exception as exc:
            logger.warning("Failed to load verified upload image from %s: %s", stored_path, exc)
            return None

    def _strip_think(self, text: str) -> str:
        text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
        return text.strip()

    def _has_final_closing_signal(self, text: str) -> bool:
        cleaned = self._strip_think(text or "")
        if not cleaned:
            return False
        if FINAL_CLOSING_RE.search(cleaned):
            return True
        if QUESTION_LIKE_FINAL_RE.search(cleaned):
            return False
        return bool(FINAL_CLOSING_INTENT_RE.search(cleaned))

    def _reward_role_label(self, role: Any, content: str) -> str:
        if "ONBOARDING_TOOL_RESULT" in (content or ""):
            return "tool"
        if role == "assistant":
            return "service"
        if role in {"user", "tool"}:
            return str(role)
        return str(role or "message")

    def _service_transcript_for_reward(self, agent_data: AgentData) -> str:
        lines: list[str] = []
        for message in agent_data.messages:
            role = message.get("role")
            if role == "system":
                continue
            content = self._content_to_text(message.get("content"))
            label = self._reward_role_label(role, content)
            if label == "service":
                content = self._strip_think(content)
            content = content.strip()
            if content:
                lines.append(f"{label}: {content}")
        return "\n".join(lines)

    def _write_debug_csv(
        self,
        agent_data: AgentData,
        *,
        event_type: str,
        role: str,
        content: str = "",
        prompt: str = "",
        response: str = "",
        tool_calls=None,
        tool_responses=None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if append_debug_csv is None:
            return
        append_debug_csv(
            os.environ.get("DIGITAL_ONBOARDING_DEBUG_CSV"),
            {
                "request_id": agent_data.request_id,
                "scenario_id": self._scenario_id(agent_data),
                "turn": agent_data.assistant_turns + agent_data.user_turns,
                "event_type": event_type,
                "role": role,
                "content": content,
                "prompt": prompt,
                "response": response,
                "tool_calls": self._tool_calls_for_log(tool_calls),
                "tool_responses": tool_responses or [],
                "metadata": metadata or {},
            },
        )

    def _terminate(self, agent_data: AgentData, reason: str, **metadata: Any) -> AgentState:
        agent_data.extra_fields["termination_reason"] = reason
        self._write_debug_csv(
            agent_data,
            event_type="TERMINATED",
            role="agent_loop",
            content=reason,
            metadata={
                "reason": reason,
                "assistant_turns": agent_data.assistant_turns,
                "user_turns": agent_data.user_turns,
                "response_mask_len": len(agent_data.response_mask),
                "response_length_limit": self.response_length,
                **metadata,
            },
        )
        return AgentState.TERMINATED

    def _remaining_generation_tokens(self, agent_data: AgentData) -> int:
        response_remaining = self.response_length - len(agent_data.response_mask)
        sequence_remaining = self.sequence_length_limit - len(agent_data.prompt_ids)
        return min(response_remaining, sequence_remaining)

    def _sampling_params_with_generation_budget(
        self, sampling_params: dict[str, Any], generation_budget: int
    ) -> dict[str, Any]:
        turn_sampling_params = dict(sampling_params)
        requested_max_tokens = turn_sampling_params.get("max_tokens", turn_sampling_params.get("max_new_tokens"))
        if requested_max_tokens is None:
            turn_sampling_params["max_tokens"] = generation_budget
        else:
            turn_sampling_params["max_tokens"] = min(int(requested_max_tokens), generation_budget)
            turn_sampling_params.pop("max_new_tokens", None)
        return turn_sampling_params

    def _would_leave_no_generation_budget(self, agent_data: AgentData, token_count: int) -> bool:
        return (
            len(agent_data.response_mask) + token_count >= self.response_length
            or len(agent_data.prompt_ids) + token_count >= self.sequence_length_limit
        )

    def _log_turn_debug(
            self,
            agent_data: AgentData,
            turn_type: str,
            prompt_ids: list[int],
            response_ids: list[int] | None = None,
            tool_calls=None,
            tool_responses=None,
        ):
        """Print raw debug info for a single turn."""
        should_print = _turn_debug_stdout_enabled()
        should_write_csv = _digital_debug_csv_enabled()
        if not should_print and not should_write_csv:
            return

        turn_num = agent_data.assistant_turns + agent_data.user_turns
        sep = "=" * 80
        prompt_text = self.tokenizer.decode(prompt_ids, skip_special_tokens=False)
        lines = [
            f"\n{sep}",
            f"[TURN DEBUG] request_id={agent_data.request_id}  turn={turn_num}  type={turn_type}",
            f"--- PROMPT ({len(prompt_ids)} tokens) ---",
            prompt_text,
        ]
        if response_ids is not None:
            response_text = self.tokenizer.decode(response_ids, skip_special_tokens=False)
            lines += [
                f"--- RESPONSE ({len(response_ids)} tokens) ---",
                response_text,
            ]
        else:
            response_text = ""
        if tool_calls:
            lines += [
                "--- TOOL CALLS ---",
                *[f"  [{i}] {tc.name}({tc.arguments})" for i, tc in enumerate(tool_calls)],
            ]
        if tool_responses:
            lines += [
                "--- TOOL RESPONSES ---",
                *[f"  [{i}] {r}" for i, r in enumerate(tool_responses)],
            ]
        lines.append(sep)
        if should_print:
            print("\n".join(lines), flush=True)
        self._write_debug_csv(
            agent_data,
            event_type=turn_type,
            role="assistant" if turn_type == "ASSISTANT_GENERATE" else "tool",
            content=response_text or "\n".join(str(r) for r in (tool_responses or [])),
            prompt=prompt_text,
            response=response_text,
            tool_calls=tool_calls,
            tool_responses=tool_responses,
            metadata={"prompt_tokens": len(prompt_ids), "response_tokens": len(response_ids or [])},
        )

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        messages = list(kwargs["raw_prompt"])

        # extract images and videos from messages
        multi_modal_data = await self.process_vision_info(messages)
        images = multi_modal_data.get("images")
        videos = multi_modal_data.get("videos")

        metrics = {}
        request_id = uuid4().hex
        tools_kwargs = kwargs.get("tools_kwargs", {})

        # Initialize interaction if needed
        interaction = None
        interaction_kwargs = {}
        if self.interaction_config_file:
            interaction_kwargs = kwargs["extra_info"]["interaction_kwargs"]
            if "name" not in interaction_kwargs:
                raise ValueError("'name' key is required in interaction_kwargs")
            interaction_name = interaction_kwargs["name"]
            if interaction_name not in self.interaction_map:
                raise ValueError(
                    f"Interaction '{interaction_name}' not found in interaction_map. Available interactions: "
                    f"{list(self.interaction_map.keys())}"
                )
            interaction = self.interaction_map[interaction_name]
            await interaction.start_interaction(request_id, **interaction_kwargs)
        # Create AgentData instance to encapsulate all state
        agent_data = AgentData(
            messages=messages,
            image_data=images,
            video_data=videos,
            metrics=metrics,
            request_id=request_id,
            tools_kwargs=tools_kwargs,
            interaction=interaction,
            interaction_kwargs=interaction_kwargs,
        )

        # State machine loop
        state = AgentState.PENDING
        while state != AgentState.TERMINATED:
            if state == AgentState.PENDING:
                state = await self._handle_pending_state(agent_data, sampling_params)
            elif state == AgentState.GENERATING:
                state = await self._handle_generating_state(agent_data, sampling_params)
            elif state == AgentState.PROCESSING_TOOLS:
                state = await self._handle_processing_tools_state(agent_data)
            elif state == AgentState.INTERACTING:
                state = await self._handle_interacting_state(agent_data)
            else:
                logger.error(f"Invalid state: {state}")
                state = AgentState.TERMINATED

        # Finalize output
        agent_data.extra_fields["service_transcript_for_reward"] = self._service_transcript_for_reward(agent_data)
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask) :]
        prompt_ids = agent_data.prompt_ids[: len(agent_data.prompt_ids) - len(agent_data.response_mask)]
        multi_modal_data = {}
        if agent_data.image_data is not None:
            multi_modal_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            multi_modal_data["videos"] = agent_data.video_data

        output: AgentLoopOutput = AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=agent_data.response_mask[: self.response_length],
            multi_modal_data=multi_modal_data,
            response_logprobs=agent_data.response_logprobs[: self.response_length]
            if agent_data.response_logprobs
            else None,
            num_turns=agent_data.user_turns + agent_data.assistant_turns + 1,
            metrics=agent_data.metrics,
            routed_experts=agent_data.routed_experts,
            extra_fields=agent_data.extra_fields,
        )
        output.extra_fields.setdefault("onboarding_state", None)
        output.extra_fields.update({"turn_scores": agent_data.turn_scores, "tool_rewards": agent_data.tool_rewards})
        return output

    async def _handle_pending_state(self, agent_data: AgentData, sampling_params: dict[str, Any]) -> AgentState:
        """Handle the pending state: prepare the prompt and start generation."""
        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=self.tool_schemas,
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        agent_data.prompt_ids = prompt_ids
        return AgentState.GENERATING

    async def _handle_generating_state(
        self, agent_data: AgentData, sampling_params: dict[str, Any], ignore_termination: bool = False
    ) -> AgentState:
        """Handle the generating state: generate model response and check for tool calls."""
        add_messages: list[dict[str, Any]] = []
        generation_budget = self._remaining_generation_tokens(agent_data)
        if generation_budget <= 0:
            return self._terminate(
                agent_data,
                "generation_budget_exhausted_before_assistant_generation",
                response_mask_len=len(agent_data.response_mask),
                prompt_tokens=len(agent_data.prompt_ids),
                response_length_limit=self.response_length,
                sequence_length_limit=self.sequence_length_limit,
            )
        turn_sampling_params = self._sampling_params_with_generation_budget(sampling_params, generation_budget)
        prompt_ids_before_generation = list(agent_data.prompt_ids)

        with simple_timer("generate_sequences", agent_data.metrics):
            output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id,
                prompt_ids=agent_data.prompt_ids,
                sampling_params=turn_sampling_params,
                image_data=agent_data.image_data,
                video_data=agent_data.video_data,
            )
        # first time to set num_preempted
        if agent_data.metrics.get("num_preempted") is None:
            agent_data.metrics["num_preempted"] = output.num_preempted if output.num_preempted is not None else -1
        # then add num_preempted to the metrics
        else:
            agent_data.metrics["num_preempted"] += output.num_preempted if output.num_preempted is not None else 0

        if not agent_data.extra_fields:
            agent_data.extra_fields.update(output.extra_fields)
        else:
            # Multi-round calls, only update the maximum max_global_steps.
            max_global_steps = output.extra_fields.get("max_global_steps", None)
            if max_global_steps:
                agent_data.extra_fields["max_global_steps"] = max_global_steps

        agent_data.assistant_turns += 1
        agent_data.response_ids = output.token_ids
        agent_data.prompt_ids += agent_data.response_ids
        agent_data.response_mask += [1] * len(agent_data.response_ids)
        
        # ✅ Log after generation
        self._log_turn_debug(
            agent_data,
            turn_type="ASSISTANT_GENERATE",
            prompt_ids=prompt_ids_before_generation,
            response_ids=output.token_ids,
            tool_calls=agent_data.tool_calls if agent_data.tool_calls else None,
        )
        if output.log_probs:
            agent_data.response_logprobs += output.log_probs

        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        # Check termination conditions
        if not agent_data.response_ids:
            reason = (
                "generation_budget_exhausted_after_assistant_generation"
                if output.stop_reason == "length"
                else "empty_assistant_generation"
            )
            return self._terminate(
                agent_data,
                reason,
                response_mask_len=len(agent_data.response_mask),
                prompt_tokens=len(agent_data.prompt_ids),
                response_length_limit=self.response_length,
                sequence_length_limit=self.sequence_length_limit,
                rollout_stop_reason=output.stop_reason,
            )
        if not ignore_termination and len(agent_data.response_mask) >= self.response_length:
            return self._terminate(
                agent_data,
                "response_length_limit_after_assistant_generation",
                response_mask_len=len(agent_data.response_mask),
                response_length_limit=self.response_length,
            )
        if not ignore_termination and self._remaining_generation_tokens(agent_data) <= 0:
            return self._terminate(
                agent_data,
                "generation_budget_exhausted_after_assistant_generation",
                response_mask_len=len(agent_data.response_mask),
                prompt_tokens=len(agent_data.prompt_ids),
                response_length_limit=self.response_length,
                sequence_length_limit=self.sequence_length_limit,
            )
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            return self._terminate(
                agent_data,
                "max_assistant_turns",
                assistant_turns=agent_data.assistant_turns,
                max_assistant_turns=self.max_assistant_turns,
            )
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            return self._terminate(
                agent_data,
                "max_user_turns",
                user_turns=agent_data.user_turns,
                max_user_turns=self.max_user_turns,
            )

        # Extract tool calls
        tools = [tool.tool_schema for tool in self.tools.values()]
        _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(agent_data.response_ids, tools)
        if agent_data.tool_calls:
            self._write_debug_csv(
                agent_data,
                event_type="SERVICE_TOOL_CALL_PARSED",
                role="assistant",
                tool_calls=agent_data.tool_calls,
                metadata={"tool_parser": self.tool_parser_name},
            )

        # Handle interaction if needed
        assistant_message = ""
        if self.interaction_config_file:
            assistant_message = await self.loop.run_in_executor(
                None, lambda: self.tokenizer.decode(agent_data.response_ids, skip_special_tokens=True)
            )
            assistant_message_for_interaction = (
                append_terms_if_needed(assistant_message) if append_terms_if_needed is not None else assistant_message
            )
            add_messages.append({"role": "assistant", "content": assistant_message_for_interaction})
            agent_data.messages.extend(add_messages)
            if (
                uploaded_image_required is not None
                and uploaded_image_required()
                and assistant_requests_document_upload is not None
                and assistant_requests_document_upload(assistant_message_for_interaction)
                and mark_document_upload_requested is not None
            ):
                mark_document_upload_requested(agent_data)
            if not agent_data.tool_calls and self._has_final_closing_signal(assistant_message):
                return self._terminate(
                    agent_data,
                    "assistant_final_closing_signal",
                    assistant_text_tail=assistant_message[-500:],
                )

        # Determine next state
        if agent_data.tool_calls:
            return AgentState.PROCESSING_TOOLS
        elif self.interaction_config_file:
            return AgentState.INTERACTING
        else:
            return AgentState.TERMINATED

    async def _handle_processing_tools_state(self, agent_data: AgentData) -> AgentState:
        """Handle the processing tools state: execute tool calls and prepare tool responses."""
        add_messages: list[dict[str, Any]] = []
        new_images_this_turn: list[Any] = []  # Local variable instead of agent_data attribute

        tasks = []
        tool_call_names = []
        for tool_call in agent_data.tool_calls[: self.max_parallel_calls]:
            tasks.append(self._call_tool(tool_call, agent_data.tools_kwargs, agent_data))
            tool_call_names.append(tool_call.name)

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)
            
        
        # ✅ Log tool responses before appending to prompt
        self._log_turn_debug(
        agent_data,
        turn_type="TOOL_RESPONSE",
        prompt_ids=agent_data.prompt_ids,  # full prompt so far
        tool_responses=[r[0].text for r in responses],  # ToolResponse.text per call
    )

        # Process tool responses and update multi_modal_data
        # Removed: agent_data.new_images_this_turn = []
        for tool_response, tool_reward, _ in responses:
            # Create message from tool response
            if tool_response.image or tool_response.video:
                # Multi-modal content with structured format
                if not getattr(self.processor, "image_processor", None):
                    raise ValueError(
                        "Multimedia data can only be processed by `processor`, but the processor is None. "
                        "This error is often caused if you are using a LLM model but your tool returns multimodal "
                        "data. Plase use a vlm as the base model."
                    )
                content = []
                if tool_response.image:
                    content.append({"type": "image"})
                if tool_response.video:
                    content.append({"type": "video"})
                if tool_response.text:
                    content.append({"type": "text", "text": tool_response.text})
                message = {"role": "tool", "content": content}
            else:
                # Text-only content
                message = {"role": "tool", "content": tool_response.text or ""}

            add_messages.append(message)

            # Handle image data
            if tool_response.image:
                # Add new image data
                if isinstance(tool_response.image, list):
                    # Ensure all elements in the list are valid image objects
                    for img in tool_response.image:
                        if img is not None:  # Add a check to ensure the image is not None
                            new_images_this_turn.append(img)  # Using local variable
                else:
                    # Ensure the image is not None
                    if tool_response.image is not None:
                        new_images_this_turn.append(tool_response.image)  # Using local variable

            # Handle video data
            if tool_response.video:
                # Currently not supported, raise informative error
                logger.warning("Multimedia type 'video' is not currently supported. Only 'image' is supported.")
                raise NotImplementedError(
                    "Multimedia type 'video' is not currently supported. Only 'image' is supported."
                )

            if tool_reward is not None:
                agent_data.tool_rewards.append(tool_reward)

        agent_data.messages.extend(add_messages)

        if self.tool_parser_name == "gpt-oss":
            logger.info("manually format tool responses for gpt-oss")
            tool_response_text = build_gpt_oss_tool_response_text(add_messages, tool_call_names)
            response_ids = await self.loop.run_in_executor(
                None, lambda: self.tokenizer.encode(tool_response_text, add_special_tokens=False)
            )
        else:
            # Note that we have to pass None to the images and videos if there are no new images / videos
            # to stay compatible with downstream image processing logic!
            images = new_images_this_turn if new_images_this_turn else None
            videos = None
            response_ids = await self.apply_chat_template(
                add_messages,
                images=images,
                videos=videos,
                remove_system_prompt=True,
            )

        if self._would_leave_no_generation_budget(agent_data, len(response_ids)):
            return self._terminate(
                agent_data,
                "response_length_limit_before_tool_response_append",
                response_mask_len=len(agent_data.response_mask),
                tool_response_tokens=len(response_ids),
                response_length_limit=self.response_length,
                prompt_tokens=len(agent_data.prompt_ids),
                sequence_length_limit=self.sequence_length_limit,
            )
        # Update prompt_ids and response_mask

        if new_images_this_turn:
            if agent_data.image_data is None:
                agent_data.image_data = []
            elif not isinstance(agent_data.image_data, list):
                agent_data.image_data = [agent_data.image_data]
            for img in new_images_this_turn:
                agent_data.image_data.append(img)

        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)
        agent_data.user_turns += 1
        return AgentState.GENERATING

    async def _handle_interacting_state(self, agent_data: AgentData) -> AgentState:
        """Handle the interacting state: get user input from interaction."""
        (
            should_terminate_sequence,
            interaction_responses,
            reward,
            metrics,
        ) = await agent_data.interaction.generate_response(
            agent_data.request_id, agent_data.messages, agent_data=agent_data, **agent_data.interaction_kwargs
        )
        interaction_info = metrics if isinstance(metrics, dict) else {}
        agent_data.user_turns += 1

        if (
            uploaded_image_required is not None
            and uploaded_image_required()
            and document_upload_pending is not None
            and document_upload_pending(agent_data)
            and contains_verified_uploaded_image is not None
            and not contains_verified_uploaded_image(agent_data, interaction_responses)
        ):
            add_messages: list[dict[str, Any]] = [{"role": "assistant", "content": UPLOAD_RETRY_MESSAGE}]
            agent_data.messages.extend(add_messages)
            response_ids = await self.apply_chat_template(add_messages, remove_system_prompt=True)
            if self._would_leave_no_generation_budget(agent_data, len(response_ids)):
                return self._terminate(
                    agent_data,
                    "generation_budget_exhausted_before_upload_retry_append",
                    response_mask_len=len(agent_data.response_mask),
                    retry_tokens=len(response_ids),
                    response_length_limit=self.response_length,
                    prompt_tokens=len(agent_data.prompt_ids),
                    sequence_length_limit=self.sequence_length_limit,
                    interaction_info=interaction_info,
                )
            agent_data.prompt_ids += response_ids
            agent_data.response_mask += [0] * len(response_ids)
            if agent_data.response_logprobs:
                agent_data.response_logprobs += [0.0] * len(response_ids)
            return AgentState.INTERACTING
        new_images_this_turn: list[Image.Image] = []
        user_content: Any = interaction_responses
        if contains_verified_uploaded_image is not None and contains_verified_uploaded_image(agent_data, interaction_responses):
            uploaded_image = self._uploaded_image_for_interaction_response(agent_data, interaction_responses)
            if uploaded_image is not None:
                new_images_this_turn.append(uploaded_image)
                image_part = {"type": "image", "image": uploaded_image}
                if uploaded_image_user_content is not None:
                    user_content = uploaded_image_user_content(str(interaction_responses), image_part)
                else:
                    user_content = [{"type": "text", "text": str(interaction_responses)}, image_part]

        add_messages = [{"role": "user", "content": user_content}]
        agent_data.messages.extend(add_messages)

        if reward is not None:
            agent_data.turn_scores.append(reward)

        # Update prompt with user responses (similar to _handle_processing_tools_state)
        response_ids = await self.apply_chat_template(
            add_messages,
            images=new_images_this_turn if new_images_this_turn else None,
            remove_system_prompt=True,
        )

        if self._would_leave_no_generation_budget(agent_data, len(response_ids)):
            return self._terminate(
                agent_data,
                "generation_budget_exhausted_before_interaction_append",
                response_mask_len=len(agent_data.response_mask),
                user_response_tokens=len(response_ids),
                response_length_limit=self.response_length,
                prompt_tokens=len(agent_data.prompt_ids),
                sequence_length_limit=self.sequence_length_limit,
                interaction_info=interaction_info,
            )

        # Update prompt_ids and response_mask
        if new_images_this_turn:
            if agent_data.image_data is None:
                agent_data.image_data = []
            elif not isinstance(agent_data.image_data, list):
                agent_data.image_data = [agent_data.image_data]
            for img in new_images_this_turn:
                agent_data.image_data.append(img)

        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)

        # double check prompt
        # Check termination condition
        if should_terminate_sequence:
            reason = str(interaction_info.get("reason") or "interaction_requested_termination")
            return self._terminate(agent_data, reason, interaction_info=interaction_info)
        if self._remaining_generation_tokens(agent_data) <= 0:
            return self._terminate(
                agent_data,
                "generation_budget_exhausted_after_interaction_append",
                response_mask_len=len(agent_data.response_mask),
                user_response_tokens=len(response_ids),
                response_length_limit=self.response_length,
                prompt_tokens=len(agent_data.prompt_ids),
                sequence_length_limit=self.sequence_length_limit,
                interaction_info=interaction_info,
            )
        return AgentState.GENERATING

    async def _call_tool(
        self, tool_call: FunctionCall, tools_kwargs: dict[str, Any], agent_data: AgentData
    ) -> tuple[ToolResponse, float, dict]:
        """Call tool and return tool response."""
        tool, instance_id = None, None
        try:
            # TODO: append malformed tool_call to the prompt: invalid function name or arguments
            tool_name = tool_call.name
            tool_args = json.loads(tool_call.arguments)
            tool = self.tools[tool_name]
            kwargs = tools_kwargs.get(tool_name, {})
            instance_id, _ = await tool.create(create_kwargs=kwargs.get("create_kwargs", {}))
            tool_execution_response, tool_reward, res = await tool.execute(
                instance_id, tool_args, agent_data=agent_data
            )
        except Exception as e:
            logger.warning(f"Error when executing tool: {e}")
            return (
                ToolResponse(
                    text=f"Error when executing tool: {e}",
                ),
                0.0,
                {},
            )
        finally:
            if tool and instance_id:
                await tool.release(instance_id)

        tool_response_text = tool_execution_response.text
        if tool_response_text and len(tool_response_text) > self.max_tool_response_length:
            if self.tool_response_truncate_side == "left":
                tool_response_text = tool_response_text[: self.max_tool_response_length] + "...(truncated)"
            elif self.tool_response_truncate_side == "right":
                tool_response_text = "(truncated)..." + tool_response_text[-self.max_tool_response_length :]
            else:
                length = self.max_tool_response_length // 2
                tool_response_text = tool_response_text[:length] + "...(truncated)..." + tool_response_text[-length:]

        # Create ToolResponse from tool execution result
        tool_response_kwargs = {"text": tool_response_text}

        # Add multimedia data if present
        for attr_name in ["image", "video"]:
            if hasattr(tool_execution_response, attr_name):
                attr_value = getattr(tool_execution_response, attr_name)
                if attr_value is not None:
                    tool_response_kwargs[attr_name] = attr_value

        return ToolResponse(**tool_response_kwargs), tool_reward, res

    def _initialize_interactions(self, interaction_config_file):
        """Initialize interactions from configuration.
        Returns:
            dict[str, BaseInteraction]: A dictionary mapping interaction names to interaction instances.
        """
        if interaction_config_file is None:
            return {}

        interaction_map = initialize_interactions_from_config(interaction_config_file)
        return interaction_map
