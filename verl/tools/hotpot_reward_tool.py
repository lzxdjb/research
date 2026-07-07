"""
HotpotRewardTool — scores a model's final answer against the ground-truth.

Scoring rules (standard HotpotQA evaluation):
  • Exact match (EM):  answer string equals ground truth after normalisation → 1.0
  • F1 token overlap:  token-level F1 between normalised answer and ground truth → [0, 1]
  • Final reward:      F1 score (EM is logged separately as a metric)

The ground truth is passed in at `create()` time via `create_kwargs`:
    {"ground_truth": "Jonathan Stark"}

This tool is named "calc_hotpot_reward" and is intentionally excluded from
the model's tool schema (the agent loop filters it out by name).
"""

import logging
import os
import re
import string
from collections import Counter
from typing import Any, Optional
from uuid import uuid4

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


# ── HotpotQA normalisation helpers (from the official eval script) ────────────

def _normalise(text: str) -> str:
    """Lower-case, strip articles and punctuation, collapse whitespace."""
    def remove_articles(s):
        return re.sub(r"\b(a|an|the)\b", " ", s)

    def white_space_fix(s):
        return " ".join(s.split())

    def remove_punc(s):
        exclude = set(string.punctuation)
        return "".join(ch for ch in s if ch not in exclude)

    return white_space_fix(remove_articles(remove_punc(text.lower())))


def _f1_score(prediction: str, ground_truth: str) -> float:
    pred_tokens = _normalise(prediction).split()
    gt_tokens   = _normalise(ground_truth).split()
    common      = Counter(pred_tokens) & Counter(gt_tokens)
    num_same    = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall    = num_same / len(gt_tokens)
    return 2 * precision * recall / (precision + recall)


def _exact_match(prediction: str, ground_truth: str) -> bool:
    return _normalise(prediction) == _normalise(ground_truth)


# ── Tool ──────────────────────────────────────────────────────────────────────

class HotpotRewardTool(BaseTool):
    """
    Internal scoring tool for HotpotQA.

    Not listed in the model's tool_schemas; called only by the agent loop's
    _handle_scoring() method.
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        # instance_id → {"ground_truth": str}
        self._instance_dict: dict[str, dict] = {}

    # ── BaseTool interface ────────────────────────────────────────────────────

    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema:
        return self.tool_schema

    async def create(
        self,
        instance_id: Optional[str] = None,
        ground_truth: Optional[str] = None,
        create_kwargs: Optional[dict] = None,
        **kwargs,
    ) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())

        create_kwargs = create_kwargs or kwargs.get("create_kwargs", {}) or {}
        if ground_truth is None:
            ground_truth = create_kwargs.get("ground_truth", kwargs.get("ground_truth", ""))
        self._instance_dict[instance_id] = {"ground_truth": str(ground_truth or "").strip()}
        return instance_id, ToolResponse()

    async def execute(
        self,
        instance_id: str,
        parameters: dict[str, Any],
        **kwargs,
    ) -> tuple[ToolResponse, float, dict]:
        state        = self._instance_dict.get(instance_id, {})
        ground_truth = state.get("ground_truth", "")
        prediction   = (parameters.get("answer") or "").strip()

        f1 = _f1_score(prediction, ground_truth)
        em = _exact_match(prediction, ground_truth)

        reward = f1   # use F1 as the training signal

        feedback = (
            f"Ground truth: {ground_truth}\n"
            f"Your answer:  {prediction}\n"
            f"F1: {f1:.3f}  |  Exact match: {'yes' if em else 'no'}"
        )
        return ToolResponse(text=feedback), reward, {"em": em, "f1": f1}

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)
