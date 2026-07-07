"""
SearchMemory tool — lets the model query the memory bank by semantic similarity.

The model calls SearchMemory(query="...") to retrieve relevant past strategies.
The tool does NOT award a tool_reward (reward is assigned in _handle_scoring based
on whether the memory was helpful, via voting in _handle_updating_memory).
"""

import logging
import os
from typing import Any, Optional
from uuid import uuid4

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


class SearchMemoryTool(BaseTool):
    """
    Semantic memory search tool.

    Configured with a reference to the shared MemoryBank singleton.
    The bank must be created (via get_or_create_memory_bank) before this tool is used.
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._top_k = config.get("top_k", 3)
        self._memory_bank = None   # injected after construction

    def set_memory_bank(self, bank) -> None:
        self._memory_bank = bank

    def set_top_k(self, top_k: int) -> None:
        self._top_k = max(1, int(top_k))

    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema:
        return self.tool_schema

    async def create(
        self,
        instance_id: Optional[str] = None,
        create_kwargs: Optional[dict] = None,
        **kwargs,
    ) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        return instance_id, ToolResponse()

    async def execute(
        self,
        instance_id: str,
        parameters: dict[str, Any],
        **kwargs,
    ) -> tuple[ToolResponse, float, dict]:
        query = (parameters.get("query") or "").strip()
        if not query or self._memory_bank is None:
            return ToolResponse(text="No relevant memories found."), 0.0, {}

        memories = self._memory_bank.search(query, k=self._top_k)

        if not memories:
            return ToolResponse(text="No relevant memories found."), 0.0, {}

        lines = ["Relevant strategies from past experience:"]
        for mem in memories:
            score = int(mem.get("score", 0))
            uses = int(mem.get("uses", 0))
            mean_delta = float(mem.get("mean_delta", 0.0))
            similarity = mem.get("similarity")
            sim_text = (
                f", similarity: {float(similarity):.3f}"
                if similarity is not None else ""
            )
            lines.append(
                f"\n[MEMORY_ID: {mem['id']}] "
                f"(score: {score:+d}, uses: {uses}, mean_delta: {mean_delta:+.3f}{sim_text})"
            )
            lines.append(f"Problem: {mem['problem']}")
            lines.append(f"Strategy: {mem['strategy']}")

        # Record which memory ids were retrieved so the agent loop can vote on them.
        # We tag the agent_data here — the loop reads agent_data.searched_memory_ids.
        agent_data = kwargs.get("agent_data")
        if agent_data is not None:
            if not hasattr(agent_data, "searched_memory_ids"):
                agent_data.searched_memory_ids = []
            agent_data.searched_memory_ids.extend(m["id"] for m in memories)

        return ToolResponse(text="\n".join(lines)), 0.0, {}

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        pass
