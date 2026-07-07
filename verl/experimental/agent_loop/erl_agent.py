# In your agent loop file — add ERLStockChartAgentLoop

import asyncio
import json
import logging
import os
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
from verl.experimental.agent_loop.stock_chart_agent import StockChartAgentLoop
from verl.experimental.agent_loop.tool_parser import FunctionCall, ToolParser
from verl.experimental.agent_loop.utils import build_gpt_oss_tool_response_text
from verl.interactions.base import BaseInteraction
from verl.interactions.utils.interaction_registry import initialize_interactions_from_config
from verl.tools.schemas import ToolResponse
from verl.tools.utils.tool_registry import initialize_tools_from_config
from verl.utils.profiler import simple_timer
from verl.utils.rollout_trace import rollout_trace_op
from verl.workers.rollout.replica import TokenOutput

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))
import re
# ---------------------------------------------------------------------------
# Additional state for this agent
# ---------------------------------------------------------------------------

from verl.experimental.agent_loop.erl_memory import get_global_memory
from verl.experimental.agent_loop.erl_reflection_prompt import (
    _REFLECTION_SYSTEM,
    build_reflection_prompt,
)

# Replace the existing _extract_final_answer function

_FINISHED_CANDIDATES_RE = re.compile(
    r"<FINISHED>.*?股票代码候选[：:]\s*([\d,，\s]+)",
    re.DOTALL | re.IGNORECASE,
)
_FINISHED_LOOSE_RE = re.compile(
    r"<FINISHED>.*?(\d{6}(?:[,，\s]+\d{6})*)",
    re.DOTALL,
)
_SIX_DIGIT_RE = re.compile(r"\b(\d{6})\b")


def _extract_final_answer(text: str) -> str:
    """
    Extract the full candidate string from the model's <FINISHED> block.
    Returns a comma-joined string of all 6-digit codes, e.g. "001209, 300033".
    Returns "" if nothing found.
    
    Passes the multi-candidate string directly to the reward tool's
    _parse_stock_codes(), which handles all parsing internally.
    """
    m = _FINISHED_CANDIDATES_RE.search(text)
    if m:
        codes = _SIX_DIGIT_RE.findall(m.group(1))
        return ", ".join(codes) if codes else ""

    m = _FINISHED_LOOSE_RE.search(text)
    if m:
        codes = _SIX_DIGIT_RE.findall(m.group(1))
        return ", ".join(codes) if codes else ""

    # Last resort: all 6-digit numbers appearing after <FINISHED>
    finished_idx = text.find("<FINISHED>")
    if finished_idx != -1:
        codes = _SIX_DIGIT_RE.findall(text[finished_idx:])
        return ", ".join(codes[-5:]) if codes else ""
    print("extract error! the text is: ", text)    
    return ""

class StockAgentState(Enum):
    PENDING          = "pending"
    GENERATING       = "generating"
    PROCESSING_TOOLS = "processing_tools"
    SCORING          = "scoring"       # dedicated state: call calc_stock_reward
    TERMINATED       = "terminated"



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
        # turn_segs: list[dict] = field(default_factory=list)
        self.turn_segs: list[dict] = []


class ERLAgentState(Enum):
    PENDING              = "pending"
    FIRST_GENERATING     = "first_generating"
    FIRST_TOOLS          = "first_tools"
    FIRST_SCORING        = "first_scoring"
    REFLECTING           = "reflecting"
    SECOND_GENERATING    = "second_generating"
    SECOND_TOOLS         = "second_tools"
    SECOND_SCORING       = "second_scoring"
    TERMINATED           = "terminated"


@register("erl_stock_chart_agent")
class ERLStockChartAgentLoop(AgentLoopBase):
    """
    Experiential Reinforcement Learning agent loop for stock chart identification.

    Extends StockChartAgentLoop with:
      1. Two-attempt structure separated by a reflection turn.
      2. Cross-episode reflection memory.
      3. Distillation target recording (x → y2) stored in extra_fields["erl_distill"].
      4. Per-attempt turn segments for potential per-turn advantage weighting.

    Configuration (under rollout_config.multi_turn):
      erl_reward_threshold (float): Threshold below which reflection is triggered. Default 0.5.
      erl_memory_max_size  (int):   Ring buffer size for reflection memory. Default 64.
      erl_memory_max_inject(int):   Max reflections injected per rollout. Default 3.
      erl_always_reflect   (bool):  If True, always reflect regardless of first reward. Default False.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mt = self.rollout_config.multi_turn
        self.max_user_turns          = mt.max_user_turns
        self.max_assistant_turns     = mt.max_assistant_turns
        self.max_parallel_calls      = mt.max_parallel_calls
        self.max_tool_response_length = mt.max_tool_response_length
        self.tool_response_truncate_side = mt.tool_response_truncate_side
        self.prompt_length           = self.rollout_config.prompt_length
        self.response_length         = self.rollout_config.response_length

        # External tools: FinQuery, Search, TickerChart
        tool_config_path = mt.tool_config_path
        tool_list = initialize_tools_from_config(tool_config_path) if tool_config_path else []
        self.tools       = {t.name: t for t in tool_list}

        self.tool_schemas = [
            t.tool_schema.model_dump(exclude_unset=True, exclude_none=True)
            for t in tool_list
            if t.name not in ["calc_stock_reward", "TickerChart"]
        ]

        self.tool_parser      = ToolParser.get_tool_parser(mt.format, self.tokenizer)
        self.tool_parser_name = mt.format
        self.reward_threshold  = getattr(mt, "erl_reward_threshold",  0.5)
        self.always_reflect    = getattr(mt, "erl_always_reflect",    False)
        mem_max_size           = getattr(mt, "erl_memory_max_size",   64)
        mem_max_inject         = getattr(mt, "erl_memory_max_inject", 3)
        self.memory            = get_global_memory(max_size=mem_max_size,
                                                   max_inject=mem_max_inject)


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
        turn_num = agent_data.assistant_turns + agent_data.user_turns
        sep = "=" * 80
        prompt_text = self.tokenizer.decode(prompt_ids, skip_special_tokens=False)
        lines = [
            # f"\n{sep}",
            # f"[TURN DEBUG] request_id={agent_data.request_id}  turn={turn_num}  type={turn_type}",
            # f"--- PROMPT ({len(prompt_ids)} tokens) ---",
            # prompt_text,
        ]
        # lines = []
        if response_ids is not None:
            response_text = self.tokenizer.decode(response_ids, skip_special_tokens=False)
            lines += [
                f"--- RESPONSE ({len(response_ids)} tokens) ---",
                response_text,
            ]
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
        print("\n".join(lines), flush=True)
    # ------------------------------------------------------------------
    # Entry point — override run()
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

        # ERL-specific state
        agent_data.extra_fields["erl_first_reward"]   = 0.0
        agent_data.extra_fields["erl_second_reward"]  = 0.0
        agent_data.extra_fields["erl_reflected"]      = False
        agent_data.extra_fields["erl_distill"]        = None  # (prompt_ids, response_ids) for distill
        agent_data.extra_fields["erl_split_idx"]        = None  # (prompt_ids, response_ids) for distill
        
        # ── Always initialize distill fields so batch sizes stay consistent ──
        agent_data.extra_fields["erl_distill_prompt_end_idx"]  = 0
        agent_data.extra_fields["erl_distill_response_start"]  = 0
        agent_data.extra_fields["_first_feedback"] = None
        agent_data.extra_fields["_first_reward"]   = 0.0
        agent_data.extra_fields["_reflection_text"]   = None

        # ── reward tool ──────────────────────────────────────────────
        reward_tool        = self.tools.get("calc_stock_reward")
        reward_instance_id = None
        if reward_tool is not None:
            rw_kwargs = tools_kwargs.get("calc_stock_reward", {})
            reward_instance_id, _ = await reward_tool.create(
                **rw_kwargs.get("create_kwargs", {})
            )

        # Save clean initial prompt (needed for distillation — no reflection context)
        initial_prompt_ids_snapshot: list[int] | None = None

        # ── state machine ─────────────────────────────────────────────
        state = ERLAgentState.PENDING
        
        while state != ERLAgentState.TERMINATED:
            if state == ERLAgentState.PENDING:
                state = await self._erl_handle_pending(agent_data, sampling_params)
                # Snapshot prompt ids right after tokenising the initial prompt,
                # before any response tokens are appended.
                initial_prompt_ids_snapshot = list(agent_data.prompt_ids)

            elif state == ERLAgentState.FIRST_GENERATING:
                state = await self._erl_handle_generating(
                    agent_data, sampling_params, reward_instance_id,
                    attempt="first",
                )

            elif state == ERLAgentState.FIRST_TOOLS:
                state, next_state = await self._erl_handle_tools(
                    agent_data, next_gen_state=ERLAgentState.FIRST_GENERATING,
                )
                state = state  # already set

            elif state == ERLAgentState.FIRST_SCORING:
                # breakpoint()
                first_reward, feedback_text, state = await self._erl_handle_scoring(
                    agent_data, reward_tool, reward_instance_id, attempt="first",
                )
                agent_data.extra_fields["erl_first_reward"] = first_reward

                # Decide whether to reflect
                should_reflect = self.always_reflect or (first_reward < self.reward_threshold)
                if should_reflect and not self._budget_exhausted(agent_data):
                    agent_data.extra_fields["erl_reflected"] = True
                    state = ERLAgentState.REFLECTING
                    # Store feedback for reflection prompt building
                    agent_data.extra_fields["_first_feedback"] = feedback_text
                    agent_data.extra_fields["_first_reward"]   = first_reward
                else:
                    
                    state = ERLAgentState.TERMINATED

            elif state == ERLAgentState.REFLECTING:
                state = await self._erl_handle_reflecting(
                    agent_data, sampling_params,
                    initial_prompt_ids=initial_prompt_ids_snapshot,
                    messages_snapshot=kwargs["raw_prompt"],
                )

            elif state == ERLAgentState.SECOND_GENERATING:
                state = await self._erl_handle_generating(
                    agent_data, sampling_params, reward_instance_id,
                    attempt="second",
                )

            elif state == ERLAgentState.SECOND_TOOLS:
                state, _ = await self._erl_handle_tools(
                    agent_data, next_gen_state=ERLAgentState.SECOND_GENERATING,
                )

            elif state == ERLAgentState.SECOND_SCORING:
                second_reward, _, state = await self._erl_handle_scoring(
                    agent_data, reward_tool, reward_instance_id, attempt="second",
                )
                agent_data.extra_fields["erl_second_reward"] = second_reward

                # Gate: store reflection in memory only if second attempt succeeded
                if second_reward > self.reward_threshold:
                    reflection_text = agent_data.extra_fields.get("_reflection_text", "")
                    if reflection_text:
                        self.memory.store(reflection_text)
                
                state = ERLAgentState.TERMINATED

            else:

                state = ERLAgentState.TERMINATED

        # ── release reward tool ──────────────────────────────────────
        if reward_tool is not None and reward_instance_id is not None:
            await reward_tool.release(reward_instance_id)

        # ── build output (same as parent) ────────────────────────────
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask):]
        prompt_ids   = agent_data.prompt_ids[: len(agent_data.prompt_ids) - len(agent_data.response_mask)]

        mm_data = {}
        if agent_data.image_data is not None:
            mm_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            mm_data["videos"] = agent_data.video_data
        # breakpoint()
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
    # ERL state handlers
    # ------------------------------------------------------------------

    async def _erl_handle_pending(
        self, agent_data: AgentData, sampling_params: dict[str, Any]
    ) -> ERLAgentState:
        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=self.tool_schemas,
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        agent_data.prompt_ids = prompt_ids
        return ERLAgentState.FIRST_GENERATING

    async def _erl_handle_generating(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
        reward_instance_id: str | None,
        attempt: str,  # "first" | "second" | "reflection"
    ) -> ERLAgentState:
        """Unified generation handler — mirrors _handle_generating from parent."""

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
            prompt_ids=agent_data.prompt_ids[: -len(output.token_ids)],  # prompt before appending response
            response_ids=output.token_ids,
            tool_calls=agent_data.tool_calls if agent_data.tool_calls else None,
        )
        # ── update preempt metrics ────────────────────────────────────
        if agent_data.metrics.get("num_preempted") is None:
            agent_data.metrics["num_preempted"] = (
                output.num_preempted if output.num_preempted is not None else -1
            )
        else:
            agent_data.metrics["num_preempted"] += (
                output.num_preempted if output.num_preempted is not None else 0
            )

        if not agent_data.extra_fields.get("max_global_steps"):
            agent_data.extra_fields.update(output.extra_fields)

        agent_data.assistant_turns += 1
        agent_data.response_ids     = output.token_ids
        agent_data.prompt_ids      += output.token_ids

        # ── response_mask: 1 for LLM-generated tokens ────────────────
        agent_data.response_mask   += [1] * len(output.token_ids)

        # Track turn segments
        resp_start = sum(agent_data.response_mask) - len(output.token_ids)
        resp_end   = sum(agent_data.response_mask)
        agent_data.turn_segs.append({
            "turn":       agent_data.assistant_turns,
            "attempt":    attempt,
            "resp_start": resp_start,
            "resp_end":   resp_end,
        })

        if output.log_probs:
            agent_data.response_logprobs += output.log_probs
        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        # ── hard termination guards ───────────────────────────────────
        if self._budget_exhausted(agent_data):
            
            return ERLAgentState.TERMINATED

        response_text: str = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(output.token_ids, skip_special_tokens=False),
        )

        # ── <FINISHED> check ─────────────────────────────────────────
        if "<FINISHED>" in response_text:
            # breakpoint()
            agent_data.messages.append({"role": "assistant", "content": response_text})
            if attempt == "first":
                return ERLAgentState.FIRST_SCORING
            else:
                return ERLAgentState.SECOND_SCORING

        # ── tool-call check ──────────────────────────────────────────
        tools = [t.tool_schema for t in self.tools.values()
            if t.name not in ["calc_stock_reward", "TickerChartTool"]]
        _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(
            output.token_ids, tools
        )

        if agent_data.tool_calls:
            agent_data.messages.append({"role": "assistant", "content": response_text})
            if attempt == "first":
                return ERLAgentState.FIRST_TOOLS
            else:
                return ERLAgentState.SECOND_TOOLS

        agent_data.messages.append({"role": "assistant", "content": response_text})
        
        return ERLAgentState.TERMINATED
    
    async def _call_external_tool(
        self,
        tool_call: FunctionCall,
        agent_data: AgentData,
    ) -> tuple[ToolResponse, float | None]:
        """
        Dispatch a single FinQuery / Search / TickerChart call.

        Returns (ToolResponse, optional_reward).
        """
        tool_name = tool_call.name
        tool = self.tools.get(tool_name)
        if tool is None:
            return (
                ToolResponse(text=f"未知工具：{tool_name}。可用工具：{list(self.tools.keys())}"),
                None,
            )

        instance_id = None
        try:
            tool_args = json.loads(tool_call.arguments)
            kwargs    = agent_data.tools_kwargs.get(tool_name, {})
            instance_id, _ = await tool.create(
                create_kwargs=kwargs.get("create_kwargs", {})
            )
            tool_response, tool_reward, _ = await tool.execute(
                instance_id, tool_args, agent_data=agent_data
            )
        except Exception as e:
            logger.warning(f"External tool '{tool_name}' error: {e}")
            return ToolResponse(text=f"工具调用失败 ({tool_name}): {e}"), None
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

        # Rebuild response with possibly-truncated text but preserve image/video
        kw = {"text": text}
        for attr in ("image", "video"):
            val = getattr(tool_response, attr, None)
            if val is not None:
                kw[attr] = val

        return ToolResponse(**kw), tool_reward
    
    async def _handle_processing_tools(self, agent_data: AgentData) -> StockAgentState:
        """
        Execute FinQuery / Search / TickerChart tool calls in parallel,
        inject results (including images) back into the conversation.
        """
        add_messages: list[dict[str, Any]] = []
        new_images_this_turn: list[Any] = []

        # Fire external tool calls (skip calc_stock_reward here — that has its own state)
        tasks = []
        tool_call_names = []
        for tc in agent_data.tool_calls[: self.max_parallel_calls]:
            if tc.name == "calc_stock_reward":
                # Should not appear here, but guard just in case
                continue
            tasks.append(self._call_external_tool(tc, agent_data))
            tool_call_names.append(tc.name)

        if not tasks:
            # Nothing to execute (all calls were filtered out)
            return StockAgentState.GENERATING

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)
            
        self._log_turn_debug(
        agent_data,
        turn_type="TOOL_RESPONSE",
        prompt_ids=agent_data.prompt_ids,  # full prompt so far
        tool_responses=[r[0].text for r in responses],  # ToolResponse.text per call
    )

        for tool_response, tool_reward in responses:
            if tool_reward is not None:
                agent_data.tool_rewards.append(tool_reward)

            # Build the tool-result message
            if tool_response.image:
                content = []
                content.append({"type": "image"})
                if tool_response.text:
                    content.append({"type": "text", "text": tool_response.text})
                message = {"role": "tool", "content": content}
                # Collect new images for multi-modal prompt update
                imgs = tool_response.image if isinstance(tool_response.image, list) else [tool_response.image]
                new_images_this_turn.extend(i for i in imgs if i is not None)
            else:
                message = {"role": "tool", "content": tool_response.text or ""}

            add_messages.append(message)

        agent_data.messages.extend(add_messages)

        # Tokenise the tool-result messages
        images  = new_images_this_turn if new_images_this_turn else None
        response_ids = await self.apply_chat_template(
            add_messages,
            images=images,
            videos=None,
            remove_system_prompt=True,
        )

        # Respect response length budget
        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            
            return StockAgentState.TERMINATED

        # Update image data
        if new_images_this_turn:
            if agent_data.image_data is None:
                agent_data.image_data = []
            elif not isinstance(agent_data.image_data, list):
                agent_data.image_data = [agent_data.image_data]
            agent_data.image_data.extend(new_images_this_turn)

        agent_data.prompt_ids    += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)

        agent_data.user_turns += 1
        return StockAgentState.GENERATING

    async def _erl_handle_tools(
        self,
        agent_data: AgentData,
        next_gen_state: ERLAgentState,
    ) -> tuple[ERLAgentState, None]:
        """Thin wrapper around parent's _handle_processing_tools."""
        # Reuse parent implementation; translate state on return
        new_state = await self._handle_processing_tools(agent_data)
        # _handle_processing_tools returns StockAgentState; map to ERLAgentState
        if new_state == StockAgentState.TERMINATED:
            
            return ERLAgentState.TERMINATED, None
        # Otherwise it returns GENERATING — map to appropriate attempt state
        return next_gen_state, None

    async def _erl_handle_scoring(
        self,
        agent_data: AgentData,
        reward_tool,
        reward_instance_id: str | None,
        attempt: str,
    ) -> tuple[float, str, ERLAgentState]:
        """Score the current attempt. Returns (reward, feedback_text, next_state)."""
        last_assistant_text = ""
        for msg in reversed(agent_data.messages):
            if msg.get("role") == "assistant":
                last_assistant_text = msg.get("content", "")
                break

        # final_answer = _extract_final_answer(last_assistant_text) or ""
        final_answer = _extract_final_answer(last_assistant_text)
        # If nothing found, pass the raw <FINISHED> block so _parse_stock_codes
        # gets a last-resort chance at bare 6-digit numbers
        if not final_answer:
            finished_idx = last_assistant_text.find("<FINISHED>")
            final_answer = last_assistant_text[finished_idx:] if finished_idx != -1 else last_assistant_text
        reward       = 0.0
        feedback_text = ""

        if reward_tool is not None and reward_instance_id is not None:
            try:
                tool_response, tool_reward, _ = await reward_tool.execute(
                    reward_instance_id,
                    {"answer": final_answer},
                )
                reward        = tool_reward if tool_reward is not None else 0.0
                feedback_text = tool_response.text or ""
                agent_data.tool_rewards.append(reward)
                agent_data.turn_scores.append(reward)

                # Append feedback to conversation (mask=0, not trained on)
                feedback_message = {"role": "tool", "content": feedback_text}
                agent_data.messages.append(feedback_message)
                feedback_ids = await self.apply_chat_template(
                    [feedback_message], remove_system_prompt=True
                )
                if not self._budget_exhausted(agent_data, extra=len(feedback_ids)):
                    agent_data.prompt_ids    += feedback_ids
                    agent_data.response_mask += [0] * len(feedback_ids)
                    if agent_data.response_logprobs:
                        agent_data.response_logprobs += [0.0] * len(feedback_ids)

                agent_data.user_turns += 1

            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"ERL scoring failed ({attempt}): {e}"
                )
        
        return reward, feedback_text, ERLAgentState.TERMINATED  # caller changes state

    async def _erl_handle_reflecting(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
        initial_prompt_ids: list[int],
        messages_snapshot: list[dict],
    ) -> ERLAgentState:
        """
        Generate a reflection conditioned on (x, y1, f1, r1, m).

        The reflection turn is structured as:
          - system: reflection system prompt
          - user:   reflection request (task + first attempt + feedback + memory)
          → assistant: reflection text  (response_mask = 1, trained by RL)

        After reflection, we reset the conversation to (x) and prepend
        the reflection as a hidden system-level hint for the second attempt,
        so the second attempt is conditioned on (x, Δ) as in the paper.
        """
        first_feedback = agent_data.extra_fields.get("_first_feedback", "")
        first_reward   = agent_data.extra_fields.get("_first_reward",   0.0)

        # Build first-attempt text from messages
        first_attempt_text = self._build_attempt_text(agent_data.messages)

        # Sample memories
        memories = self.memory.sample()

        # Task description = original user message text
        task_description = self._extract_task_description(messages_snapshot)

        reflection_user_content = build_reflection_prompt(
            task_description=task_description,
            first_attempt=first_attempt_text,
            feedback=first_feedback,
            reward=first_reward,
            memories=memories,
        )

        reflection_user_message_content = [
        {"type": "image"},                              # placeholder; actual image injected via image_data
        {"type": "text", "text": reflection_user_content},
    ]
        
        reflection_messages = [
        {"role": "system", "content": _REFLECTION_SYSTEM},
        {"role": "user",   "content": reflection_user_message_content},
    ]

        reflection_images = agent_data.image_data  # already a list of PIL Images
        
        # Tokenise reflection prompt (no tools, no images for the reflection turn)
        reflection_prompt_ids = await self.apply_chat_template(
            reflection_messages,
            tools=None,
            images=reflection_images,
            videos=None,
        )

        # Append as observation tokens (mask=0 for the reflection *prompt*)
        reflection_prompt_delta = reflection_prompt_ids[len(agent_data.prompt_ids):]
        # Actually we build a fresh prompt for the reflection sub-call,
        # then treat the reflection *output* as a trainable turn.
        # To keep things simple and compatible, we append the reflection
        # prompt as mask=0 context, then generate the reflection text.

        if self._budget_exhausted(agent_data, extra=len(reflection_prompt_ids)):
            
            return ERLAgentState.TERMINATED

        # Temporarily swap prompt_ids to reflection prompt for generation
        saved_prompt_ids    = agent_data.prompt_ids
        saved_response_mask = agent_data.response_mask
        saved_logprobs      = agent_data.response_logprobs
        saved_images        = agent_data.image_data

        agent_data.prompt_ids         = reflection_prompt_ids
        agent_data.image_data         = None  # reflection is text-only

        # Generate the reflection
  
        with simple_timer("erl_reflection", agent_data.metrics):
            ref_output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id + "_reflect",
                prompt_ids=reflection_prompt_ids,
                sampling_params=sampling_params,
                image_data=None,
                video_data=None,
            )
            
        self._log_turn_debug(
            agent_data,
            turn_type="REFLECT",
            prompt_ids=agent_data.prompt_ids[: -len(ref_output.token_ids)],  # prompt before appending response
            response_ids=ref_output.token_ids,
            tool_calls=agent_data.tool_calls if agent_data.tool_calls else None,
        )

        reflection_text: str = self.tokenizer.decode(
            ref_output.token_ids, skip_special_tokens=False
        )
        # Strip special tokens for storage / injection
        reflection_text_clean: str = self.tokenizer.decode(
            ref_output.token_ids, skip_special_tokens=True
        ).strip()

        agent_data.extra_fields["_reflection_text"] = reflection_text_clean

        # ── Restore base trajectory and append reflection as trainable turn ──
        agent_data.prompt_ids    = saved_prompt_ids
        agent_data.response_mask = saved_response_mask
        agent_data.response_logprobs = saved_logprobs
        agent_data.image_data    = saved_images

        # The reflection is appended to the ongoing trajectory:
        #   prompt ... | first-attempt tokens (mask=1) | feedback (mask=0) |
        #   reflection-prompt (mask=0) | reflection-output (mask=1)
        #
        # reflection-prompt delta = the new tokens introduced by switching to
        # the reflection prompt context; we treat them as observation (mask=0).
        # For simplicity, we encode just the reflection *request* as a user turn.

        reflection_context_message = {
            "role": "user",
            "content": (
                "[反思请求]\n"
                + reflection_user_content
            ),
        }
        reflection_context_ids = await self.apply_chat_template(
            [reflection_context_message], remove_system_prompt=True
        )

        if not self._budget_exhausted(agent_data, extra=len(reflection_context_ids)):
            agent_data.prompt_ids    += reflection_context_ids
            agent_data.response_mask += [0] * len(reflection_context_ids)
            if agent_data.response_logprobs:
                agent_data.response_logprobs += [0.0] * len(reflection_context_ids)
            agent_data.user_turns += 1

        # Reflection output — trainable (mask=1)
        agent_data.extra_fields["erl_split_idx"] = len(agent_data.response_mask)
        if not self._budget_exhausted(agent_data, extra=len(ref_output.token_ids)):
            agent_data.prompt_ids    += ref_output.token_ids
            agent_data.response_mask += [1] * len(ref_output.token_ids)
            if ref_output.log_probs:
                agent_data.response_logprobs += ref_output.log_probs
            else:
                if agent_data.response_logprobs:
                    agent_data.response_logprobs += [0.0] * len(ref_output.token_ids)
            agent_data.assistant_turns += 1

            resp_start = sum(agent_data.response_mask) - len(ref_output.token_ids)
            resp_end   = sum(agent_data.response_mask)
            agent_data.turn_segs.append({
                "turn":       agent_data.assistant_turns,
                "attempt":    "reflection",
                "resp_start": resp_start,
                "resp_end":   resp_end,
            })

        # ── Now set up the second attempt ────────────────────────────
        # The second attempt is conditioned on (x, Δ):
        # We inject the reflection as a system hint and re-run from the
        # original prompt. The second attempt appends to the *same* trajectory.
        second_attempt_hint = {
            "role": "user",
            "content": (
                "[基于以下反思，请重新分析图表并给出答案]\n\n"
                + reflection_text_clean
                 + "\n\n---\n"
                    "请重新从图表出发，按照工具调用格式（Thought/ActionList/<tool_call>）"
                    "进行分析，不要直接给出结论，必须通过工具验证后再输出 <FINISHED>。"
            ),
        }
        hint_ids = await self.apply_chat_template(
            [second_attempt_hint], remove_system_prompt=True
        )

        if not self._budget_exhausted(agent_data, extra=len(hint_ids)):
            agent_data.prompt_ids    += hint_ids
            agent_data.response_mask += [0] * len(hint_ids)
            if agent_data.response_logprobs:
                agent_data.response_logprobs += [0.0] * len(hint_ids)
            agent_data.user_turns += 1
            agent_data.messages.append(second_attempt_hint)

        # ── Record distillation target ───────────────────────────────
        # For Ldistill we need: input = initial_prompt_ids (x only, no reflection)
        #                        output = second attempt tokens (to be filled in)
        # We record the split point so the distillation pass can extract y2.
        agent_data.extra_fields["erl_distill_prompt_end_idx"] = len(initial_prompt_ids)
        agent_data.extra_fields["erl_distill_response_start"] = len(agent_data.response_mask)

        return ERLAgentState.SECOND_GENERATING

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _budget_exhausted(self, agent_data: AgentData, extra: int = 0) -> bool:
        used = len(agent_data.response_mask) + extra
        if used >= self.response_length:
            return True
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            return True
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            return True
        return False

    def _build_attempt_text(self, messages: list[dict]) -> str:
        """Concatenate assistant messages to build first-attempt transcript."""
        parts = []
        for msg in messages:
            role    = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Multi-modal content — extract text parts only
                content = " ".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                )
            if role in ("assistant", "tool"):
                parts.append(f"[{role}]: {content}")
        return "\n\n".join(parts)

    def _extract_task_description(self, messages: list[dict]) -> str:
        """Extract the original user task from the initial message list."""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                return content
        return "（股票K线图识别任务）"