"""
HotpotQA PAG agent loop.

PAG (Policy as Generative Verifier) alternates two generations from the same
policy:
    question -> answer attempt -> verifier judgment -> optional revised attempt

The verifier decision, not the hidden ground-truth checker, controls whether the
episode stops. The checker is used only after each generated attempt/judgment to
produce training rewards:
    R_y = 1 if the answer attempt is correct
    R_v = 1 if the verifier correctly judges that attempt

The loop emits token-level rm_scores through extra_fields["pag_token_rewards"] so
solver and verifier generations are both optimized.
"""

import asyncio
import json
import logging
import os
import re
import string
from collections import Counter
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

import numpy as np
import ray
import torch

from verl.experimental.agent_loop.agent_loop import (
    AgentLoopBase,
    AgentLoopManager,
    AgentLoopOutput,
    AgentLoopWorker,
    register,
)
from verl.experimental.agent_loop.tool_parser import FunctionCall, ToolParser
from verl.interactions.base import BaseInteraction
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


class HotpotPAGAgentLoopWorker(AgentLoopWorker):
    """PAG-only worker that converts PAG token rewards after the shared postprocess."""

    @staticmethod
    def _non_tensor_value(non_tensor_batch: dict, key: str, row_idx: int):
        if key not in non_tensor_batch:
            return None
        values = non_tensor_batch[key]
        try:
            return values[row_idx]
        except Exception:
            return None

    def _postprocess(self, inputs, input_non_tensor_batch=None):
        output = super()._postprocess(inputs, input_non_tensor_batch=input_non_tensor_batch)

        response_mask = output.batch["response_mask"]
        attention_mask = output.batch["attention_mask"]
        prompt_length = output.batch["prompts"].size(1)
        rm_scores = (
            output.batch["rm_scores"].clone()
            if "rm_scores" in output.batch.keys()
            else torch.zeros_like(response_mask, dtype=torch.float32)
        )

        updated = False
        for i in range(len(output)):
            agent_name = self._non_tensor_value(output.non_tensor_batch, "agent_name", i)
            data_source = self._non_tensor_value(output.non_tensor_batch, "data_source", i)
            if agent_name != "hotpot_qa_pag_agent" and data_source != "hotpot_pag":
                continue
            if self._non_tensor_value(output.non_tensor_batch, "pag_validate", i):
                continue

            pag_token_rewards = self._non_tensor_value(output.non_tensor_batch, "pag_token_rewards", i)
            if pag_token_rewards is None:
                continue

            actual_response_length = int(attention_mask[i, prompt_length:].sum().item())
            try:
                reward_values = np.asarray(pag_token_rewards, dtype=np.float32).reshape(-1)
            except Exception:
                logger.exception("Failed to parse pag_token_rewards; keeping scalar reward_score rm_scores")
                continue

            if len(reward_values) != actual_response_length:
                logger.warning(
                    "Ignoring pag_token_rewards due to length mismatch: got %d, expected %d",
                    len(reward_values),
                    actual_response_length,
                )
                continue

            reward_tensor = torch.tensor(reward_values, dtype=torch.float32, device=rm_scores.device)
            rm_scores[i].zero_()
            rm_scores[i, :actual_response_length] = (
                reward_tensor * response_mask[i, :actual_response_length].float()
            )
            updated = True

        if updated:
            output.batch["rm_scores"] = rm_scores
        return output


class HotpotPAGAgentLoopManager(AgentLoopManager):
    """AgentLoopManager that scopes dense PAG reward postprocess to PAG jobs."""

    def __init__(self, *args, **kwargs):
        self.agent_loop_workers_class = ray.remote(HotpotPAGAgentLoopWorker)
        super().__init__(*args, **kwargs)


# ---------------------------------------------------------------------------
# Parsing and scoring helpers
# ---------------------------------------------------------------------------

_ATTEMPT_STRICT_RE = re.compile(
    r"<ATTEMPT>.*?Answer\s*:\s*(.+?)(?:\n|</ATTEMPT>|$)",
    re.DOTALL | re.IGNORECASE,
)
_ATTEMPT_LOOSE_RE = re.compile(r"<ATTEMPT>(.*?)(?:</ATTEMPT>|$)", re.DOTALL | re.IGNORECASE)
_FINISHED_STRICT_RE = re.compile(
    r"<FINISHED>.*?Answer\s*:\s*(.+?)(?:\n|</FINISHED>|$)",
    re.DOTALL | re.IGNORECASE,
)
_ANSWER_LINE_RE = re.compile(r"^\s*Answer\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_VERIFY_BLOCK_RE = re.compile(r"<VERIFY>(.*?)(?:</VERIFY>|$)", re.DOTALL | re.IGNORECASE)
_JUDGMENT_RE = re.compile(
    r"Judg(?:e)?ment\s*:\s*(INCORRECT|WRONG|CORRECT|YES|NO|REJECTED|ACCEPTED)\b",
    re.IGNORECASE,
)
_BRACKET_JUDGMENT_RE = re.compile(
    r"\[\[\s*(INCORRECT|WRONG|CORRECT|YES|NO|REJECTED|ACCEPTED)\s*\]\]",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = text.translate(str.maketrans("", "", string.punctuation))
    return " ".join(text.split())


def _token_f1(prediction: str, ground_truth: str) -> float:
    pred_tokens = _normalise(prediction).split()
    gt_tokens = _normalise(ground_truth).split()
    if not pred_tokens or not gt_tokens:
        return 0.0
    common = Counter(pred_tokens) & Counter(gt_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gt_tokens)
    return 2 * precision * recall / (precision + recall)


def _exact_match(prediction: str, ground_truth: str) -> bool:
    return bool(prediction) and _normalise(prediction) == _normalise(ground_truth)


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return re.sub(r"^Answer\s*:\s*", "", line, flags=re.IGNORECASE).strip()
    return ""


def _extract_attempt_answer(text: str) -> str:
    m = _ATTEMPT_STRICT_RE.search(text)
    if m:
        return m.group(1).strip()

    m = _ATTEMPT_LOOSE_RE.search(text)
    if m:
        return _first_non_empty_line(m.group(1))

    # Compatibility with older Hotpot prompts and malformed attempts.
    m = _FINISHED_STRICT_RE.search(text)
    if m:
        return m.group(1).strip()

    m = _ANSWER_LINE_RE.search(text)
    if m:
        return m.group(1).strip()
    return ""


def _has_attempt_answer(text: str) -> bool:
    return bool(_ATTEMPT_STRICT_RE.search(text) or _ATTEMPT_LOOSE_RE.search(text) or _FINISHED_STRICT_RE.search(text))


def _judgment_label_to_bool(label: str) -> bool | None:
    label = label.strip().lower()
    if label in {"correct", "yes", "accepted"}:
        return True
    if label in {"wrong", "incorrect", "no", "rejected"}:
        return False
    return None


def _extract_verifier_decision(text: str) -> bool | None:
    block_match = _VERIFY_BLOCK_RE.search(text)
    block = block_match.group(1) if block_match else text

    for regex in (_JUDGMENT_RE, _BRACKET_JUDGMENT_RE):
        m = regex.search(block)
        if m:
            return _judgment_label_to_bool(m.group(1))

    # Conservative fallback for simple one-line outputs.
    wrong = re.search(r"\b(incorrect|wrong|not\s+correct|rejected|reject)\b", block, re.IGNORECASE)
    correct = re.search(r"\b(correct|right|accepted|accept)\b", block, re.IGNORECASE)
    if wrong and (not correct or wrong.start() <= correct.start()):
        return False
    if correct:
        return True
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _get_ground_truth(kwargs: dict[str, Any], tools_kwargs: dict[str, Any]) -> str:
    reward_model = kwargs.get("reward_model")
    if isinstance(reward_model, dict) and reward_model.get("ground_truth") is not None:
        return str(reward_model.get("ground_truth", "")).strip()

    extra_info = kwargs.get("extra_info")
    if isinstance(extra_info, dict) and extra_info.get("ground_truth") is not None:
        return str(extra_info.get("ground_truth", "")).strip()

    reward_kwargs = tools_kwargs.get("calc_hotpot_reward", {})
    create_kwargs = reward_kwargs.get("create_kwargs", {}) if isinstance(reward_kwargs, dict) else {}
    return str(create_kwargs.get("ground_truth", "")).strip()


def _verifier_prompt(attempt_idx: int) -> str:
    return (
        f"Now switch to VERIFIER for Attempt {attempt_idx}. Decide whether the attempted "
        "answer is correct using only the question, the retrieved evidence, and the "
        "conversation so far. You are not given the ground truth.\n\n"
        "Output exactly this structure:\n"
        "<VERIFY>\n"
        "Judgment: CORRECT or WRONG\n"
        "Reason: one brief sentence\n"
        "</VERIFY>\n\n"
        "If the evidence is insufficient or you are uncertain, choose WRONG."
    )


def _revision_prompt(next_attempt_idx: int) -> str:
    return (
        "Your verifier judged the previous attempt as WRONG. Produce a revised solver "
        f"attempt as Attempt {next_attempt_idx}. You may call Search if more evidence is "
        "needed. End the solver turn with exactly:\n"
        "<ATTEMPT>\n"
        "Answer: [your concise answer]\n"
        "</ATTEMPT>"
    )


class AgentData:
    def __init__(
        self,
        messages: list[dict[str, Any]],
        image_data,
        video_data,
        metrics: dict[str, Any],
        request_id: str,
        tools_kwargs: dict[str, Any],
        ground_truth: str,
        is_validate: bool = False,
        interaction: Optional[BaseInteraction] = None,
        interaction_kwargs: Optional[dict[str, Any]] = None,
    ):
        self.messages = messages
        self.image_data = image_data
        self.video_data = video_data
        self.metrics = metrics
        self.request_id = request_id
        self.tools_kwargs = tools_kwargs
        self.ground_truth = ground_truth
        self.is_validate = is_validate
        self.interaction = interaction
        self.interaction_kwargs = interaction_kwargs or {}

        self.prompt_ids: list[int] = []
        self.response_ids: list[int] = []
        self.response_mask: list[int] = []
        self.response_logprobs: list[float] = []
        self.pag_token_rewards: list[float] = []

        self.turn_scores: list[float] = []
        self.tool_rewards: list[float] = []
        self.turn_segs: list[dict[str, Any]] = []
        self.tool_calls: list[FunctionCall] = []

        self.user_turns = 0
        self.assistant_turns = 0
        self.routed_experts = None
        self.extra_fields: dict[str, Any] = {}

        self.current_role: str | None = None
        self.current_segment_start = 0
        self.current_attempt_answer = ""
        self.current_attempt_text = ""
        self.current_attempt_correct = False
        self.current_attempt_f1 = 0.0
        self.current_verifier_text = ""
        self.current_verifier_decision: bool | None = None

        self.attempt_count = 0
        self.accepted = False
        self.final_answer = ""
        self.final_attempt_text = ""
        self.final_answer_reward = 0.0
        self.answer_reward_sum = 0.0
        self.verifier_reward_sum = 0.0
        self.pag_events: list[dict[str, Any]] = []


class HotpotPAGState(Enum):
    PENDING = "pending"
    ATTEMPT_GENERATING = "attempt_generating"
    PROCESSING_TOOLS = "processing_tools"
    SCORING_ATTEMPT = "scoring_attempt"
    VERIFYING = "verifying"
    SCORING_VERIFIER = "scoring_verifier"
    TERMINATED = "terminated"


@register("hotpot_qa_pag_agent")
class HotpotQAPAGAgentLoop(AgentLoopBase):
    """PAG loop for multi-hop QA with hidden answer/verifier rewards."""

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

        pag_cfg = getattr(mt, "pag", None)
        self.max_attempts = int(getattr(pag_cfg, "max_attempts", 3) if pag_cfg else 3)
        self.answer_reward_weight = float(getattr(pag_cfg, "answer_reward_weight", 1.0) if pag_cfg else 1.0)
        self.verifier_reward_weight = float(getattr(pag_cfg, "verifier_reward_weight", 1.0) if pag_cfg else 1.0)
        self.verifier_tools = _as_bool(getattr(pag_cfg, "verifier_tools", False) if pag_cfg else False)

        tool_config_path = mt.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools = {t.name: t for t in tool_list}
        self._hidden_tool_names = {"calc_hotpot_reward"}

        self.tool_schemas = [
            t.tool_schema.model_dump(exclude_unset=True, exclude_none=True)
            for t in tool_list
            if t.name not in self._hidden_tool_names
        ]

        self.tool_parser = ToolParser.get_tool_parser(mt.format, self.tokenizer)
        self.tool_parser_name = mt.format

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        messages = list(kwargs["raw_prompt"])
        multi_modal_data = await self.process_vision_info(messages)
        images = multi_modal_data.get("images")
        videos = multi_modal_data.get("videos")

        request_id = uuid4().hex
        tools_kwargs = kwargs.get("tools_kwargs", {})
        ground_truth = _get_ground_truth(kwargs, tools_kwargs)
        is_validate = _as_bool(kwargs.get("_agent_loop_validate", False))

        agent_data = AgentData(
            messages=messages,
            image_data=images,
            video_data=videos,
            metrics={},
            request_id=request_id,
            tools_kwargs=tools_kwargs,
            ground_truth=ground_truth,
            is_validate=is_validate,
        )

        reward_tool, reward_instance_id = await self._create_reward_instance(tools_kwargs)

        state = HotpotPAGState.PENDING
        try:
            while state != HotpotPAGState.TERMINATED:
                if state == HotpotPAGState.PENDING:
                    state = await self._handle_pending(agent_data)
                elif state == HotpotPAGState.ATTEMPT_GENERATING:
                    state = await self._handle_generating(agent_data, sampling_params, role="attempt")
                elif state == HotpotPAGState.VERIFYING:
                    state = await self._handle_generating(agent_data, sampling_params, role="verifier")
                elif state == HotpotPAGState.PROCESSING_TOOLS:
                    state = await self._handle_processing_tools(agent_data)
                elif state == HotpotPAGState.SCORING_ATTEMPT:
                    state = await self._handle_scoring_attempt(agent_data, reward_tool, reward_instance_id)
                elif state == HotpotPAGState.SCORING_VERIFIER:
                    state = await self._handle_scoring_verifier(agent_data)
                else:
                    logger.error(f"Unknown state {state}, terminating.")
                    self._close_current_segment(agent_data, 0.0)
                    state = HotpotPAGState.TERMINATED
        finally:
            if reward_tool is not None and reward_instance_id is not None:
                await reward_tool.release(reward_instance_id)

        return self._build_output(agent_data)

    async def _create_reward_instance(self, tools_kwargs: dict[str, Any]) -> tuple[Any, str | None]:
        reward_tool = self.tools.get("calc_hotpot_reward")
        if reward_tool is None:
            return None, None
        rw_kwargs = tools_kwargs.get("calc_hotpot_reward", {})
        reward_instance_id, _ = await reward_tool.create(**rw_kwargs.get("create_kwargs", {}))
        return reward_tool, reward_instance_id

    def _model_tool_schemas(self, role: str) -> list[dict[str, Any]]:
        if role == "verifier" and not self.verifier_tools:
            return []
        return self.tool_schemas

    def _parser_tool_schemas(self, role: str):
        if role == "verifier" and not self.verifier_tools:
            return []
        return [t.tool_schema for t in self.tools.values() if t.name not in self._hidden_tool_names]

    def _start_segment(self, agent_data: AgentData, role: str) -> None:
        agent_data.current_role = role
        agent_data.current_segment_start = sum(agent_data.response_mask)

    def _response_token_count_to_column(self, agent_data: AgentData, token_count: int) -> int | None:
        seen = 0
        for col, mask in enumerate(agent_data.response_mask):
            if mask:
                seen += 1
                if seen == token_count:
                    return col
        return None

    def _close_current_segment(self, agent_data: AgentData, reward: float) -> None:
        role = agent_data.current_role
        if role is None:
            return

        resp_start = agent_data.current_segment_start
        resp_end = sum(agent_data.response_mask)
        if resp_end > resp_start:
            turn = len(agent_data.turn_segs) + 1
            agent_data.turn_segs.append(
                {
                    "turn": turn,
                    "resp_start": resp_start,
                    "resp_end": resp_end,
                    "role": role,
                    "attempt": agent_data.attempt_count,
                    "reward": float(reward),
                }
            )
            reward_col = self._response_token_count_to_column(agent_data, resp_end)
            if reward_col is not None and reward_col < len(agent_data.pag_token_rewards):
                agent_data.pag_token_rewards[reward_col] += float(reward)
            agent_data.turn_scores.append(float(reward))

        agent_data.current_role = None
        agent_data.current_segment_start = sum(agent_data.response_mask)

    async def _handle_pending(self, agent_data: AgentData) -> HotpotPAGState:
        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=self._model_tool_schemas("attempt"),
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        if len(prompt_ids) > self.prompt_length:
            prompt_ids = prompt_ids[-self.prompt_length:]
        agent_data.prompt_ids = prompt_ids
        self._start_segment(agent_data, "attempt")
        return HotpotPAGState.ATTEMPT_GENERATING

    async def _handle_generating(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
        role: str,
    ) -> HotpotPAGState:
        with simple_timer("generate_sequences", agent_data.metrics):
            output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id,
                prompt_ids=agent_data.prompt_ids,
                sampling_params=sampling_params,
                image_data=agent_data.image_data,
                video_data=agent_data.video_data,
            )

        if agent_data.metrics.get("num_preempted") is None:
            agent_data.metrics["num_preempted"] = output.num_preempted if output.num_preempted is not None else -1
        else:
            agent_data.metrics["num_preempted"] += output.num_preempted if output.num_preempted is not None else 0

        if output.extra_fields:
            if not agent_data.extra_fields:
                agent_data.extra_fields.update(output.extra_fields)
            else:
                max_gs = output.extra_fields.get("max_global_steps")
                if max_gs:
                    agent_data.extra_fields["max_global_steps"] = max_gs

        agent_data.assistant_turns += 1
        agent_data.response_ids = output.token_ids
        agent_data.prompt_ids += agent_data.response_ids
        agent_data.response_mask += [1] * len(agent_data.response_ids)
        agent_data.pag_token_rewards += [0.0] * len(agent_data.response_ids)

        if output.log_probs:
            agent_data.response_logprobs += output.log_probs
        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        response_text: str = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(output.token_ids, skip_special_tokens=False),
        )
        agent_data.messages.append({"role": "assistant", "content": response_text})

        if role == "attempt" and _has_attempt_answer(response_text):
            agent_data.current_attempt_text = response_text
            agent_data.current_attempt_answer = _extract_attempt_answer(response_text)
            return HotpotPAGState.SCORING_ATTEMPT

        if role == "verifier":
            decision = _extract_verifier_decision(response_text)
            if decision is not None or not self.verifier_tools or "<VERIFY>" in response_text:
                agent_data.current_verifier_text = response_text
                agent_data.current_verifier_decision = decision
                return HotpotPAGState.SCORING_VERIFIER

        if len(agent_data.response_mask) >= self.response_length:
            self._close_current_segment(agent_data, 0.0)
            return HotpotPAGState.TERMINATED
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            self._close_current_segment(agent_data, 0.0)
            return HotpotPAGState.TERMINATED
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            self._close_current_segment(agent_data, 0.0)
            return HotpotPAGState.TERMINATED

        tool_schemas = self._parser_tool_schemas(role)
        if tool_schemas:
            _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(output.token_ids, tool_schemas)
            if agent_data.tool_calls:
                return HotpotPAGState.PROCESSING_TOOLS

        if role == "attempt":
            # Treat malformed non-tool solver output as an attempt, with reward based
            # on whatever answer can be parsed from it.
            agent_data.current_attempt_text = response_text
            agent_data.current_attempt_answer = _extract_attempt_answer(response_text)
            return HotpotPAGState.SCORING_ATTEMPT

        agent_data.current_verifier_text = response_text
        agent_data.current_verifier_decision = _extract_verifier_decision(response_text)
        return HotpotPAGState.SCORING_VERIFIER

    async def _handle_processing_tools(self, agent_data: AgentData) -> HotpotPAGState:
        role = agent_data.current_role or "attempt"
        tasks = [
            self._call_external_tool(tc, agent_data)
            for tc in agent_data.tool_calls[: self.max_parallel_calls]
            if tc.name not in self._hidden_tool_names
        ]
        if not tasks:
            return HotpotPAGState.VERIFYING if role == "verifier" else HotpotPAGState.ATTEMPT_GENERATING

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)

        add_messages: list[dict[str, Any]] = []
        for tool_response, tool_reward in responses:
            if tool_reward is not None:
                agent_data.tool_rewards.append(tool_reward)
            add_messages.append({"role": "tool", "content": tool_response.text or ""})

        agent_data.messages.extend(add_messages)

        response_ids = await self.apply_chat_template(add_messages, images=None, videos=None, remove_system_prompt=True)
        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            self._close_current_segment(agent_data, 0.0)
            return HotpotPAGState.TERMINATED

        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        agent_data.pag_token_rewards += [0.0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)

        agent_data.user_turns += 1
        return HotpotPAGState.VERIFYING if role == "verifier" else HotpotPAGState.ATTEMPT_GENERATING

    async def _handle_scoring_attempt(
        self,
        agent_data: AgentData,
        reward_tool,
        reward_instance_id: str | None,
    ) -> HotpotPAGState:
        agent_data.attempt_count += 1
        answer = agent_data.current_attempt_answer
        correct, f1 = await self._score_answer(agent_data, reward_tool, reward_instance_id, answer)
        raw_reward = 1.0 if correct else 0.0
        reward = self.answer_reward_weight * raw_reward

        agent_data.current_attempt_correct = correct
        agent_data.current_attempt_f1 = f1
        agent_data.final_answer = answer
        agent_data.final_attempt_text = agent_data.current_attempt_text
        agent_data.final_answer_reward = raw_reward
        agent_data.answer_reward_sum += reward
        agent_data.tool_rewards.append(raw_reward)

        self._close_current_segment(agent_data, reward)
        agent_data.pag_events.append(
            {
                "role": "attempt",
                "attempt": agent_data.attempt_count,
                "answer": answer,
                "correct": bool(correct),
                "reward": raw_reward,
                "f1": float(f1),
            }
        )

        if not await self._append_context_message(agent_data, _verifier_prompt(agent_data.attempt_count)):
            return HotpotPAGState.TERMINATED

        self._start_segment(agent_data, "verifier")
        return HotpotPAGState.VERIFYING

    async def _handle_scoring_verifier(self, agent_data: AgentData) -> HotpotPAGState:
        decision = agent_data.current_verifier_decision
        verifier_correct = decision is not None and bool(decision) == bool(agent_data.current_attempt_correct)
        raw_reward = 1.0 if verifier_correct else 0.0
        reward = self.verifier_reward_weight * raw_reward

        agent_data.verifier_reward_sum += reward
        agent_data.tool_rewards.append(raw_reward)
        self._close_current_segment(agent_data, reward)

        accepted = decision is True
        agent_data.accepted = accepted
        agent_data.pag_events.append(
            {
                "role": "verifier",
                "attempt": agent_data.attempt_count,
                "decision": "CORRECT" if decision is True else "WRONG" if decision is False else "UNPARSED",
                "attempt_correct": bool(agent_data.current_attempt_correct),
                "verifier_correct": bool(verifier_correct),
                "reward": raw_reward,
            }
        )

        if accepted or agent_data.attempt_count >= self.max_attempts:
            return HotpotPAGState.TERMINATED

        if not await self._append_context_message(agent_data, _revision_prompt(agent_data.attempt_count + 1)):
            return HotpotPAGState.TERMINATED

        agent_data.current_attempt_answer = ""
        agent_data.current_attempt_text = ""
        agent_data.current_verifier_text = ""
        agent_data.current_verifier_decision = None
        self._start_segment(agent_data, "attempt")
        return HotpotPAGState.ATTEMPT_GENERATING

    async def _score_answer(self, agent_data: AgentData, reward_tool, reward_instance_id: str | None, answer: str) -> tuple[bool, float]:
        if reward_tool is not None and reward_instance_id is not None:
            try:
                _tool_response, _tool_reward, extra = await reward_tool.execute(reward_instance_id, {"answer": answer})
                return bool(extra.get("em", False)), float(extra.get("f1", 0.0))
            except Exception as e:
                logger.warning(f"PAG answer scoring failed for request {agent_data.request_id}: {e}")

        correct = _exact_match(answer, agent_data.ground_truth)
        return correct, _token_f1(answer, agent_data.ground_truth)

    async def _append_context_message(self, agent_data: AgentData, content: str) -> bool:
        message = {"role": "user", "content": content}
        response_ids = await self.apply_chat_template([message], images=None, videos=None, remove_system_prompt=True)
        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            return False

        agent_data.messages.append(message)
        agent_data.prompt_ids += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        agent_data.pag_token_rewards += [0.0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)
        agent_data.user_turns += 1
        return True

    def _validation_score(self, agent_data: AgentData) -> tuple[float, str, float]:
        match = _ATTEMPT_STRICT_RE.search(agent_data.final_attempt_text or "")
        prediction = match.group(1).strip() if match else ""
        if not prediction:
            return -0.1, "", 0.0
        return (1.0 if _exact_match(prediction, agent_data.ground_truth) else 0.0), prediction, 1.0

    def _build_output(self, agent_data: AgentData) -> AgentLoopOutput:
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask):]
        prompt_ids = agent_data.prompt_ids[: len(agent_data.prompt_ids) - len(agent_data.response_mask)]

        mm_data = {}
        if agent_data.image_data is not None:
            mm_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            mm_data["videos"] = agent_data.video_data

        train_reward = float(sum(agent_data.pag_token_rewards[: self.response_length]))
        validation_score, validation_prediction, validation_format_ok = self._validation_score(agent_data)
        reward_score = validation_score if agent_data.is_validate else train_reward
        metric_acc = validation_score if agent_data.is_validate else float(agent_data.final_answer_reward)
        prediction = validation_prediction if agent_data.is_validate else agent_data.final_answer
        reward_extra_info = {
            "score": reward_score,
            "acc": metric_acc,
            "format_ok": validation_format_ok if agent_data.is_validate else 1.0,
            "pag_train_score": train_reward,
            "pag_validate": int(agent_data.is_validate),
            "pag_answer_reward_sum": float(agent_data.answer_reward_sum),
            "pag_verifier_reward_sum": float(agent_data.verifier_reward_sum),
            "pag_attempts": int(agent_data.attempt_count),
            "pag_accepted": int(agent_data.accepted),
            "pag_final_answer_reward": float(agent_data.final_answer_reward),
            "prediction": prediction,
            "ground_truth": agent_data.ground_truth,
        }

        output = AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=agent_data.response_mask[: self.response_length],
            multi_modal_data=mm_data,
            response_logprobs=(
                agent_data.response_logprobs[: self.response_length]
                if agent_data.response_logprobs
                else None
            ),
            reward_score=reward_score,
            num_turns=agent_data.user_turns + agent_data.assistant_turns + 1,
            metrics=agent_data.metrics,
            routed_experts=agent_data.routed_experts,
            extra_fields=agent_data.extra_fields,
        )

        extra = {
            "turn_scores": agent_data.turn_scores,
            "tool_rewards": agent_data.tool_rewards,
            "turn_segs": agent_data.turn_segs,
            "pag_token_rewards": agent_data.pag_token_rewards[: self.response_length],
            "pag_events": agent_data.pag_events,
            "pag_final_answer": agent_data.final_answer,
            "pag_validate": bool(agent_data.is_validate),
            "reward_extra_info": reward_extra_info,
        }
        output.extra_fields.update(extra)
        return output

    async def _call_external_tool(
        self,
        tool_call: FunctionCall,
        agent_data: AgentData,
    ) -> tuple[ToolResponse, float | None]:
        tool_name = tool_call.name
        tool = self.tools.get(tool_name)
        if tool is None:
            return (
                ToolResponse(text=f"Unknown tool: {tool_name}. Available tools: {list(self.tools.keys())}"),
                None,
            )

        instance_id = None
        try:
            tool_args = json.loads(tool_call.arguments)
            kwargs = agent_data.tools_kwargs.get(tool_name, {})
            instance_id, _ = await tool.create(create_kwargs=kwargs.get("create_kwargs", {}))
            tool_response, tool_reward, _ = await tool.execute(instance_id, tool_args, agent_data=agent_data)
        except Exception as e:
            logger.warning(f"External tool '{tool_name}' error: {e}")
            return ToolResponse(text=f"Tool call failed ({tool_name}): {e}"), None
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
