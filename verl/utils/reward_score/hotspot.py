"""
Reward function and HotpotRewardTool for HotpotQA multi-turn RL.

Answer extraction
-----------------
We look for text inside the model's <FINISHED> block:
    <FINISHED>
    答案：Jonathan Stark
    </FINISHED>

Matching uses exact-match (EM) after normalisation (lower-case, strip
articles/punctuation, collapse whitespace) — the standard HotpotQA metric.

Reward structure
----------------
┌──────────────────────────────┬────────────┬──────────────────────────────────┐
│ Component                    │ Range      │ Notes                            │
├──────────────────────────────┼────────────┼──────────────────────────────────┤
│ acc_reward          (primary)│ -0.1 / 1.0 │ EM match after normalisation     │
│ f1_partial          (partial)│  0.0–0.5   │ token-level F1, only if EM fails │
└──────────────────────────────┴────────────┴──────────────────────────────────┘

Total = acc_reward + (f1_partial if acc == 0 else 0), clipped to [-0.1, 1.0].

To keep shaping simple and unfakeable, we do NOT add turn/tool bonuses here —
those are provided by the per-step tool_reward signals emitted by SearchTool.
"""

import re
import string
from typing import Optional
from uuid import uuid4

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse

# ── Regex for answer extraction ───────────────────────────────────────────────

_FINISHED_STRICT_RE = re.compile(
    r"<FINISHED>.*?Answer\s*:\s*(.+?)(?:\n|</FINISHED>|$)",
    re.DOTALL | re.IGNORECASE,
)
_FINISHED_LOOSE_RE = re.compile(r"<FINISHED>(.*)", re.DOTALL)
_TOOL_RESPONSE_RE  = re.compile(
    r"<tool_response>.*?</tool_response>",
    re.DOTALL | re.IGNORECASE,
)


# ── Text normalisation (standard HotpotQA / SQuAD style) ────────────────────

def _normalise(text: str) -> str:
    """Lower-case, strip articles, remove punctuation, collapse whitespace."""
    text = text.lower().strip()
    # Remove articles
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    # Remove punctuation
    text = text.translate(str.maketrans("", "", string.punctuation))
    # Collapse whitespace
    text = " ".join(text.split())
    return text


def _token_f1(prediction: str, ground_truth: str) -> float:
    """Token-level F1 between two normalised strings."""
    pred_tokens = _normalise(prediction).split()
    gt_tokens   = _normalise(ground_truth).split()

    if not pred_tokens or not gt_tokens:
        return 0.0

    common = set(pred_tokens) & set(gt_tokens)
    if not common:
        return 0.0

    precision = len(common) / len(pred_tokens)
    recall    = len(common) / len(gt_tokens)
    return 2 * precision * recall / (precision + recall)


# ── Answer extraction from the full predict string ───────────────────────────

def extract_answer(predict_str: str) -> str:
    """Return the model's stated answer (empty string if not found).

    Expected format:
        <FINISHED>
        Answer: Jonathan Stark
        </FINISHED>
    """
    clean = _TOOL_RESPONSE_RE.sub("", predict_str)

    m = _FINISHED_STRICT_RE.search(clean)
    if m:
        return m.group(1).strip()

    m = _FINISHED_LOOSE_RE.search(clean)
    if m:
        # Grab the first non-empty line after <FINISHED>
        for line in m.group(1).splitlines():
            line = line.strip()
            if line:
                # Strip a leading "Answer:" prefix if present
                line = re.sub(r"^Answer\s*:\s*", "", line, flags=re.IGNORECASE)
                return line
    return ""


# ── Reward components ─────────────────────────────────────────────────────────

def acc_reward(prediction: str, ground_truth: str) -> float:
    """Exact-match reward: +1.0 if EM, -0.1 if no answer found, 0.0 otherwise."""
    if not prediction:
        return -0.1
    if _normalise(prediction) == _normalise(ground_truth):
        return 1.0
    return 0.0


def f1_partial_reward(prediction: str, ground_truth: str) -> float:
    """
    Token F1 partial credit, capped at 0.5.
    Only used when exact match fails to provide a gradient signal.
    """
    if not prediction:
        return 0.0
    f1 = _token_f1(prediction, ground_truth)
    return min(f1 * 0.5, 0.5)   # scale so it can't exceed 0.5


# ── Public interface (used by compute_score in the verl reward pipeline) ──────

def compute_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    """
    Entry point called by the verl reward worker.

    Parameters
    ----------
    predict_str  : full concatenated token string for the rollout
    ground_truth : canonical answer string from the dataset
    extra_info   : optional dict with metadata (unused here)

    Returns
    -------
    dict with 'score' (the value used for RL) plus diagnostic sub-scores.
    """
    prediction = extract_answer(predict_str)
    acc        = acc_reward(prediction, ground_truth)

    if acc == 1.0:
        total = 1.0
        f1    = 0.0
    else:
        f1    = f1_partial_reward(prediction, ground_truth)
        total = acc + f1  # e.g. -0.1 + 0.0  or  0.0 + 0.3

    # Hard clip
    total = max(-0.1, min(total, 1.0))

    return {
        "score":       total,
        "acc":         acc,
        "f1_partial":  f1,
        "prediction":  prediction,
        "ground_truth": ground_truth,
    }


# ── Reward tool (called by the agent loop's SCORING state) ───────────────────

class HotpotRewardTool(BaseTool):
    """
    Internal scoring tool.  The model never sees this; the agent loop calls it
    directly after detecting <FINISHED> in the model output.
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instances: dict[str, dict] = {}

    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema:
        return self.tool_schema

    async def create(
        self,
        instance_id: Optional[str] = None,
        ground_truth: str = "",
        **kwargs,
    ) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instances[instance_id] = {"ground_truth": ground_truth}
        return instance_id, ToolResponse()

    async def execute(
        self,
        instance_id: str,
        parameters: dict,
        **kwargs,
    ) -> tuple[ToolResponse, float, dict]:
        ground_truth = self._instances.get(instance_id, {}).get("ground_truth", "")
        prediction   = (parameters.get("answer") or "").strip()

        acc = acc_reward(prediction, ground_truth)
        if acc == 1.0:
            total, f1 = 1.0, 0.0
            feedback  = f"✅ Correct! '{prediction}' matches the ground truth '{ground_truth}'."
        else:
            f1        = f1_partial_reward(prediction, ground_truth)
            total     = max(-0.1, acc + f1)
            if not prediction:
                feedback = "❌ No answer detected (missing <FINISHED> block or incorrect format)."
            else:
                feedback = (
                    f"❌ '{prediction}' does not match the ground truth '{ground_truth}'. "
                    f"(Token F1: {f1:.2f})"
                )

        return ToolResponse(text=feedback), total, {}

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instances.pop(instance_id, None)