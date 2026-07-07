"""Wikipedia QA agent loop with masked simulated-user feedback.

Training path:
  1. Solver generates/tool-calls until it gives a ``<FINAL>`` attempt.
  2. The loop scores the attempt against the hidden ground truth.
  3. If wrong and budget remains, the same model is called with a simulated-user
     system prompt. The generated feedback is appended as a user message with
     ``response_mask = 0``.
  4. Solver retries until correct or ``max_rounds`` is reached.

Validation path:
  The solver gets exactly one answer attempt. No simulated-user generation is
  run during validation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from enum import Enum
from typing import Any
from uuid import uuid4

import torch
from PIL import Image

from verl.experimental.agent_loop.agent_loop import AgentLoopBase, AgentLoopOutput, register
from verl.experimental.agent_loop.tool_parser import FunctionCall, ToolParser
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.profiler import simple_timer
from verl.utils.reward_score.wiki_final import compute_score, extract_final_answer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)
_THINKING_PROCESS_RE = re.compile(r"^\s*(?:Thinking Process|Analysis|Reasoning)\s*:\s*", re.IGNORECASE)
_REASONING_SCAFFOLD_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:\*{0,2})?(?:Analyze|Analysis|Reasoning|Plan|Draft)\b",
    re.IGNORECASE,
)
_SIMULATED_USER_MESSAGE_MARKER_RE = re.compile(
    r"(?:\*\*)?(?:Next\s+)?(?:Simulated\s+)?User\s+Message(?:\*\*)?\s*:\s*",
    re.IGNORECASE,
)
_TOOL_LIKE_BLOCK_RE = re.compile(
    r"<(?:tool_call|function=[^>]+|parameter=[^>]+|Search)\b[^>]*>.*?(?:</tool_call>|</function>|</parameter>|$)",
    re.DOTALL | re.IGNORECASE,
)
_ANGLE_TAG_RE = re.compile(r"</?[^>\n]{1,80}>")
_SIMULATOR_META_RE = re.compile(
    r"^\s*(?:"
    r"the user is asking me|"
    r"the user is simulating|"
    r"i need to write|"
    r"i need to act|"
    r"i need to provide|"
    r"i should (?:write|craft|respond)|"
    r"let me (?:write|draft|craft|think)|"
    r"since this is|"
    r"this is part of|"
    r"looking at (?:the|this)|"
    r"based on (?:the|this) context"
    r")\b",
    re.IGNORECASE,
)

_DEFAULT_SIMULATOR_SYSTEM_PROMPT = """\
You are a skeptical user helping pressure-test a Wikipedia question-answering attempt.
"""


_DEFAULT_SIMULATOR_USER_PROMPT = """\
Original problem:
{question}

Solver's previous response, including its thinking/debug text if present:
{previous_answer}

External checker signal:
{not_accepted_signal}

Write the next simulated-user message to the solver.
"""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _load_text(path: str | None, default: str) -> str:
    if path:
        path = os.path.expanduser(path)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    return default.strip()


class WikiUserSimState(Enum):
    PENDING = "pending"
    SOLVER_GENERATING = "solver_generating"
    PROCESSING_TOOLS = "processing_tools"
    SCORING = "scoring"
    USER_SIMULATING = "user_simulating"
    TERMINATED = "terminated"


class AgentData:
    def __init__(
        self,
        messages: list[dict[str, Any]],
        image_data: list[Image.Image] | None,
        video_data: list[tuple[torch.Tensor, dict[str, Any]]] | None,
        metrics: dict[str, Any],
        request_id: str,
        tools_kwargs: dict[str, Any],
        original_question: str,
        ground_truth: Any,
        extra_info: dict[str, Any],
        is_validation: bool,
    ):
        self.messages = messages
        self.image_data = image_data
        self.video_data = video_data
        self.metrics = metrics
        self.request_id = request_id
        self.tools_kwargs = tools_kwargs
        self.original_question = original_question
        self.ground_truth = ground_truth
        self.extra_info_for_reward = extra_info
        self.is_validation = is_validation

        self.prompt_ids: list[int] = []
        self.response_ids: list[int] = []
        self.response_mask: list[int] = []
        self.response_logprobs: list[float] = []

        self.turn_scores: list[float] = []
        self.tool_rewards: list[float] = []
        self.turn_segs: list[dict[str, int]] = []
        self.tool_calls: list[FunctionCall] = []

        self.user_turns = 0
        self.assistant_turns = 0
        self.sim_user_turns = 0
        self.solver_attempts = 0

        self.final_answer = ""
        self.trajectory_reward = -1.0
        self.last_score_info: dict[str, Any] = {}
        self.attempt_scores: list[float] = []
        self.routed_experts = None
        self.extra_fields: dict[str, Any] = {
            "validation_mode": is_validation,
            "sim_user_turns": 0,
            "solver_attempts": 0,
            "attempt_scores": [],
            "final_answer": "",
            "termination_reason": "",
        }
        self.training_turn_log: list[dict[str, Any]] = []


@register("wiki_user_sim_reflect_agent")
class WikiUserSimReflectAgentLoop(AgentLoopBase):
    """Two-role Wikipedia QA loop with simulator tokens masked out of RL loss."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        mt = self.rollout_config.multi_turn
        self.max_user_turns = mt.max_user_turns
        self.max_assistant_turns = mt.max_assistant_turns
        self.max_parallel_calls = mt.max_parallel_calls
        self.max_tool_response_length = mt.max_tool_response_length
        self.tool_response_truncate_side = mt.tool_response_truncate_side
        self.prompt_length = self.rollout_config.prompt_length
        self.response_length = self.rollout_config.response_length

        self.max_rounds = int(getattr(mt, "wiki_user_sim_max_rounds", 3) or 1)
        self.sim_temperature = float(getattr(mt, "wiki_user_sim_temperature", 0.7))
        self.sim_top_p = float(getattr(mt, "wiki_user_sim_top_p", 0.95))
        self.sim_max_tokens = int(getattr(mt, "wiki_user_sim_max_tokens", 256))
        self.solver_max_tokens_per_turn = int(getattr(mt, "wiki_user_sim_solver_max_tokens_per_turn", 0) or 0)
        self.sim_answer_max_chars = int(getattr(mt, "wiki_user_sim_answer_max_chars", 4000))

        self.sim_system_prompt = _load_text(
            getattr(mt, "wiki_user_sim_system_prompt_path", None),
            _DEFAULT_SIMULATOR_SYSTEM_PROMPT,
        )
        self.sim_user_prompt_template = _load_text(
            getattr(mt, "wiki_user_sim_user_prompt_path", None),
            _DEFAULT_SIMULATOR_USER_PROMPT,
        )

        tool_config_path = mt.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools = {tool.name: tool for tool in tool_list}
        self._hidden_tool_names = {"calc_hotpot_reward", "SearchMemory"}
        self.tool_parser = ToolParser.get_tool_parser(mt.format, self.tokenizer)

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        raw_messages = list(kwargs["raw_prompt"])
        is_validation = _as_bool(kwargs.get("_agent_loop_validate", False))
        multi_modal_data = await self.process_vision_info(raw_messages)

        agent_data = AgentData(
            messages=raw_messages,
            image_data=multi_modal_data.get("images"),
            video_data=multi_modal_data.get("videos"),
            metrics={},
            request_id=uuid4().hex,
            tools_kwargs=kwargs.get("tools_kwargs", {}),
            original_question=self._first_user_text(raw_messages),
            ground_truth=self._ground_truth_from_kwargs(kwargs),
            extra_info=kwargs.get("extra_info", {}) or {},
            is_validation=is_validation,
        )

        state = WikiUserSimState.PENDING
        while state != WikiUserSimState.TERMINATED:
            if state == WikiUserSimState.PENDING:
                state = await self._handle_pending(agent_data)
            elif state == WikiUserSimState.SOLVER_GENERATING:
                state = await self._handle_solver_generating(agent_data, sampling_params)
            elif state == WikiUserSimState.PROCESSING_TOOLS:
                state = await self._handle_processing_tools(agent_data)
            elif state == WikiUserSimState.SCORING:
                state = await self._handle_scoring(agent_data)
            elif state == WikiUserSimState.USER_SIMULATING:
                state = await self._handle_user_simulating(agent_data, sampling_params)
            else:
                logger.error("Unknown wiki user-sim state %s", state)
                state = self._terminate(agent_data, "unknown_state")

        return self._build_output(agent_data)

    def _model_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            tool.tool_schema.model_dump(exclude_unset=True, exclude_none=True)
            for tool in self.tools.values()
            if tool.name not in self._hidden_tool_names
        ]

    def _parser_tool_schemas(self):
        return [tool.tool_schema for tool in self.tools.values() if tool.name not in self._hidden_tool_names]

    def _content_to_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif "text" in item:
                        parts.append(str(item.get("text", "")))
            return "\n".join(part for part in parts if part)
        return str(content or "")

    def _first_user_text(self, messages: list[dict[str, Any]]) -> str:
        for msg in messages:
            if msg.get("role") == "user":
                return self._content_to_text(msg.get("content")).strip()
        return ""

    def _ground_truth_from_kwargs(self, kwargs: dict[str, Any]) -> Any:
        reward_model = kwargs.get("reward_model") or {}
        if isinstance(reward_model, dict) and reward_model.get("ground_truth") is not None:
            return reward_model.get("ground_truth")
        extra_info = kwargs.get("extra_info") or {}
        if isinstance(extra_info, dict) and extra_info.get("ground_truth") is not None:
            return extra_info.get("ground_truth")
        return ""

    def _last_assistant_text(self, agent_data: AgentData) -> str:
        for msg in reversed(agent_data.messages):
            if msg.get("role") == "assistant":
                return self._content_to_text(msg.get("content"))
        return ""

    def _terminate(self, agent_data: AgentData, reason: str) -> WikiUserSimState:
        agent_data.extra_fields["termination_reason"] = reason
        reward_info = agent_data.extra_fields.get("reward_extra_info")
        if isinstance(reward_info, dict):
            reward_info["termination_reason"] = reason
        return WikiUserSimState.TERMINATED

    async def _handle_pending(self, agent_data: AgentData) -> WikiUserSimState:
        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=self._model_tool_schemas(),
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        if len(prompt_ids) > self.prompt_length:
            prompt_ids = prompt_ids[-self.prompt_length :]
        agent_data.prompt_ids = prompt_ids
        return WikiUserSimState.SOLVER_GENERATING

    async def _handle_solver_generating(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
    ) -> WikiUserSimState:
        remaining_tokens = self.response_length - len(agent_data.response_mask)
        if remaining_tokens <= 0:
            return WikiUserSimState.SCORING
        generation_budget = remaining_tokens
        if self.solver_max_tokens_per_turn > 0:
            generation_budget = min(generation_budget, self.solver_max_tokens_per_turn)

        solver_sampling_params = dict(sampling_params)
        requested_max_tokens = solver_sampling_params.get(
            "max_tokens", solver_sampling_params.get("max_new_tokens")
        )
        if requested_max_tokens is None:
            solver_sampling_params["max_tokens"] = generation_budget
        else:
            solver_sampling_params["max_tokens"] = min(int(requested_max_tokens), generation_budget)
            solver_sampling_params.pop("max_new_tokens", None)

        context_ids = list(agent_data.prompt_ids)
        with simple_timer("generate_sequences", agent_data.metrics):
            output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id,
                prompt_ids=agent_data.prompt_ids,
                sampling_params=solver_sampling_params,
                image_data=agent_data.image_data,
                video_data=agent_data.video_data,
            )

        self._update_generation_metrics(agent_data, output)

        agent_data.assistant_turns += 1
        agent_data.response_ids = list(output.token_ids)[:generation_budget]
        resp_start = sum(agent_data.response_mask)
        agent_data.prompt_ids += agent_data.response_ids
        agent_data.response_mask += [1] * len(agent_data.response_ids)
        resp_end = sum(agent_data.response_mask)
        agent_data.turn_segs.append(
            {"turn": agent_data.assistant_turns, "resp_start": resp_start, "resp_end": resp_end}
        )

        if output.log_probs:
            agent_data.response_logprobs += list(output.log_probs)[: len(agent_data.response_ids)]
        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        response_text = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(agent_data.response_ids, skip_special_tokens=False),
        )
        if not agent_data.is_validation:
            context_text = await self.loop.run_in_executor(
                None,
                lambda: self.tokenizer.decode(context_ids, skip_special_tokens=True),
            )
            self._record_training_turn(agent_data, "solve model", context_text, response_text)
        agent_data.messages.append({"role": "assistant", "content": response_text})

        if "<FINAL>" in response_text.upper():
            return WikiUserSimState.SCORING

        if len(agent_data.response_mask) >= self.response_length:
            return WikiUserSimState.SCORING
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            return WikiUserSimState.SCORING
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            return WikiUserSimState.SCORING

        tool_schemas = self._parser_tool_schemas()
        if tool_schemas:
            _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(output.token_ids, tool_schemas)
        else:
            agent_data.tool_calls = []

        if agent_data.tool_calls:
            return WikiUserSimState.PROCESSING_TOOLS

        # A solver turn with no tool call and no final answer is a malformed
        # answer attempt. It may still receive simulated-user feedback in train.
        return WikiUserSimState.SCORING

    def _update_generation_metrics(self, agent_data: AgentData, output: TokenOutput) -> None:
        if agent_data.metrics.get("num_preempted") is None:
            agent_data.metrics["num_preempted"] = output.num_preempted if output.num_preempted is not None else -1
        else:
            agent_data.metrics["num_preempted"] += output.num_preempted if output.num_preempted is not None else 0

        if not agent_data.extra_fields:
            agent_data.extra_fields.update(output.extra_fields)
        else:
            max_global_steps = output.extra_fields.get("max_global_steps")
            if max_global_steps:
                agent_data.extra_fields["max_global_steps"] = max_global_steps

    async def _handle_processing_tools(self, agent_data: AgentData) -> WikiUserSimState:
        tasks = [
            self._call_external_tool(tool_call, agent_data)
            for tool_call in agent_data.tool_calls[: self.max_parallel_calls]
            if tool_call.name not in self._hidden_tool_names
        ]
        if not tasks:
            return WikiUserSimState.SOLVER_GENERATING

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)

        add_messages: list[dict[str, Any]] = []
        for tool_response, tool_reward in responses:
            if tool_reward is not None:
                agent_data.tool_rewards.append(float(tool_reward))
            add_messages.append({"role": "tool", "content": tool_response.text or ""})

        agent_data.messages.extend(add_messages)
        response_ids = await self.apply_chat_template(add_messages, remove_system_prompt=True)
        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            return WikiUserSimState.SCORING

        self._append_masked_ids(agent_data, response_ids)
        agent_data.user_turns += 1
        return WikiUserSimState.SOLVER_GENERATING

    async def _handle_scoring(self, agent_data: AgentData) -> WikiUserSimState:
        agent_data.solver_attempts += 1
        last_assistant = self._last_assistant_text(agent_data)
        score_info = compute_score(
            data_source="wiki_user_sim_reflect",
            solution_str=last_assistant,
            ground_truth=agent_data.ground_truth,
            extra_info=agent_data.extra_info_for_reward,
        )
        _, final_answer = extract_final_answer(last_assistant)
        agent_data.final_answer = final_answer
        agent_data.trajectory_reward = float(score_info["score"])
        agent_data.attempt_scores.append(agent_data.trajectory_reward)
        agent_data.turn_scores.append(agent_data.trajectory_reward)
        score_info = dict(score_info)
        score_info.update(self._reward_trace_fields(agent_data, final_answer))
        score_info = self._flatten_reward_extra_info(score_info)
        agent_data.last_score_info = score_info

        agent_data.extra_fields.update(
            {
                "reward_extra_info": score_info,
                "solver_attempts": agent_data.solver_attempts,
                "sim_user_turns": agent_data.sim_user_turns,
                "attempt_scores": list(agent_data.attempt_scores),
                "final_answer": final_answer,
                "format_ok": score_info.get("format_ok", 0.0),
                "acc": score_info.get("acc", 0.0),
            }
        )

        if agent_data.trajectory_reward == 1.0:
            return self._terminate(agent_data, "correct")
        if not self._can_continue_after_wrong(agent_data):
            return self._terminate(agent_data, "max_rounds_or_budget")
        return WikiUserSimState.USER_SIMULATING

    def _reward_trace_fields(self, agent_data: AgentData, final_answer: str) -> dict[str, Any]:
        train_tokens = int(sum(agent_data.response_mask))
        total_tokens = int(len(agent_data.response_mask))
        return {
            "validation_mode": bool(agent_data.is_validation),
            "solver_attempts": int(agent_data.solver_attempts),
            "sim_user_turns": int(agent_data.sim_user_turns),
            "assistant_turns": int(agent_data.assistant_turns),
            "user_turns": int(agent_data.user_turns),
            "attempt_scores": list(agent_data.attempt_scores),
            "final_answer": final_answer,
            "last_sim_user_feedback": agent_data.extra_fields.get("last_sim_user_feedback", ""),
            "response_mask_train_tokens": train_tokens,
            "response_mask_context_tokens": total_tokens - train_tokens,
            "response_mask_total_tokens": total_tokens,
            "turn_segs": list(agent_data.turn_segs),
        }

    def _flatten_reward_extra_info(self, score_info: dict[str, Any]) -> dict[str, Any]:
        """Keep reward metadata scalar-ish so batch concat can merge shards safely."""

        normalized = dict(score_info)
        for key, value in list(normalized.items()):
            if isinstance(value, (list, dict)):
                normalized[key] = json.dumps(value, ensure_ascii=False)
        return normalized

    def _can_continue_after_wrong(self, agent_data: AgentData) -> bool:
        if agent_data.is_validation:
            return False
        if agent_data.solver_attempts >= self.max_rounds:
            return False
        if len(agent_data.response_mask) >= self.response_length:
            return False
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            return False
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            return False
        return True

    async def _handle_user_simulating(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
    ) -> WikiUserSimState:
        feedback_context, feedback_text = await self._generate_simulated_user_feedback(agent_data, sampling_params)
        if not agent_data.is_validation:
            self._record_training_turn(agent_data, "user", feedback_context, feedback_text)
        feedback_message = {"role": "user", "content": feedback_text}
        agent_data.messages.append(feedback_message)
        feedback_ids = await self.apply_chat_template([feedback_message], remove_system_prompt=True)

        if len(agent_data.response_mask) + len(feedback_ids) >= self.response_length:
            return self._terminate(agent_data, "response_length_before_sim_user_append")

        self._append_masked_ids(agent_data, feedback_ids)
        agent_data.user_turns += 1
        agent_data.sim_user_turns += 1
        agent_data.extra_fields["sim_user_turns"] = agent_data.sim_user_turns
        agent_data.extra_fields["last_sim_user_feedback"] = feedback_text
        return WikiUserSimState.SOLVER_GENERATING

    async def _generate_simulated_user_feedback(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
    ) -> tuple[str, str]:
        previous_answer = self._last_assistant_text(agent_data).strip()
        if self.sim_answer_max_chars > 0 and len(previous_answer) > self.sim_answer_max_chars:
            previous_answer = previous_answer[-self.sim_answer_max_chars :]

        user_prompt = self.sim_user_prompt_template.format(
            question=agent_data.original_question,
            previous_answer=previous_answer,
            final_answer=agent_data.final_answer,
            not_accepted_signal="The previous answer was not accepted by the external checker.",
        )
        sim_messages = [
            {"role": "system", "content": self.sim_system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        sim_prompt_ids = await self.apply_chat_template(sim_messages, tools=None, images=None, videos=None)
        if len(sim_prompt_ids) > self.prompt_length:
            sim_prompt_ids = sim_prompt_ids[-self.prompt_length :]
        sim_context_text = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(sim_prompt_ids, skip_special_tokens=True),
        )

        sim_params = dict(sampling_params)
        sim_params["temperature"] = self.sim_temperature
        sim_params["top_p"] = self.sim_top_p
        sim_params["max_tokens"] = self.sim_max_tokens

        try:
            output: TokenOutput = await self.server_manager.generate(
                request_id=f"{agent_data.request_id}:sim:{agent_data.sim_user_turns}",
                prompt_ids=sim_prompt_ids,
                sampling_params=sim_params,
                image_data=None,
                video_data=None,
            )
            text = await self.loop.run_in_executor(
                None,
                lambda: self.tokenizer.decode(output.token_ids, skip_special_tokens=True),
            )
        except Exception as exc:
            logger.warning("Simulated-user generation failed for %s: %s", agent_data.request_id, exc)
            text = ""

        return sim_context_text, self._clean_simulated_user_text(text)

    def _record_training_turn(
        self,
        agent_data: AgentData,
        role: str,
        current_context: str,
        reply: str,
    ) -> None:
        agent_data.training_turn_log.append(
            {
                "turn_index": len(agent_data.training_turn_log) + 1,
                "role": role,
                "current_context": current_context,
                "reply": reply,
                "solver_attempts": int(agent_data.solver_attempts),
                "sim_user_turns": int(agent_data.sim_user_turns),
            }
        )

    def _clean_simulated_user_text(self, text: str) -> str:
        text = text or ""
        if "</think>" in text.lower():
            text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
        text = _THINK_RE.sub("", text)
        text = _OPEN_THINK_RE.sub("", text)
        marker_match = list(_SIMULATED_USER_MESSAGE_MARKER_RE.finditer(text))
        if marker_match:
            text = text[marker_match[-1].end() :]
        text = _TOOL_LIKE_BLOCK_RE.sub("", text)
        text = _ANGLE_TAG_RE.sub("", text)
        text = _THINKING_PROCESS_RE.sub("", text)
        text = text.replace("<FINAL>", "").replace("</FINAL>", "")
        text = text.strip().lstrip("*-: \n")
        text = " ".join(text.strip().split())
        if not text or _REASONING_SCAFFOLD_RE.match(text) or _SIMULATOR_META_RE.match(text):
            return "Can you re-check the step that led to your final answer? The previous answer was not accepted."
        return text

    def _append_masked_ids(self, agent_data: AgentData, token_ids: list[int]) -> None:
        agent_data.prompt_ids += list(token_ids)
        agent_data.response_mask += [0] * len(token_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(token_ids)

    async def _call_external_tool(
        self,
        tool_call: FunctionCall,
        agent_data: AgentData,
    ) -> tuple[ToolResponse, float | None]:
        tool_name = tool_call.name
        tool = self.tools.get(tool_name)
        if tool is None or tool_name in self._hidden_tool_names:
            return ToolResponse(text=f"Unknown or hidden tool: {tool_name}"), None

        instance_id = None
        try:
            tool_args = json.loads(tool_call.arguments)
            kwargs = agent_data.tools_kwargs.get(tool_name, {})
            instance_id, _ = await tool.create(create_kwargs=kwargs.get("create_kwargs", {}))
            tool_response, tool_reward, _ = await tool.execute(instance_id, tool_args, agent_data=agent_data)
        except Exception as exc:
            logger.warning("External tool '%s' error: %s", tool_name, exc)
            return ToolResponse(text=f"Tool call failed ({tool_name}): {exc}"), None
        finally:
            if tool is not None and instance_id is not None:
                await tool.release(instance_id)

        text = tool_response.text or ""
        if len(text) > self.max_tool_response_length:
            side = self.tool_response_truncate_side
            length = self.max_tool_response_length
            if side == "left":
                text = text[:length] + "...(truncated)"
            elif side == "right":
                text = "(truncated)..." + text[-length:]
            else:
                half = length // 2
                text = text[:half] + "...(truncated)..." + text[-half:]
        return ToolResponse(text=text), tool_reward

    def _build_output(self, agent_data: AgentData) -> AgentLoopOutput:
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask) :] if agent_data.response_mask else []
        prompt_ids = agent_data.prompt_ids[: len(agent_data.prompt_ids) - len(agent_data.response_mask)]

        mm_data = {}
        if agent_data.image_data is not None:
            mm_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            mm_data["videos"] = agent_data.video_data

        extra = {
            "turn_scores": agent_data.turn_scores,
            "tool_rewards": agent_data.tool_rewards,
            "turn_segs": agent_data.turn_segs,
        }
        if not agent_data.is_validation:
            extra["training_turn_log"] = agent_data.training_turn_log
        agent_data.extra_fields.update(extra)

        response_logprobs = None
        if agent_data.response_logprobs:
            response_logprobs = agent_data.response_logprobs[: self.response_length]

        return AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=agent_data.response_mask[: self.response_length],
            response_logprobs=response_logprobs,
            multi_modal_data=mm_data,
            reward_score=agent_data.trajectory_reward,
            num_turns=agent_data.user_turns + agent_data.assistant_turns + 1,
            metrics=agent_data.metrics,
            routed_experts=agent_data.routed_experts,
            extra_fields=agent_data.extra_fields,
        )
