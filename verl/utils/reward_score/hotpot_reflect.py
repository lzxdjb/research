"""
Reward function for HotpotQA **with memory reflection**.

Strict format rule
------------------
The model MUST emit a <FINISHED> block.  Any response that lacks it receives
-0.1 immediately — no partial credit.

Expected output format:
    <FINISHED>
    Answer: Jonathan Stark [MEMORY_ID: abc123]
    </FINISHED>

The [MEMORY_ID: ...] citation is optional; it is stripped before scoring.

Scoring (only reached when <FINISHED> is present)
--------------------------------------------------
┌──────────────────────────────┬────────────┬──────────────────────────────────┐
│ Component                    │ Range      │ Notes                            │
├──────────────────────────────┼────────────┼──────────────────────────────────┤
│ acc_reward          (primary)│  0.0 / 1.0 │ EM match after normalisation     │
│ f1_partial          (partial)│  0.0–0.5   │ token-level F1, only if EM fails │
└──────────────────────────────┴────────────┴──────────────────────────────────┘

Total = acc_reward + (f1_partial if EM==0 else 0), clipped to [0.0, 1.0].
Missing <FINISHED> → -0.1 (hard penalty, no partial credit).
"""

import re
import string
from typing import Optional

# ── Regex ─────────────────────────────────────────────────────────────────────

_FINISHED_RE = re.compile(r"<FINISHED>(.*?)</FINISHED>", re.DOTALL | re.IGNORECASE)
_ANSWER_RE   = re.compile(r"Answer\s*:\s*(.+)", re.IGNORECASE)
_MEMORY_CITE_RE = re.compile(r"\[MEMORY_ID\s*:\s*[a-f0-9]+\]", re.IGNORECASE)
_TOOL_RESP_RE   = re.compile(r"<tool_response>.*?</tool_response>", re.DOTALL | re.IGNORECASE)


# ── Text normalisation ────────────────────────────────────────────────────────

def _normalise(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = text.translate(str.maketrans("", "", string.punctuation))
    return " ".join(text.split())


def _token_f1(prediction: str, ground_truth: str) -> float:
    pred_tokens = _normalise(prediction).split()
    gt_tokens   = _normalise(ground_truth).split()
    if not pred_tokens or not gt_tokens:
        return 0.0
    from collections import Counter
    common   = Counter(pred_tokens) & Counter(gt_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall    = num_same / len(gt_tokens)
    return 2 * precision * recall / (precision + recall)


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _memory_delta_info(extra_info: Optional[dict]) -> tuple[float, float, float]:
    if not extra_info:
        return 0.0, 0.0, 0.0
    memory_advantage = _as_float(extra_info.get("memory_advantage"), 0.0)
    # Keep the paired memory delta in logs only. PPO reward remains the plain
    # answer reward so validation and training optimize the same objective.
    return memory_advantage, 0.0, 0.0


# ── Answer extraction ─────────────────────────────────────────────────────────

def _has_finished_block(text: str) -> bool:
    return bool(_FINISHED_RE.search(text))


def extract_answer(predict_str: str) -> str:
    """
    Extract the answer from the <FINISHED> block and strip any [MEMORY_ID:...] citation.
    Returns empty string if no valid block found.
    """
    clean = _TOOL_RESP_RE.sub("", predict_str)
    m = _FINISHED_RE.search(clean)
    if not m:
        return ""
    block = m.group(1)

    # Try "Answer: ..." line first
    am = _ANSWER_RE.search(block)
    if am:
        answer = am.group(1)
    else:
        # Fallback: first non-empty line
        answer = ""
        for line in block.splitlines():
            line = line.strip()
            if line:
                answer = line
                break

    # Strip memory citation
    answer = _MEMORY_CITE_RE.sub("", answer).strip()
    return answer


# ── Public interface ──────────────────────────────────────────────────────────

def compute_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    """
    Entry point called by the verl reward worker.

    Hard rule: no <FINISHED> block → score = -0.1, no further computation.
    """
    memory_advantage, memory_delta_coef, memory_delta_bonus = _memory_delta_info(extra_info)

    # Hard penalty: missing <FINISHED>
    if not _has_finished_block(predict_str):
        base_score = -0.1
        return {
            "score":        base_score,
            "base_score":   base_score,
            "acc":          -0.1,
            "f1_partial":    0.0,
            "memory_advantage": memory_advantage,
            "memory_delta_reward_coef": memory_delta_coef,
            "memory_delta_bonus": memory_delta_bonus,
            "prediction":   "",
            "ground_truth": ground_truth,
            "has_finished": False,
        }

    prediction = extract_answer(predict_str)

    if _normalise(prediction) == _normalise(ground_truth):
        acc, f1, total = 1.0, 0.0, 1.0
    elif not prediction:
        # Block present but empty answer
        acc, f1 = 0.0, 0.0
        total   = 0.0
    else:
        acc = 0.0
        f1  = min(_token_f1(prediction, ground_truth) * 0.5, 0.5)
        total = f1

    base_score = max(0.0, min(total, 1.0))
    return {
        "score":        base_score,
        "base_score":   base_score,
        "acc":          acc,
        "f1_partial":   f1,
        "memory_advantage": memory_advantage,
        "memory_delta_reward_coef": memory_delta_coef,
        "memory_delta_bonus": memory_delta_bonus,
        "prediction":   prediction,
        "ground_truth": ground_truth,
        "has_finished": True,
    }
