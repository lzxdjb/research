"""
HotpotQA multi-turn RL agent loop **with Memory Reflection**.

State machine:
    PENDING  →  GENERATING  →  PROCESSING_TOOLS  →  GENERATING  → … → SCORING
                            ↘  SCORING (on <FINISHED>)
                            ↘  TERMINATED (hard limits / no tool)
                                              ↓
                                          REFLECTING
                                              ↓
                                      UPDATING_MEMORY
                                              ↓
                                          TERMINATED

Key design (v2 — embedding-based memory):
  1. MemoryBank stores (problem, strategy) pairs with embeddings.
  2. By default there is NO blind injection in PENDING — the model queries
     memory on demand via the SearchMemory tool, which returns semantically
     relevant (problem, strategy) pairs. An opt-in config can directly inject
     top-k memories into the initial prompt for ablation experiments.
  3. REFLECTING phase asks LLM to produce <PROBLEM> + <REFLECTION> blocks.
  4. UPDATING_MEMORY votes +1/-1 on every memory the model retrieved via
     SearchMemory during the episode, then stores the new (problem, strategy) pair.
"""

import asyncio
import copy
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
from verl.tools.hotpot_memory_bank import get_or_create_memory_bank
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
_REFLECTION_RE     = re.compile(r"<REFLECTION>(.*?)</REFLECTION>", re.DOTALL | re.IGNORECASE)
_PROBLEM_RE        = re.compile(r"<PROBLEM>(.*?)</PROBLEM>",       re.DOTALL | re.IGNORECASE)
_THINK_RE          = re.compile(r"<think>.*?</think>",             re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE     = re.compile(r"<think[^>]*>",                   re.IGNORECASE)
_STRUCTURED_TAG_RE = re.compile(r"<(?:PROBLEM|REFLECTION)>",       re.IGNORECASE)

_INVALID_MEMORY_RE = re.compile(
    r"("
    r"<think|</think|the user is asking|let me think|looking at this|"
    r"i need to|ground truth|exact match|\bf1\s*:|this trajectory|"
    r"this attempt|correct answer|tool description mentions|prompt says"
    r")",
    re.IGNORECASE,
)
_SEARCH_MEMORY_SECTION_RE = re.compile(
    r"\n### SearchMemory\n.*?(?=\n### |\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)
_MEMORY_BANK_SECTION_RE = re.compile(
    r"\n## Memory Bank\n.*?(?=\n## |\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _strip_thinking(text: str) -> str:
    """Remove thinking blocks before parsing structured reflection tags."""
    text = _THINK_RE.sub("", text)
    open_match = _OPEN_THINK_RE.search(text)
    if open_match:
        tag_match = _STRUCTURED_TAG_RE.search(text, open_match.end())
        if tag_match:
            text = text[:open_match.start()] + text[tag_match.start():]
        else:
            text = text[:open_match.start()]
    return text.strip()


def _extract_final_answer(text: str) -> str:
    m = _FINISHED_STRICT_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _FINISHED_LOOSE_RE.search(text)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if line:
                line = re.sub(r"^Answer\s*:\s*", "", line, flags=re.IGNORECASE)
                return line
    return ""


def _extract_problem(text: str) -> str:
    m = _PROBLEM_RE.search(text)
    return m.group(1).strip() if m else ""


def _extract_reflection(text: str) -> str:
    m = _REFLECTION_RE.search(text)
    return m.group(1).strip() if m else ""


def _is_valid_memory_entry(problem: str, strategy: str) -> bool:
    if not problem or not strategy:
        return False
    if len(problem) > 500 or len(strategy) > 1200:
        return False
    if _INVALID_MEMORY_RE.search(problem) or _INVALID_MEMORY_RE.search(strategy):
        return False
    return True


def _get_latest_user_content(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content", "")).strip()
    return ""


def _prepend_to_latest_user_message(messages: list[dict[str, Any]], prefix: str) -> None:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            msg["content"] = f"{prefix}\n\n## Question\n{msg.get('content', '')}"
            return
    messages.append({"role": "user", "content": prefix})


def _hide_search_memory_prompt(messages: list[dict[str, Any]]) -> None:
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = str(msg.get("content", ""))
        content = _SEARCH_MEMORY_SECTION_RE.sub("", content)
        content = _MEMORY_BANK_SECTION_RE.sub("", content)
        content = content.replace(
            "2. **If retrieval or reasoning becomes uncertain**, call `SearchMemory` "
            "with a description of the current difficulty type to retrieve relevant past strategies.",
            "2. If retrieval or reasoning becomes uncertain, continue with targeted Search queries and careful reasoning.",
        )
        content = content.replace(
            "2. Optionally consult the memory bank for relevant strategies.",
            "2. Use Search and careful reasoning; do not use memory-bank tools.",
        )
        msg["content"] = content


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _format_injected_memories(memories: list[dict], max_chars: int) -> str:
    lines = [
        "## Relevant Past Experiences",
        "The following memories were retrieved automatically from the memory bank. "
        "Use them only as general strategy hints; answer the factual question using evidence.",
    ]
    for mem in memories:
        problem = str(mem.get("problem", "")).strip()
        strategy = str(mem.get("strategy", "")).strip()
        if max_chars > 0:
            problem = problem[:max_chars]
            strategy = strategy[:max_chars]
        score = int(mem.get("score", 0))
        uses = int(mem.get("uses", 0))
        mean_delta = float(mem.get("mean_delta", 0.0))
        lines.append(
            f"\n[MEMORY_ID: {mem.get('id', '')}] "
            f"(score: {score:+d}, uses: {uses}, mean_delta: {mean_delta:+.3f})"
        )
        lines.append(f"Problem: {problem}")
        lines.append(f"Strategy: {strategy}")
    return "\n".join(lines)


# ── Agent state ───────────────────────────────────────────────────────────────

class AgentData:
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

        self.prompt_ids:        list[int]   = []
        self.response_ids:      list[int]   = []
        self.response_mask:     list[int]   = []
        self.response_logprobs: list[float] = []

        self.turn_scores:  list[float] = []
        self.tool_rewards: list[float] = []

        self.user_turns      = 0
        self.assistant_turns = 0

        self.turn_segs: list[dict] = []
        self.tool_calls: list[FunctionCall] = []

        self.routed_experts = None
        self.extra_fields: dict[str, Any] = {}

        # Reflection-specific
        self.final_answer:         str       = ""
        self.trajectory_reward:    float     = 0.0
        # Memory ids that the model retrieved via SearchMemory during this episode
        self.searched_memory_ids:  list[str] = []
        # Memory ids injected into the initial prompt by the optional ablation path
        self.injected_memory_ids:  list[str] = []
        self.new_memory_id:        Optional[str] = None
        self.reflect_response_ids: list[int] = []
        self.reflect_response_logprobs: list[float] = []
        self.reflect_prompt_len: int = 0

        # Per-trajectory controls used by ablations.
        self.disabled_tool_names: set[str] = set()
        self.disable_memory_injection: bool = False
        self.skip_memory_vote: bool = False


class HotpotReflectState(Enum):
    PENDING          = "pending"
    GENERATING       = "generating"
    PROCESSING_TOOLS = "processing_tools"
    SCORING          = "scoring"
    REFLECTING       = "reflecting"
    UPDATING_MEMORY  = "updating_memory"
    TERMINATED       = "terminated"


# ── Agent loop ────────────────────────────────────────────────────────────────

@register("hotpot_qa_reflect_agent")
class HotpotQAReflectAgentLoop(AgentLoopBase):
    """
    Multi-turn RL agent loop for HotpotQA with semantic memory & reflection.

    The model can call SearchMemory(query=...) during generation to retrieve
    relevant past (problem, strategy) pairs. After each episode the model reflects,
    producing a new (problem, strategy) pair. Memories are voted on based on
    whether they were retrieved and the answer was correct.
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

        # Internal tools excluded from the model's schema
        self._hidden_tool_names = {"calc_hotpot_reward"}
        self.tool_schemas = [
            t.tool_schema.model_dump(exclude_unset=True, exclude_none=True)
            for t in tool_list
            if t.name not in self._hidden_tool_names
        ]

        self.tool_parser      = ToolParser.get_tool_parser(mt.format, self.tokenizer)
        self.tool_parser_name = mt.format

        # Memory bank config
        mem_cfg = getattr(mt, "memory", None)
        self.memory_save_dir      = getattr(mem_cfg, "save_dir",          "/tmp/hotpot_memory") if mem_cfg else "/tmp/hotpot_memory"
        self.memory_max_entries   = getattr(mem_cfg, "max_entries",        100)  if mem_cfg else 100
        self.memory_prune_thresh  = getattr(mem_cfg, "prune_threshold",    -2)   if mem_cfg else -2
        self.memory_dedup_thresh  = getattr(mem_cfg, "dedup_threshold",    0.85) if mem_cfg else 0.85
        self.memory_embedding_url = getattr(mem_cfg, "embedding_server",   "http://localhost:8765") if mem_cfg else "http://localhost:8765"
        self.memory_prune_min_uses = int(getattr(mem_cfg, "prune_min_uses", 4) if mem_cfg else 4)
        self.memory_prune_mean_delta_threshold = float(
            getattr(mem_cfg, "prune_mean_delta_threshold", 0.0) if mem_cfg else 0.0
        )
        self.memory_delta_ema_alpha = float(getattr(mem_cfg, "delta_ema_alpha", 0.2) if mem_cfg else 0.2)
        self.memory_search_min_similarity = float(getattr(mem_cfg, "search_min_similarity", 0.0) if mem_cfg else 0.0)
        self.memory_search_candidate_multiplier = int(
            getattr(mem_cfg, "search_candidate_multiplier", 4) if mem_cfg else 4
        )
        self.memory_search_top_k = int(getattr(mem_cfg, "search_top_k", 0) if mem_cfg else 0)
        self.memory_inject_top_k  = int(getattr(mem_cfg, "inject_top_k",   0)    if mem_cfg else 0)
        self.memory_inject_min_score = int(getattr(mem_cfg, "inject_min_score", 2) if mem_cfg else 2)
        self.memory_inject_max_chars = int(getattr(mem_cfg, "inject_max_chars", 700) if mem_cfg else 700)
        self.memory_vote_injected = _as_bool(getattr(mem_cfg, "vote_injected", True) if mem_cfg else True)
        self.memory_disable_search_memory = _as_bool(getattr(mem_cfg, "disable_search_memory", False) if mem_cfg else False)
        self.memory_paired_rollout = _as_bool(getattr(mem_cfg, "paired_rollout", False) if mem_cfg else False)
        self.memory_paired_force_search_memory = _as_bool(getattr(mem_cfg, "paired_force_search_memory", True) if mem_cfg else True)
        # Legacy knobs kept for old configs. Paired deltas now update memory
        # statistics only; they do not shape PPO reward or direct vote signs.
        self.memory_paired_nonpositive_vote_delta = int(getattr(mem_cfg, "paired_nonpositive_vote_delta", 0) if mem_cfg else 0)
        self.memory_paired_delta_reward_coef = float(getattr(mem_cfg, "paired_delta_reward_coef", 0.0) if mem_cfg else 0.0)
        self.memory_mask_reflection = _as_bool(getattr(mem_cfg, "mask_reflection", True) if mem_cfg else True)
        if self.memory_disable_search_memory:
            self._hidden_tool_names.add("SearchMemory")
            self.tool_schemas = [
                t.tool_schema.model_dump(exclude_unset=True, exclude_none=True)
                for t in tool_list
                if t.name not in self._hidden_tool_names
            ]

        # Reflect sampling
        self.reflect_temperature = getattr(mem_cfg, "reflect_temperature", 0.3) if mem_cfg else 0.3
        self.reflect_max_tokens  = getattr(mem_cfg, "reflect_max_tokens",  512) if mem_cfg else 512

        self.memory_bank = get_or_create_memory_bank(
            save_dir=self.memory_save_dir,
            max_memories=self.memory_max_entries,
            prune_threshold=self.memory_prune_thresh,
            dedup_threshold=self.memory_dedup_thresh,
            embedding_server=self.memory_embedding_url,
            prune_min_uses=self.memory_prune_min_uses,
            prune_mean_delta_threshold=self.memory_prune_mean_delta_threshold,
            delta_ema_alpha=self.memory_delta_ema_alpha,
            search_min_similarity=self.memory_search_min_similarity,
            search_candidate_multiplier=self.memory_search_candidate_multiplier,
        )

        # Wire memory bank into SearchMemory tool
        search_mem_tool = self.tools.get("SearchMemory")
        if search_mem_tool is not None:
            search_mem_tool.set_memory_bank(self.memory_bank)
            if self.memory_search_top_k > 0:
                search_mem_tool.set_top_k(self.memory_search_top_k)

        # Load reflect prompt template
        reflect_prompt_path = getattr(mem_cfg, "reflect_prompt_path", None) if mem_cfg else None
        if reflect_prompt_path and os.path.isfile(reflect_prompt_path):
            with open(reflect_prompt_path, "r", encoding="utf-8") as f:
                self._reflect_prompt_template = f.read().strip()
        else:
            self._reflect_prompt_template = _DEFAULT_REFLECT_PROMPT

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @rollout_trace_op
    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        raw_messages = kwargs["raw_prompt"]
        is_validation = _as_bool(kwargs.get("_agent_loop_validate", False))

        multi_modal_data = await self.process_vision_info(raw_messages)
        images = multi_modal_data.get("images")
        videos = multi_modal_data.get("videos")
        tools_kwargs = kwargs.get("tools_kwargs", {})

        if self.memory_paired_rollout:
            if is_validation:
                return await self._run_validation_memory_rollout(
                    sampling_params, raw_messages, images, videos, tools_kwargs
                )
            return await self._run_paired_memory_rollout(
                sampling_params, raw_messages, images, videos, tools_kwargs
            )

        agent_data = self._new_agent_data(
            copy.deepcopy(raw_messages), images, videos, tools_kwargs
        )
        reward_tool, reward_instance_id = (
            (None, None) if is_validation else await self._create_reward_instance(tools_kwargs)
        )
        try:
            await self._execute_episode(
                agent_data, sampling_params, reward_tool, reward_instance_id,
                stop_after_scoring=is_validation,
            )
        finally:
            if reward_tool is not None and reward_instance_id is not None:
                await reward_tool.release(reward_instance_id)

        self.memory_bank.flush()
        return self._build_output(agent_data)

    def _new_agent_data(
        self, messages: list[dict[str, Any]], image_data, video_data,
        tools_kwargs: dict[str, Any],
    ) -> AgentData:
        agent_data = AgentData(
            messages=messages,
            image_data=image_data,
            video_data=video_data,
            metrics={},
            request_id=uuid4().hex,
            tools_kwargs=tools_kwargs,
        )
        agent_data.extra_fields.update({
            "reflection_text":    "",
            "reflection_problem": "",
            "reflection_content": "",
            "new_memory_id":      "",
            "reflection_skipped": "",
            "eval_memory_only": False,
            "injected_memory_ids": "",
            "injected_memory_query": "",
            "injected_memory_count": 0,
            "paired_rollout": False,
            "paired_selected": "",
            "paired_no_memory_reward": 0.0,
            "paired_with_memory_reward": 0.0,
            "memory_advantage": 0.0,
            "memory_delta_reward_coef": 0.0,
            "paired_shaped_reward": 0.0,
            "paired_plain_reward": 0.0,
            "paired_with_memory_used_ids": "",
        })
        return agent_data

    async def _create_reward_instance(
        self, tools_kwargs: dict[str, Any]
    ) -> tuple[Any, str | None]:
        reward_tool = self.tools.get("calc_hotpot_reward")
        if reward_tool is None:
            return None, None
        rw_kwargs = tools_kwargs.get("calc_hotpot_reward", {})
        reward_instance_id, _ = await reward_tool.create(
            **rw_kwargs.get("create_kwargs", {})
        )
        return reward_tool, reward_instance_id

    async def _execute_episode(
        self, agent_data: AgentData, sampling_params: dict[str, Any],
        reward_tool, reward_instance_id: str | None, stop_after_scoring: bool,
    ) -> None:
        state = HotpotReflectState.PENDING
        while state != HotpotReflectState.TERMINATED:
            if state == HotpotReflectState.PENDING:
                state = await self._handle_pending(agent_data, sampling_params)
            elif state == HotpotReflectState.GENERATING:
                state = await self._handle_generating(
                    agent_data, sampling_params, reward_instance_id
                )
            elif state == HotpotReflectState.PROCESSING_TOOLS:
                state = await self._handle_processing_tools(agent_data)
            elif state == HotpotReflectState.SCORING:
                state = await self._handle_scoring(
                    agent_data, reward_tool, reward_instance_id
                )
                if stop_after_scoring:
                    state = HotpotReflectState.TERMINATED
            elif state == HotpotReflectState.REFLECTING:
                state = await self._handle_reflecting(agent_data, sampling_params)
            elif state == HotpotReflectState.UPDATING_MEMORY:
                state = await self._handle_updating_memory(agent_data)
            else:
                logger.error(f"Unknown state {state}, terminating.")
                state = HotpotReflectState.TERMINATED

    async def _run_reflection_update(
        self, agent_data: AgentData, sampling_params: dict[str, Any]
    ) -> None:
        state = await self._handle_reflecting(agent_data, sampling_params)
        if state == HotpotReflectState.UPDATING_MEMORY:
            await self._handle_updating_memory(agent_data)

    async def _run_validation_memory_rollout(
        self, sampling_params: dict[str, Any], raw_messages: list[dict[str, Any]],
        images, videos, tools_kwargs: dict[str, Any],
    ) -> AgentLoopOutput:
        """
        Validation/test path for paired-memory experiments.

        Training paired mode samples no-memory and with-memory trajectories,
        but PPO is trained on the memory-conditioned branch. Evaluation mirrors
        that deploy-time branch and leaves final scoring to the normal
        validation reward path.
        """
        agent_data = self._new_agent_data(
            copy.deepcopy(raw_messages), images, videos, tools_kwargs
        )
        # Match the with-memory arm of paired training: use SearchMemory as the
        # memory interface, not blind top-k injection.
        agent_data.disable_memory_injection = True
        agent_data.extra_fields["eval_memory_only"] = True
        agent_data.extra_fields["paired_selected"] = "with_memory_eval"

        if self.memory_paired_force_search_memory:
            _prepend_to_latest_user_message(
                agent_data.messages,
                "## Memory-Conditioned Rollout\nBefore answering, first call SearchMemory exactly once with a one-sentence difficulty type. Then solve the problem using the returned strategy and normal evidence search.",
            )

        await self._execute_episode(
            agent_data, sampling_params, reward_tool=None,
            reward_instance_id=None, stop_after_scoring=True,
        )
        self.memory_bank.flush()
        return self._build_output(agent_data)

    async def _run_paired_memory_rollout(
        self, sampling_params: dict[str, Any], raw_messages: list[dict[str, Any]],
        images, videos, tools_kwargs: dict[str, Any],
    ) -> AgentLoopOutput:
        no_memory_data = self._new_agent_data(
            copy.deepcopy(raw_messages), images, videos, tools_kwargs
        )
        no_memory_data.disable_memory_injection = True
        no_memory_data.disabled_tool_names.add("SearchMemory")
        _prepend_to_latest_user_message(
            no_memory_data.messages,
            "## No-Memory Baseline\nSolve this problem without memory tools. Use Search and reasoning only.",
        )

        reward_tool, reward_instance_id = await self._create_reward_instance(tools_kwargs)
        try:
            await self._execute_episode(
                no_memory_data, sampling_params, reward_tool, reward_instance_id,
                stop_after_scoring=True,
            )
        finally:
            if reward_tool is not None and reward_instance_id is not None:
                await reward_tool.release(reward_instance_id)

        with_memory_data = self._new_agent_data(
            copy.deepcopy(raw_messages), images, videos, tools_kwargs
        )
        with_memory_data.disable_memory_injection = True
        if self.memory_paired_force_search_memory:
            _prepend_to_latest_user_message(
                with_memory_data.messages,
                "## Memory-Conditioned Rollout\nBefore answering, first call SearchMemory exactly once with a one-sentence difficulty type. Then solve the problem using the returned strategy and normal evidence search.",
            )

        reward_tool, reward_instance_id = await self._create_reward_instance(tools_kwargs)
        try:
            await self._execute_episode(
                with_memory_data, sampling_params, reward_tool, reward_instance_id,
                stop_after_scoring=True,
            )
        finally:
            if reward_tool is not None and reward_instance_id is not None:
                await reward_tool.release(reward_instance_id)

        no_memory_reward = float(no_memory_data.trajectory_reward)
        with_memory_reward = float(with_memory_data.trajectory_reward)
        used_memory_ids = sorted(set(with_memory_data.searched_memory_ids))
        raw_advantage = with_memory_reward - no_memory_reward
        memory_advantage = raw_advantage if used_memory_ids else 0.0

        if used_memory_ids:
            for mid in used_memory_ids:
                self.memory_bank.vote(mid, memory_advantage)

        selected_data = with_memory_data
        selected_name = "with_memory"
        selected_data.skip_memory_vote = True
        selected_data.extra_fields.update({
            "paired_rollout": True,
            "paired_selected": selected_name,
            "paired_no_memory_reward": no_memory_reward,
            "paired_with_memory_reward": with_memory_reward,
            "memory_advantage": memory_advantage,
            "memory_delta_reward_coef": 0.0,
            "paired_shaped_reward": with_memory_reward,
            "paired_plain_reward": with_memory_reward,
            "paired_raw_reward_delta": raw_advantage,
            "paired_with_memory_used_ids": ",".join(used_memory_ids),
        })

        await self._run_reflection_update(selected_data, sampling_params)
        self.memory_bank.flush()
        return self._build_output(selected_data, reward_score=with_memory_reward)

    def _is_tool_visible(self, tool_name: str, agent_data: AgentData | None = None) -> bool:
        if tool_name in self._hidden_tool_names:
            return False
        if agent_data is not None and tool_name in agent_data.disabled_tool_names:
            return False
        return True

    def _model_tool_schemas(self, agent_data: AgentData) -> list[dict[str, Any]]:
        return [
            t.tool_schema.model_dump(exclude_unset=True, exclude_none=True)
            for t in self.tools.values()
            if self._is_tool_visible(t.name, agent_data)
        ]

    def _parser_tool_schemas(self, agent_data: AgentData):
        return [
            t.tool_schema for t in self.tools.values()
            if self._is_tool_visible(t.name, agent_data)
        ]

    def _build_output(
        self, agent_data: AgentData, reward_score: float | None = None
    ) -> AgentLoopOutput:
        response_ids = agent_data.prompt_ids[-len(agent_data.response_mask):]
        prompt_ids = agent_data.prompt_ids[: len(agent_data.prompt_ids)
                                           - len(agent_data.response_mask)]

        reflect_ids = agent_data.reflect_response_ids
        reflect_logprobs = agent_data.reflect_response_logprobs
        if reflect_ids:
            budget = self.response_length - len(agent_data.response_mask)
            reflect_ids = reflect_ids[:max(0, budget)]
            reflect_logprobs = reflect_logprobs[:len(reflect_ids)]

        reflect_mask = [0] * len(reflect_ids)
        if reflect_ids and not self.memory_mask_reflection:
            prompt_len = min(agent_data.reflect_prompt_len, len(reflect_ids))
            reflect_mask = [0] * prompt_len + [1] * (len(reflect_ids) - prompt_len)

        response_ids = list(response_ids) + reflect_ids
        response_mask = agent_data.response_mask + reflect_mask
        response_logprobs = None
        if agent_data.response_logprobs:
            if not reflect_logprobs:
                reflect_logprobs = [0.0] * len(reflect_ids)
            response_logprobs = agent_data.response_logprobs + reflect_logprobs

        mm_data = {}
        if agent_data.image_data is not None:
            mm_data["images"] = agent_data.image_data
        if agent_data.video_data is not None:
            mm_data["videos"] = agent_data.video_data

        output = AgentLoopOutput(
            prompt_ids=prompt_ids,
            response_ids=response_ids[: self.response_length],
            response_mask=response_mask[: self.response_length],
            multi_modal_data=mm_data,
            response_logprobs=(
                response_logprobs[: self.response_length]
                if response_logprobs else None
            ),
            reward_score=reward_score,
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
    ) -> HotpotReflectState:
        """Tokenise the initial prompt, optionally injecting top-k memories."""
        self._maybe_inject_memories(agent_data)
        if not self._is_tool_visible("SearchMemory", agent_data):
            _hide_search_memory_prompt(agent_data.messages)

        prompt_ids = await self.apply_chat_template(
            agent_data.messages,
            tools=self._model_tool_schemas(agent_data),
            images=agent_data.image_data,
            videos=agent_data.video_data,
        )
        if len(prompt_ids) > self.prompt_length:
            prompt_ids = prompt_ids[-self.prompt_length:]
        agent_data.prompt_ids = prompt_ids
        return HotpotReflectState.GENERATING

    def _maybe_inject_memories(self, agent_data: AgentData) -> None:
        """Opt-in memory injection for comparing direct memory vs model-called memory."""
        if agent_data.disable_memory_injection or self.memory_inject_top_k <= 0:
            return

        query = _get_latest_user_content(agent_data.messages)
        if not query:
            return

        agent_data.extra_fields["injected_memory_min_score"] = self.memory_inject_min_score

        memories = [
            mem for mem in self.memory_bank.get_top(k=self.memory_inject_top_k)
            if mem.get("score", 0) >= self.memory_inject_min_score
        ]
        if not memories:
            return

        memory_ids = [str(mem.get("id", "")) for mem in memories if mem.get("id")]
        agent_data.injected_memory_ids = memory_ids
        agent_data.extra_fields["injected_memory_ids"] = ",".join(memory_ids)
        agent_data.extra_fields["injected_memory_query"] = query
        agent_data.extra_fields["injected_memory_count"] = len(memory_ids)

        memory_block = _format_injected_memories(memories, self.memory_inject_max_chars)
        _prepend_to_latest_user_message(agent_data.messages, memory_block)

    async def _handle_generating(
        self,
        agent_data: AgentData,
        sampling_params: dict[str, Any],
        reward_instance_id: str | None,
    ) -> HotpotReflectState:
        with simple_timer("generate_sequences", agent_data.metrics):
            output: TokenOutput = await self.server_manager.generate(
                request_id=agent_data.request_id,
                prompt_ids=agent_data.prompt_ids,
                sampling_params=sampling_params,
                image_data=agent_data.image_data,
                video_data=agent_data.video_data,
            )

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

        agent_data.turn_segs.append({
            "turn":       agent_data.assistant_turns,
            "resp_start": resp_tokens_before,
            "resp_end":   resp_tokens_after,
        })

        if output.log_probs:
            agent_data.response_logprobs += output.log_probs
        if output.routed_experts is not None:
            agent_data.routed_experts = output.routed_experts

        if len(agent_data.response_mask) >= self.response_length:
            return HotpotReflectState.TERMINATED
        if self.max_assistant_turns and agent_data.assistant_turns >= self.max_assistant_turns:
            return HotpotReflectState.TERMINATED
        if self.max_user_turns and agent_data.user_turns >= self.max_user_turns:
            return HotpotReflectState.TERMINATED

        response_text: str = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(output.token_ids, skip_special_tokens=False),
        )
        agent_data.messages.append({"role": "assistant", "content": response_text})

        if "<FINISHED>" in response_text:
            return HotpotReflectState.SCORING

        tool_schemas = self._parser_tool_schemas(agent_data)
        _, agent_data.tool_calls = await self.tool_parser.extract_tool_calls(
            output.token_ids, tool_schemas
        )

        if agent_data.tool_calls:
            return HotpotReflectState.PROCESSING_TOOLS

        return HotpotReflectState.TERMINATED

    async def _handle_processing_tools(
        self, agent_data: AgentData
    ) -> HotpotReflectState:
        add_messages: list[dict[str, Any]] = []
        tasks = [
            self._call_external_tool(tc, agent_data)
            for tc in agent_data.tool_calls[: self.max_parallel_calls]
            if self._is_tool_visible(tc.name, agent_data)
        ]
        if not tasks:
            return HotpotReflectState.GENERATING

        with simple_timer("tool_calls", agent_data.metrics):
            responses = await asyncio.gather(*tasks)

        for tool_response, tool_reward in responses:
            if tool_reward is not None:
                agent_data.tool_rewards.append(tool_reward)
            add_messages.append({"role": "tool", "content": tool_response.text or ""})

        agent_data.messages.extend(add_messages)

        response_ids = await self.apply_chat_template(
            add_messages, images=None, videos=None, remove_system_prompt=True,
        )

        if len(agent_data.response_mask) + len(response_ids) >= self.response_length:
            return HotpotReflectState.TERMINATED

        agent_data.prompt_ids    += response_ids
        agent_data.response_mask += [0] * len(response_ids)
        if agent_data.response_logprobs:
            agent_data.response_logprobs += [0.0] * len(response_ids)

        agent_data.user_turns += 1
        return HotpotReflectState.GENERATING

    async def _handle_scoring(
        self,
        agent_data: AgentData,
        reward_tool,
        reward_instance_id: str | None,
    ) -> HotpotReflectState:
        last_assistant_text = ""
        for msg in reversed(agent_data.messages):
            if msg.get("role") == "assistant":
                last_assistant_text = msg.get("content", "")
                break

        final_answer = _extract_final_answer(last_assistant_text)
        agent_data.final_answer = final_answer

        if reward_tool is not None and reward_instance_id is not None:
            try:
                tool_response, tool_reward, _ = await reward_tool.execute(
                    reward_instance_id, {"answer": final_answer},
                )
                agent_data.trajectory_reward = tool_reward
                agent_data.tool_rewards.append(tool_reward)

                feedback_message = {"role": "tool", "content": tool_response.text or ""}
                agent_data.messages.append(feedback_message)

                feedback_ids = await self.apply_chat_template(
                    [feedback_message], remove_system_prompt=True,
                )
                if (len(agent_data.response_mask) + len(feedback_ids)
                        < self.response_length):
                    agent_data.prompt_ids    += feedback_ids
                    agent_data.response_mask += [0] * len(feedback_ids)
                    if agent_data.response_logprobs:
                        agent_data.response_logprobs += [0.0] * len(feedback_ids)

                agent_data.turn_scores.append(tool_reward)

            except Exception as e:
                logger.warning(f"Scoring failed for {agent_data.request_id}: {e}")

        return HotpotReflectState.REFLECTING

    async def _handle_reflecting(
        self, agent_data: AgentData, sampling_params: dict[str, Any]
    ) -> HotpotReflectState:
        reflect_user_msg = self._reflect_prompt_template.format(
            correct="correct" if agent_data.trajectory_reward > 0 else "incorrect",
        )

        reflect_messages = [{"role": "user", "content": reflect_user_msg}]
        reflect_prompt_ids = await self.apply_chat_template(
            reflect_messages, images=None, videos=None, remove_system_prompt=True,
        )

        reflect_params = dict(sampling_params)
        reflect_params["temperature"] = self.reflect_temperature
        reflect_params["max_tokens"]  = self.reflect_max_tokens

        combined_prompt_ids = agent_data.prompt_ids + reflect_prompt_ids

        if len(agent_data.response_mask) + len(reflect_prompt_ids) >= self.response_length:
            return HotpotReflectState.UPDATING_MEMORY

        try:
            reflect_output: TokenOutput = await self.server_manager.generate(
                request_id=uuid4().hex,
                prompt_ids=combined_prompt_ids,
                sampling_params=reflect_params,
                image_data=None,
                video_data=None,
            )
        except Exception as e:
            logger.warning(f"Reflection generation failed: {e}")
            return HotpotReflectState.UPDATING_MEMORY

        reflect_text: str = await self.loop.run_in_executor(
            None,
            lambda: self.tokenizer.decode(reflect_output.token_ids, skip_special_tokens=False),
        )

        agent_data.extra_fields["reflection_text"] = reflect_text

        # Strip thinking tokens before parsing structured tags so that
        # chain-of-thought text does not bleed into problem/strategy fields.
        clean_reflect = _strip_thinking(reflect_text)
        agent_data.extra_fields["reflection_problem"]  = _extract_problem(clean_reflect)
        agent_data.extra_fields["reflection_content"]  = _extract_reflection(clean_reflect)

        all_reflect_ids = reflect_prompt_ids + list(reflect_output.token_ids)
        reflect_logprobs = list(
            reflect_output.log_probs
            if reflect_output.log_probs is not None
            else [0.0] * len(reflect_output.token_ids)
        )
        agent_data.reflect_response_ids = all_reflect_ids
        agent_data.reflect_prompt_len = len(reflect_prompt_ids)
        agent_data.reflect_response_logprobs = [0.0] * len(reflect_prompt_ids) + reflect_logprobs

        return HotpotReflectState.UPDATING_MEMORY

    async def _handle_updating_memory(
        self, agent_data: AgentData
    ) -> HotpotReflectState:
        """
        1. Vote on memories the model retrieved via SearchMemory, plus optionally
           the memories injected by the direct-memory ablation path.
        2. Store the new (problem, strategy) pair from this episode's reflection.
        """
        correct = agent_data.trajectory_reward > 0
        delta   = 1 if correct else -1

        if not agent_data.skip_memory_vote:
            # Deduplicate memory ids before voting (a memory may have been retrieved
            # multiple times in one episode)
            voted_memory_ids = set(agent_data.searched_memory_ids)
            if self.memory_vote_injected:
                voted_memory_ids.update(agent_data.injected_memory_ids)
            for mid in voted_memory_ids:
                self.memory_bank.vote(mid, delta)

        problem  = agent_data.extra_fields.get("reflection_problem", "")
        strategy = agent_data.extra_fields.get("reflection_content", "")
        if _is_valid_memory_entry(problem, strategy):
            new_id = self.memory_bank.add(problem, strategy)
            agent_data.new_memory_id = new_id
            agent_data.extra_fields["new_memory_id"] = new_id or ""
        else:
            agent_data.extra_fields["reflection_skipped"] = "invalid_or_unstructured"

        return HotpotReflectState.TERMINATED

    # ------------------------------------------------------------------
    # External tool dispatch
    # ------------------------------------------------------------------

    async def _call_external_tool(
        self, tool_call: FunctionCall, agent_data: AgentData,
    ) -> tuple[ToolResponse, float | None]:
        tool_name = tool_call.name
        tool      = self.tools.get(tool_name)
        if tool is None:
            return (
                ToolResponse(
                    text=f"Unknown tool: {tool_name}. Available: {list(self.tools.keys())}"
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


# ── Default reflection prompt ─────────────────────────────────────────────────

_DEFAULT_REFLECT_PROMPT = """\
A problem-solving attempt just finished. The answer was {correct}.

Your task is to write ONE reusable experience entry composed of exactly two parts:

<PROBLEM>
Describe in 1 sentence the type of difficulty or situation this attempt encountered.
Use abstract terms — no specific entities, names, queries, or facts from this trajectory.
Choose the most specific root-cause difficulty from the attempt; do not copy wording
from the tool descriptions or this instruction.
</PROBLEM>

<REFLECTION>
State the general strategy to use (if correct) or avoid/fix (if incorrect) when facing that problem type.
2–3 sentences MAX. General and reusable — useful for ANY similar future problem.
</REFLECTION>

Rules — you MUST follow all of them:
- The PROBLEM tag must describe a situation/difficulty type, not a summary of what happened.
- The REFLECTION tag must contain only a strategy rule, not a description of this trajectory.
- No specific names, entities, questions, search queries, or facts from this trajectory in either tag.
- Do not mention tool-description examples, prompt rules, the user asking for reflection, or your hidden reasoning.
- Nothing outside the two tags."""
