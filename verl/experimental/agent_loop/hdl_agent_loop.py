"""HDL agent loop for iterative RTL/Chisel generation and local judging."""

from __future__ import annotations

import json
import logging
import os
import re
import asyncio
from enum import Enum
from typing import Any
from uuid import uuid4

import torch
from PIL import Image

from recipe.hdl_agent.hdl_judge import compute_hdl_score
from verl.experimental.agent_loop.agent_loop import AgentLoopBase, AgentLoopOutput, register
from verl.experimental.agent_loop.tool_parser import FunctionCall, ToolParser
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _load_json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def _task_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    candidate = kwargs.get("reward_model", {}).get("ground_truth") if isinstance(kwargs.get("reward_model"), dict) else None
    task = _load_json_maybe(candidate)
    if isinstance(task, dict):
        return task
    extra_info = kwargs.get("extra_info", {}) or {}
    task = _load_json_maybe(extra_info.get("hdl_task"))
    if isinstance(task, dict):
        return task
    meta = _load_json_maybe(extra_info.get("meta_json"))
    if isinstance(meta, dict) and isinstance(meta.get("hdl_task"), dict):
        return meta["hdl_task"]
    raise ValueError("HDL agent loop requires reward_model.ground_truth or extra_info.hdl_task to be a task dict")


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max(0, (max_chars - 80) // 2)
    return text[:head] + "\n...(truncated)...\n" + text[-head:]


def _score_passed(value: Any, threshold: float = 0.999) -> bool:
    try:
        return float(value) >= threshold
    except (TypeError, ValueError):
        return False


def _tool_result_passed(tool_name: str, result: dict[str, Any], tool_reward: float) -> bool:
    if tool_name == "compile_hdl":
        lint_ok = result.get("hdl_lint_ok")
        return _score_passed(result.get("hdl_slang_ok")) and (lint_ok is None or _score_passed(lint_ok))
    if tool_name == "simulate_hdl":
        return _score_passed(result.get("hdl_functional_sim_ok"))
    if tool_name == "synthesize_hdl":
        return _score_passed(result.get("hdl_synth_ok"))
    return _score_passed(tool_reward)


class HDLAgentState(Enum):
    PENDING = "pending"
    GENERATING = "generating"
    JUDGING = "judging"
    TERMINATED = "terminated"


class AgentData:
    def __init__(
        self,
        messages: list[dict[str, Any]],
        image_data: list[Image.Image] | None,
        video_data: list[tuple[torch.Tensor, dict[str, Any]]] | None,
        metrics: dict[str, Any],
        request_id: str,
        task: dict[str, Any],
        extra_info: dict[str, Any],
        is_validation: bool,
    ):
        self.messages = messages
        self.image_data = image_data
        self.video_data = video_data
        self.metrics = metrics
        self.request_id = request_id
        self.task = task
        self.extra_info = extra_info
        self.is_validation = is_validation

        self.prompt_ids: list[int] = []
        self.response_ids: list[int] = []
        self.response_mask: list[int] = []
        self.response_logprobs: list[float] = []

        self.user_turns = 0
        self.assistant_turns = 0

        self.turn_scores: list[float] = []
        self.judge_feedbacks: list[str] = []
        self.judge_results: list[dict[str, Any]] = []
        self.latest_solution_text: str = ""
        self.final_judge_result: dict[str, Any] = {}
        self.final_score: float = -0.1
        self.tool_calls: list[FunctionCall] = []
        self.tool_rewards: list[float] = []
        self.tool_call_names: list[str] = []
        self.tool_results: list[dict[str, Any]] = []
        self.reward_score: float = -0.1

        self.routed_experts = None
        self.extra_fields: dict[str, Any] = {
            "validation_mode": is_validation,
            "attempt_scores": [],
            "judge_feedbacks": [],
            "judge_results": [],
            "tool_rewards": [],
            "tool_call_names": [],
            "tool_results": [],
            "num_hdl_tool_calls": 0,
            "used_compile_hdl": False,
            "used_simulate_hdl": False,
            "used_synthesize_hdl": False,
            "successful_compile_hdl": False,
            "successful_simulate_hdl": False,
            "successful_functional_simulate_hdl": False,
            "successful_auto_smoke_simulate_hdl": False,
            "successful_synthesize_hdl": False,
            "final_score": -0.1,
            "reward_score": -0.1,
            "reward_bonus": 0.0,
            "final_pass": False,
            "termination_reason": "",
            "task_language": str(task.get("language") or "systemverilog"),
            "task_name": str(task.get("name") or task.get("task_id") or "hdl_task"),
        }


@register("hdl_agent")
class HDLAgentLoop(AgentLoopBase):
    """Multi-turn HDL rollout loop with judge-generated feedback."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mt = self.rollout_config.multi_turn
        self.max_rounds = int(getattr(mt, "hdl_max_rounds", None) or os.environ.get("HDL_AGENT_MAX_ROUNDS", 2) or 1)
        self.pass_score_threshold = float(
            getattr(mt, "hdl_pass_score_threshold", None)
            or os.environ.get("HDL_AGENT_PASS_SCORE_THRESHOLD", 0.999)
        )
        self.judge_timeout = int(getattr(mt, "hdl_judge_timeout", None) or os.environ.get("HDL_AGENT_TIMEOUT", 30) or 30)
        self.feedback_max_chars = int(
            getattr(mt, "hdl_feedback_max_chars", None) or os.environ.get("HDL_AGENT_FEEDBACK_MAX_CHARS", 5000) or 5000
        )
        self.keep_judge_work = _as_bool(
            getattr(mt, "hdl_keep_judge_work", None), _as_bool(os.environ.get("HDL_AGENT_KEEP_WORK"), False)
        )
        self.hdl_env_sh = getattr(mt, "hdl_env_sh", None) or os.environ.get("HDL_ENV_SH")
        self.simulate_tool_bonus = float(getattr(mt, "hdl_simulate_tool_bonus", 0.03) or 0.0)
        self.synthesize_tool_bonus = float(getattr(mt, "hdl_synthesize_tool_bonus", 0.02) or 0.0)
        self.tool_bonus_min_final_score = float(getattr(mt, "hdl_tool_bonus_min_final_score", 0.05) or 0.0)
        self.reward_cap = float(getattr(mt, "hdl_reward_cap", 1.05) or 1.0)
        self.max_assistant_turns = mt.max_assistant_turns
        self.max_user_turns = mt.max_user_turns
        self.max_parallel_calls = mt.max_parallel_calls
        self.max_tool_response_length = mt.max_tool_response_length
        self.tool_response_truncate_side = mt.tool_response_truncate_side
        tool_config_path = mt.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools = {tool.name: tool for tool in tool_list}
        self.tool_schemas = [tool.tool_schema.model_dump(exclude_unset=True, exclude_none=True) for tool in tool_list]
        self.tool_parser = ToolParser.get_tool_parser(mt.format, self.tokenizer)
        self.prompt_length = self.rollout_config.prompt_length
        self.response_length = self.rollout_config.response_length
        configured_sequence_length = self.prompt_length + self.response_length
        model_context_length = self._infer_model_context_length()
        self.sequence_length_limit = (
            min(configured_sequence_length, model_context_length)
            if model_context_length is not None
            else configured_sequence_length
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
        return self._find_max_position_embeddings(model_config)

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

    def _remaining_generation_tokens(self, agent_data: AgentData) -> int:
        response_remaining = self.response_length - len(agent_data.response_mask)
        sequence_remaining = self.sequence_length_limit - len(agent_data.prompt_ids)
        return min(response_remaining, sequence_remaining)

    def _would_leave_no_generation_budget(self, agent_data: AgentData, token_count: int) -> bool:
        return (
            len(agent_data.response_mask) + token_count >= self.response_length
            or len(agent_data.prompt_ids) + token_count >= self.sequence_length_limit
        )

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

    def _termination_reason(self, score: float, attempt_index: int, max_rounds: int) -> str:
        if score >= self.pass_score_threshold:
            return "judge_passed"
        if attempt_index >= max_rounds:
            return "max_rounds_reached"
        return "judge_requested_revision"

    def _judge_feedback_message(self, judge_result: dict[str, Any]) -> str:
        feedback = str(judge_result.get("hdl_feedback") or "")
        score = float(judge_result.get("score", 0.0))
        return (
            "HDL judge feedback:\n"
            f"score: {score:.3f}\n\n"
            f"{feedback}\n\n"
            "Revise the design and return a complete corrected solution only."
        )

    async def _append_feedback(self, agent_data: AgentData, feedback: str) -> tuple[list[int], bool]:
        add_messages = [{"role": "user", "content": feedback}]
        agent_data.messages.extend(add_messages)
        feedback_ids = await self.apply_chat_template(add_messages, remove_system_prompt=True)
        if self._would_leave_no_generation_budget(agent_data, len(feedback_ids)):
            return [], False
        agent_data.prompt_ids += feedback_ids
        agent_data.response_mask += [0] * len(feedback_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(feedback_ids)
        agent_data.user_turns += 1
        return feedback_ids, True

    async def _call_tool(self, tool_call: FunctionCall, agent_data: AgentData) -> tuple[ToolResponse, float, dict]:
        tool = None
        instance_id = None
        try:
            tool = self.tools[tool_call.name]
            tool_args = json.loads(tool_call.arguments)
            instance_id, _ = await tool.create()
            tool_response, tool_reward, result = await tool.execute(instance_id, tool_args, agent_data=agent_data)
            return tool_response, float(tool_reward or 0.0), result or {}
        except Exception as exc:
            logger.warning("Error executing HDL tool %s: %s", getattr(tool_call, "name", ""), exc)
            return ToolResponse(text=f"HDL tool execution failed: {type(exc).__name__}: {exc}"), 0.0, {}
        finally:
            if tool is not None and instance_id is not None:
                await tool.release(instance_id)

    def _record_tool_call(
        self,
        agent_data: AgentData,
        tool_name: str,
        tool_reward: float,
        result: dict[str, Any],
    ) -> None:
        normalized = str(tool_name or "")
        result = dict(result or {})
        result["_tool_name"] = normalized
        result["_tool_reward"] = float(tool_reward or 0.0)

        agent_data.tool_call_names.append(normalized)
        agent_data.tool_results.append(result)
        agent_data.extra_fields["tool_call_names"] = list(agent_data.tool_call_names)
        agent_data.extra_fields["tool_results"] = list(agent_data.tool_results)
        agent_data.extra_fields["num_hdl_tool_calls"] = len(agent_data.tool_call_names)

        if normalized in {"compile_hdl", "simulate_hdl", "synthesize_hdl"}:
            agent_data.extra_fields[f"used_{normalized}"] = True
            if normalized == "simulate_hdl":
                if _score_passed(result.get("hdl_functional_sim_ok")):
                    agent_data.extra_fields["successful_functional_simulate_hdl"] = True
                if _score_passed(result.get("hdl_auto_smoke_sim_ok")):
                    agent_data.extra_fields["successful_auto_smoke_simulate_hdl"] = True
            if _tool_result_passed(normalized, result, tool_reward):
                agent_data.extra_fields[f"successful_{normalized}"] = True

    def _shaped_reward(self, final_score: float, agent_data: AgentData) -> tuple[float, float]:
        if final_score < self.tool_bonus_min_final_score:
            return final_score, 0.0

        bonus = 0.0
        if agent_data.extra_fields.get("successful_simulate_hdl"):
            bonus += self.simulate_tool_bonus
        if agent_data.extra_fields.get("successful_synthesize_hdl"):
            bonus += self.synthesize_tool_bonus

        if bonus <= 0.0:
            return final_score, 0.0
        return min(self.reward_cap, final_score + bonus), bonus

    async def _append_tool_responses(self, agent_data: AgentData) -> tuple[list[int], bool]:
        tool_calls = agent_data.tool_calls[: self.max_parallel_calls]
        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*(self._call_tool(tool_call, agent_data) for tool_call in tool_calls))

        add_messages: list[dict[str, Any]] = []
        for tool_call, (tool_response, tool_reward, result) in zip(tool_calls, responses, strict=False):
            text = tool_response.text or ""
            if len(text) > self.max_tool_response_length:
                if self.tool_response_truncate_side == "left":
                    text = text[: self.max_tool_response_length] + "...(truncated)"
                elif self.tool_response_truncate_side == "right":
                    text = "(truncated)..." + text[-self.max_tool_response_length :]
                else:
                    half = self.max_tool_response_length // 2
                    text = text[:half] + "...(truncated)..." + text[-half:]
            add_messages.append({"role": "tool", "content": text})
            agent_data.tool_rewards.append(tool_reward)
            agent_data.judge_results.append(result)
            self._record_tool_call(agent_data, tool_call.name, tool_reward, result)

        agent_data.messages.extend(add_messages)
        response_ids = await self.apply_chat_template(add_messages, remove_system_prompt=True)
        if self._would_leave_no_generation_budget(agent_data, len(response_ids)):
            return [], False
        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)
        agent_data.user_turns += len(add_messages)
        agent_data.extra_fields["tool_rewards"] = list(agent_data.tool_rewards)
        agent_data.extra_fields["judge_results"] = list(agent_data.judge_results)
        return response_ids, True

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        messages = list(kwargs["raw_prompt"])
        multi_modal_data = await self.process_vision_info(messages)

        task = _task_from_kwargs(kwargs)
        is_validation = _as_bool(kwargs.get("_agent_loop_validate", False))

        agent_data = AgentData(
            messages=messages,
            image_data=multi_modal_data.get("images"),
            video_data=multi_modal_data.get("videos"),
            metrics={},
            request_id=uuid4().hex,
            task=task,
            extra_info=kwargs.get("extra_info", {}) or {},
            is_validation=is_validation,
        )

        state = HDLAgentState.PENDING
        attempt_index = 0
        while state != HDLAgentState.TERMINATED:
            if state == HDLAgentState.PENDING:
                agent_data.prompt_ids = await self.apply_chat_template(
                    agent_data.messages,
                    tools=self.tool_schemas,
                    images=agent_data.image_data,
                    videos=agent_data.video_data,
                )
                state = HDLAgentState.GENERATING
            elif state == HDLAgentState.GENERATING:
                generation_budget = self._remaining_generation_tokens(agent_data)
                if generation_budget <= 0:
                    agent_data.extra_fields["termination_reason"] = "generation_budget_exhausted"
                    state = HDLAgentState.TERMINATED
                    continue

                turn_sampling_params = self._sampling_params_with_generation_budget(sampling_params, generation_budget)
                with simple_timer("generate_sequences", agent_data.metrics):
                    output: TokenOutput = await self.server_manager.generate(
                        request_id=agent_data.request_id,
                        prompt_ids=agent_data.prompt_ids,
                        sampling_params=turn_sampling_params,
                        image_data=agent_data.image_data,
                        video_data=agent_data.video_data,
                    )

                if agent_data.metrics.get("num_preempted") is None:
                    agent_data.metrics["num_preempted"] = output.num_preempted if output.num_preempted is not None else -1
                else:
                    agent_data.metrics["num_preempted"] += output.num_preempted if output.num_preempted is not None else 0

                agent_data.assistant_turns += 1
                agent_data.latest_solution_text = self.tokenizer.decode(output.token_ids, skip_special_tokens=True)
                agent_data.prompt_ids += output.token_ids
                agent_data.response_ids = output.token_ids
                agent_data.response_mask += [1] * len(output.token_ids)
                if output.log_probs:
                    agent_data.response_logprobs += output.log_probs
                if output.routed_experts is not None:
                    agent_data.routed_experts = output.routed_experts

                if len(output.token_ids) == 0:
                    agent_data.final_judge_result = {
                        "score": -0.1,
                        "hdl_feedback": "The model produced an empty answer.",
                    }
                    agent_data.final_score = -0.1
                    agent_data.turn_scores.append(-0.1)
                    agent_data.extra_fields["attempt_scores"] = agent_data.turn_scores
                    agent_data.extra_fields["judge_feedbacks"] = agent_data.judge_feedbacks
                    agent_data.extra_fields["judge_results"] = agent_data.judge_results
                    agent_data.extra_fields["final_score"] = agent_data.final_score
                    agent_data.reward_score = agent_data.final_score
                    agent_data.extra_fields["reward_score"] = agent_data.reward_score
                    agent_data.extra_fields["final_pass"] = False
                    agent_data.extra_fields["termination_reason"] = "empty_assistant_generation"
                    state = HDLAgentState.TERMINATED
                    continue

                _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(output.token_ids, [tool.tool_schema for tool in self.tools.values()])
                if agent_data.tool_calls:
                    tool_ids, ok = await self._append_tool_responses(agent_data)
                    agent_data.extra_fields["last_tool_response_tokens"] = len(tool_ids)
                    if not ok:
                        agent_data.extra_fields["termination_reason"] = "response_length_limit_before_tool_response_append"
                        state = HDLAgentState.TERMINATED
                        continue
                    if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
                        agent_data.extra_fields["termination_reason"] = "max_assistant_turns_after_tool_call"
                        state = HDLAgentState.JUDGING
                    else:
                        state = HDLAgentState.GENERATING
                    continue

                state = HDLAgentState.JUDGING
            elif state == HDLAgentState.JUDGING:
                judge_result = compute_hdl_score(
                    solution_str=agent_data.latest_solution_text,
                    ground_truth=agent_data.task,
                    extra_info=agent_data.extra_info,
                    env_sh=self.hdl_env_sh,
                    timeout=self.judge_timeout,
                    feedback_max_chars=self.feedback_max_chars,
                    keep_work=self.keep_judge_work,
                )
                score = float(judge_result.get("score", -0.1))
                agent_data.final_judge_result = dict(judge_result)
                agent_data.final_score = score
                agent_data.turn_scores.append(score)
                agent_data.judge_results.append(judge_result)
                feedback = _truncate_text(self._judge_feedback_message(judge_result), self.feedback_max_chars)
                agent_data.judge_feedbacks.append(feedback)
                agent_data.extra_fields["attempt_scores"] = list(agent_data.turn_scores)
                agent_data.extra_fields["judge_feedbacks"] = list(agent_data.judge_feedbacks)
                agent_data.extra_fields["judge_results"] = list(agent_data.judge_results)
                agent_data.extra_fields["final_score"] = score
                agent_data.reward_score, reward_bonus = self._shaped_reward(score, agent_data)
                agent_data.extra_fields["reward_score"] = agent_data.reward_score
                agent_data.extra_fields["reward_bonus"] = reward_bonus
                agent_data.extra_fields["final_pass"] = bool(score >= self.pass_score_threshold)
                agent_data.extra_fields["latest_solution_chars"] = len(agent_data.latest_solution_text)
                agent_data.extra_fields["last_generation_tokens"] = len(output.token_ids) if "output" in locals() else 0

                attempt_index += 1
                if score >= self.pass_score_threshold or attempt_index >= self.max_rounds:
                    agent_data.extra_fields["termination_reason"] = self._termination_reason(
                        score, attempt_index, self.max_rounds
                    )
                    state = HDLAgentState.TERMINATED
                    continue

                feedback_ids, ok = await self._append_feedback(agent_data, feedback)
                if not ok:
                    agent_data.extra_fields["termination_reason"] = "response_length_limit_before_feedback_append"
                    state = HDLAgentState.TERMINATED
                    continue
                agent_data.extra_fields["last_feedback_tokens"] = len(feedback_ids)
                state = HDLAgentState.GENERATING
            else:
                agent_data.extra_fields["termination_reason"] = "unknown_state"
                state = HDLAgentState.TERMINATED

        prompt_ids = agent_data.prompt_ids[: len(agent_data.prompt_ids) - len(agent_data.response_mask)]
        response_ids = agent_data.prompt_ids[len(prompt_ids) :]
        output = AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=agent_data.response_mask[: self.response_length],
            response_logprobs=agent_data.response_logprobs[: self.response_length] if agent_data.response_logprobs else None,
            routed_experts=agent_data.routed_experts,
            multi_modal_data=multi_modal_data,
            reward_score=agent_data.reward_score,
            num_turns=agent_data.user_turns + agent_data.assistant_turns + 1,
            metrics=agent_data.metrics,
            extra_fields=agent_data.extra_fields,
        )
        return output
