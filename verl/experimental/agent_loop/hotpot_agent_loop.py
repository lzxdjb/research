"""
HotpotQA multi-turn RL agent loop.

State machine:
    PENDING  →  GENERATING  →  PROCESSING_TOOLS  →  GENERATING  → … → SCORING → TERMINATED
                            ↘  SCORING (on <FINISHED>)               ↗
                            ↘  TERMINATED (hard limits / no tool)    ↗
"""

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
from verl.interactions.base import BaseInteraction
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))

# ── Regex helpers ─────────────────────────────────────────────────────────────

_FINISHED_STRICT_RE = re.compile(
    r"<FINISHED>.*?Answer\s*:\s*(.+?)(?:\n|</FINISHED>|$)",
    re.DOTALL | re.IGNORECASE,
)
_FINISHED_LOOSE_RE = re.compile(r"<FINISHED>(.*)", re.DOTALL)


def _extract_final_answer(text: str) -> str:
    """Pull the answer string from the model's <FINISHED> block.

    Expected format:
        <FINISHED>
        Answer: Jonathan Stark
        </FINISHED>
    Falls back to grabbing the first non-empty line after <FINISHED>.
    """
    m = _FINISHED_STRICT_RE.search(text)
    if m:
        return m.group(1).strip()
    # Loose fallback: first non-empty line inside the FINISHED block
    m = _FINISHED_LOOSE_RE.search(text)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line:
                # Strip a leading "Answer:" prefix if present
                line = re.sub(r"^Answer\s*:\s*", "", line, flags=re.IGNORECASE)
                return line
    return ""


# ── Agent state ───────────────────────────────────────────────────────────────

class AgentData:
    """All mutable state for one rollout episode."""

    def __init__(
        self,
        messages: list[dict[str, Any]],
        image_data,
        video_data,
        metrics: dict[str, Any],
        request_id: str,
        tools_kwargs: dict[str, Any],
        interaction: Optional[BaseInteraction] = None,
        interaction_kwargs: Optional[dict[str, Any]] = None,
    ):
        self.messages           = messages
        self.image_data         = image_data
        self.video_data         = video_data
        self.metrics            = metrics
        self.request_id         = request_id
        self.tools_kwargs       = tools_kwargs
        self.interaction        = interaction
        self.interaction_kwargs = interaction_kwargs or {}

        # Token bookkeeping
        self.prompt_ids:       list[int]   = []
        self.response_ids:     list[int]   = []
        self.response_mask:    list[int]   = []
        self.response_logprobs: list[float] = []

        # Reward / scoring bookkeeping
        self.turn_scores:  list[float] = []
        self.tool_rewards: list[float] = []

        # Turn counters
        self.user_turns      = 0
        self.assistant_turns = 0

        # Per-turn segment index (required by SEEUpo / turn-level credit assignment)
        self.turn_segs: list[dict] = []

        # Tool calls parsed from the latest assistant turn
        self.tool_calls: list[FunctionCall] = []

        self.routed_experts = None
        self.extra_fields: dict[str, Any] = {}


class HotpotAgentState(Enum):
    PENDING          = "pending"
    GENERATING       = "generating"
    PROCESSING_TOOLS = "processing_tools"
    SCORING          = "scoring"
    TERMINATED       = "terminated"


# ── Agent loop ────────────────────────────────────────────────────────────────

@register("hotpot_qa_agent")
class HotpotQAAgentLoop(AgentLoopBase):
    """
    Multi-turn RL agent loop for HotpotQA.

    The model iteratively calls the Search tool to gather evidence, then
    emits <FINISHED>答案：…</FINISHED> when confident.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        mt = self.rollout_config.multi_turn
        self.max_user_turns              = mt.max_user_turns
        self.max_assistant_turns         = mt.max_assistant_turns
        self.max_parallel_calls          = mt.max_parallel_calls
        self.max_tool_response_length    = mt.max_tool_response_length
        self.tool_response_truncate_side = mt.tool_response_truncate_side
        self.prompt_length               = self.rollout_config.prompt_length
        self.response_length             = self.rollout_config.response_length

        tool_config_path = mt.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools = {t.name: t for t in tool_list}

        # Tool schemas exposed to the model (exclude reward tool)
        self.tool_schemas = [
            t.tool_schema.model_dump(exclude_unset=True, exclude_none=True)
            for t in tool_list
            if t.name != "calc_hotpot_reward"
        ]

        self.tool_parser      = ToolParser.get_tool_parser(mt.format, self.tokenizer)
        self.tool_parser_name = mt.format

    # ------------------------------------------------------------------
    # Debug helper
    # ------------------------------------------------------------------

    def _log_turn_debug(self, agent_data, turn_type, prompt_ids,
                        response_ids=None, tool_calls=None, tool_responses=None):
        sep = "=" * 80
        lines = []
        if response_ids is not None:
            response_text = self.tokenizer.decode(response_ids, skip_special_tokens=False)
            lines += [f"--- RESPONSE ({len(response_ids)} tokens) ---", response_text]
        if tool_calls:
            lines += ["--- TOOL CALLS ---",
                      *[f"  [{i}] {tc.name}({tc.arguments})"
                        for i, tc in enumerate(tool_calls)]]
        if tool_responses:
            lines += ["--- TOOL RESPONSES ---",
                      *[f"  [{i}] {r}" for i, r in enumerate(tool_responses)]]
        if lines:
            lines.append(sep)
            print("\n".join(lines), flush=True)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        messages = list(kwargs["raw_prompt"])

        multi_modal_data = await self.process_vision_info(messages)
        images = multi_modal_data.get("images")
        videos = multi_modal_data.get("videos")

        request_id   = uuid4().hex
        tools_kwargs = kwargs.get("tools_kwargs", {})

        agent_data = AgentData(
            messages=messages,
            image_data=images,
            video_data=videos,
            metrics={},
            request_id=request_id,
            tools_kwargs=tools_kwargs,
        )

        # Initialise reward tool instance
        reward_tool        = self.tools.get("calc_hotpot_reward")
        reward_instance_id = None
        if reward_tool is not None:
            rw_kwargs = tools_kwargs.get("calc_hotpot_reward", {})
            reward_instance_id, _ = await reward_tool.create(
                **rw_kwargs.get("create_kwargs", {})
            )

        # Run state machine
        state = HotpotAgentState.PENDING
        while state != HotpotAgentState.TERMINATED:
            if state == HotpotAgentState.PENDING:
                state = await self._handle_pending(agent_data, sampling_params)
            elif state == HotpotAgentState.GENERATING:
                state = await self._handle_generating(
                    agent_data, sampling_params, reward_instance_id
                )
            elif state == HotpotAgentState.PROCESSING_TOOLS:
                state = await self._handle_processing_tools(agent_data)
            elif state == HotpotAgentState.SCORING:
                state = await self._handle_scoring(
                    agent_data, reward_tool, reward_instance_id
                )
            else:
                logger.error(f"Unknown state {state}, terminating.")
                state = HotpotAgentState.TERMINATED

        # Release reward tool
        if reward_tool is not None and reward_instance_id is not None:
            await reward_tool.release(reward_instance_id)

        # Build output
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask):]
        prompt_ids   = agent_data.prompt_ids[: len(agent_data.prompt_ids)
                                               - len(agent_data.response_mask)]

        mm_data = {}
        if agent_data.image_data is not None:
            mm_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            mm_data["videos"] = agent_data.video_data

        output = AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=agent_data.response_mask[: self.response_length],
            multi_modal_data=mm_data,
            response_logprobs=(
                agent_data.response_logprobs[: self.response_length]
                if agent_data.response_logprobs else None
            ),
            num_turns=agent_data.user_turns + agent_data.assistant_turns + 1,
            metrics=agent_data.metrics,
            routed_experts=agent_data.routed_experts,
            extra_fields=agent_data.extra_fields,
        )

        use_seeupo = getattr(self.rollout_config.multi_turn, "use_seeupo", False)
        extra = {
            "turn_scores":  agent_data.turn_scores,
            "tool_rewards": agent_data.tool_rewards,
        }
        if use_seeupo:
            extra["turn_segs"] = agent_data.turn_segs
        output.extra_fields.update(extra)
        return output

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    async def _handle_pending(
        self, agent_data: AgentData, sampling_params: dict[str, Any]
    ) -> HotpotAgentState:
        """Tokenise the initial system + user prompt."""
        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=self.tool_schemas,
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        agent_data.prompt_ids = prompt_ids
        return HotpotAgentState.GENERATING

    async def _handle_generating(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
        reward_instance_id: str | None,
    ) -> HotpotAgentState:
        """Run the LLM for one turn and decide next state."""
        with simple_timer("generate_sequences", agent_data.metrics):
            output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id,
                prompt_ids=agent_data.prompt_ids,
                sampling_params=sampling_params,
                image_data=agent_data.image_data,
                video_data=agent_data.video_data,
            )

        self._log_turn_debug(
            agent_data,
            turn_type="ASSISTANT_GENERATE",
            prompt_ids=agent_data.prompt_ids[: -len(output.token_ids)],
            response_ids=output.token_ids,
        )

        # ── book-keeping ──────────────────────────────────────────────
        if agent_data.metrics.get("num_preempted") is None:
            agent_data.metrics["num_preempted"] = (
                output.num_preempted if output.num_preempted is not None else -1
            )
        else:
            agent_data.metrics["num_preempted"] += (
                output.num_preempted if output.num_preempted is not None else 0
            )

        if not agent_data.extra_fields:
            agent_data.extra_fields.update(output.extra_fields)
        else:
            max_gs = output.extra_fields.get("max_global_steps")
            if max_gs:
                agent_data.extra_fields["max_global_steps"] = max_gs

        agent_data.assistant_turns += 1
        agent_data.response_ids     = output.token_ids
        agent_data.prompt_ids      += agent_data.response_ids

        resp_tokens_before = sum(agent_data.response_mask)
        agent_data.response_mask += [1] * len(agent_data.response_ids)
        resp_tokens_after  = sum(agent_data.response_mask)

        # ── record turn segment (required by SEEUpo) ──────────────────
        agent_data.turn_segs.append({
            "turn":       agent_data.assistant_turns,   # already incremented
            "resp_start": resp_tokens_before,
            "resp_end":   resp_tokens_after,            # exclusive
        })

        if output.log_probs:
            agent_data.response_logprobs += output.log_probs
        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        # ── hard termination guards ───────────────────────────────────
        if len(agent_data.response_mask) >= self.response_length:
            return HotpotAgentState.TERMINATED
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            return HotpotAgentState.TERMINATED
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            return HotpotAgentState.TERMINATED

        # ── decode to inspect content ─────────────────────────────────
        response_text: str = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(output.token_ids, skip_special_tokens=False),
        )

        agent_data.messages.append({"role": "assistant", "content": response_text})

        # ── check for <FINISHED> ──────────────────────────────────────
        if "<FINISHED>" in response_text:
            return HotpotAgentState.SCORING

        # ── check for tool calls ──────────────────────────────────────
        tool_schemas = [
            t.tool_schema for t in self.tools.values()
            if t.name != "calc_hotpot_reward"
        ]
        _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(
            output.token_ids, tool_schemas
        )

        if agent_data.tool_calls:
            return HotpotAgentState.PROCESSING_TOOLS

        # No tool calls, no <FINISHED> → model finished without signalling;
        # treat as terminal (no answer extracted → penalty via reward function)
        return HotpotAgentState.TERMINATED

    async def _handle_processing_tools(
        self, agent_data: AgentData
    ) -> HotpotAgentState:
        """Execute Search tool calls and inject results back."""
        add_messages: list[dict[str, Any]] = []

        tasks = [
            self._call_external_tool(tc, agent_data)
            for tc in agent_data.tool_calls[: self.max_parallel_calls]
            if tc.name != "calc_hotpot_reward"
        ]
        if not tasks:
            return HotpotAgentState.GENERATING

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)

        self._log_turn_debug(
            agent_data,
            turn_type="TOOL_RESPONSE",
            prompt_ids=agent_data.prompt_ids,
            tool_responses=[r[0].text for r in responses],
        )

        for tool_response, tool_reward in responses:
            if tool_reward is not None:
                agent_data.tool_rewards.append(tool_reward)
            add_messages.append({
                "role":    "tool",
                "content": tool_response.text or "",
            })

        agent_data.messages.extend(add_messages)

        # Tokenise tool results (no images for text-only QA)
        response_ids = await self.apply_chat_template(
            add_messages,
            images=None,
            videos=None,
            remove_system_prompt=True,
        )

        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            return HotpotAgentState.TERMINATED

        agent_data.prompt_ids    += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)

        agent_data.user_turns += 1
        return HotpotAgentState.GENERATING

    async def _handle_scoring(
        self,
        agent_data: AgentData,
        reward_tool,
        reward_instance_id: str | None,
    ) -> HotpotAgentState:
        """Extract final answer from <FINISHED> block and call reward tool."""
        last_assistant_text = ""
        for msg in reversed(agent_data.messages):
            if msg.get("role") == "assistant":
                last_assistant_text = msg.get("content", "")
                break

        final_answer = _extract_final_answer(last_assistant_text)

        if reward_tool is not None and reward_instance_id is not None:
            try:
                tool_response, tool_reward, _ = await reward_tool.execute(
                    reward_instance_id,
                    {"answer": final_answer},
                )
                agent_data.tool_rewards.append(tool_reward)

                feedback_message = {
                    "role":    "tool",
                    "content": tool_response.text or "",
                }
                agent_data.messages.append(feedback_message)

                # Append feedback tokens (mask=0, not trained on)
                feedback_ids = await self.apply_chat_template(
                    [feedback_message],
                    remove_system_prompt=True,
                )
                if (len(agent_data.response_mask) + len(feedback_ids)
                        < self.response_length):
                    agent_data.prompt_ids    += feedback_ids
                    agent_data.response_mask += [0] * len(feedback_ids)
                    if agent_data.response_logprobs:
                        agent_data.response_logprobs += [0.0] * len(feedback_ids)

                agent_data.turn_scores.append(tool_reward)

            except Exception as e:
                logger.warning(
                    f"Scoring failed for request {agent_data.request_id}: {e}"
                )

        return HotpotAgentState.TERMINATED

    # ------------------------------------------------------------------
    # External tool dispatch
    # ------------------------------------------------------------------

    async def _call_external_tool(
        self,
        tool_call: FunctionCall,
        agent_data: AgentData,
    ) -> tuple[ToolResponse, float | None]:
        tool_name = tool_call.name
        tool      = self.tools.get(tool_name)
        if tool is None:
            return (
                ToolResponse(
                    text=f"Unknown tool: {tool_name}. Available tools: {list(self.tools.keys())}"
                ),
                None,
            )

        instance_id = None
        try:
            tool_args   = json.loads(tool_call.arguments)
            kwargs      = agent_data.tools_kwargs.get(tool_name, {})
            instance_id, _ = await tool.create(
                create_kwargs=kwargs.get("create_kwargs", {})
            )
            tool_response, tool_reward, _ = await tool.execute(
                instance_id, tool_args, agent_data=agent_data
            )
        except Exception as e:
            logger.warning(f"External tool '{tool_name}' error: {e}")
            return ToolResponse(text=f"Tool call failed ({tool_name}): {e}"), None
        finally:
            if tool is not None and instance_id is not None:
                await tool.release(instance_id)

        # Truncate over-long responses
        text = tool_response.text or ""
        if len(text) > self.max_tool_response_length:
            side = self.tool_response_truncate_side
            L    = self.max_tool_response_length
            if side == "left":
                text = text[:L] + "...(truncated)"
            elif side == "right":
                text = "(truncated)..." + text[-L:]
            else:
                half = L // 2
                text = text[:half] + "...(truncated)..." + text[-half:]

        return ToolResponse(text=text), tool_reward