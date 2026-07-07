import re
from typing import Optional

_SIX_DIGIT_RE = re.compile(r"\b(\d{6})\b")
_FINISHED_STRICT_RE = re.compile(
    r"<FINISHED>.*?股票代码候选[：:]\s*([\d,，\s]+)",
    re.DOTALL | re.IGNORECASE,
)
_FINISHED_LOOSE_RE = re.compile(r"<FINISHED>(.*)", re.DOTALL)

# Matches tool_response blocks to strip them out
_TOOL_RESPONSE_RE = re.compile(
    r"<tool_response>.*?</tool_response>",
    re.DOTALL | re.IGNORECASE,
)


def _strip_tool_responses(text: str) -> str:
    """Remove all tool response blocks so codes from DB results aren't harvested."""
    return _TOOL_RESPONSE_RE.sub("", text)


def extract_answers(predict_str: str) -> list[str]:
    # Only look at text AFTER the last assistant turn, outside tool responses
    clean = _strip_tool_responses(predict_str)

    m = _FINISHED_STRICT_RE.search(clean)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))

    m = _FINISHED_LOOSE_RE.search(clean)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))

    # No <FINISHED> tag at all → no answer
    return []


def acc_reward(predict_str: str, ground_truth: str) -> float:
    candidates = extract_answers(predict_str)
    if not candidates:
        return -0.1
    return 1.0 if str(ground_truth).strip() in candidates else 0.0

def compute_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    """
    Simplified reward using only unfakeable signals.

    ┌──────────────────────────────┬───────────────┬──────────────────────────────────────┐
    │ Component                    │ Range         │ Source                               │
    ├──────────────────────────────┼───────────────┼──────────────────────────────────────┤
    │ acc_reward          (primary)│ -0.10 → +1.00 │ answer extraction from predict_str   │
    ├──────────────────────────────┼───────────────┼──────────────────────────────────────┤
    │ real_turn_bonus              │  0.00 → +0.25 │ num_turns // 2 - 1  ★ REAL          │
    │ real_tool_execution_bonus    │  0.00 → +0.25 │ productive tool_rewards  ★ REAL      │
    └──────────────────────────────┴───────────────┴──────────────────────────────────────┘

    Total clipped to [-0.1, 1.5].

    Incentive structure:
    - 0 tool calls, wrong answer:           -0.10
    - 3 tool calls (all empty), wrong:      +0.15  (turn bonus only)
    - 3 tool calls (2 productive), wrong:   +0.35  (turn + execution bonus)
    - 3 tool calls (2 productive), correct: +1.35
    """
    acc = acc_reward(predict_str, ground_truth)
    # turn_bon = real_turn_bonus(extra_info)
    # tool_bon = real_tool_execution_bonus(extra_info)

    # shaping = turn_bon + tool_bon
    # total = max(-0.1, min(acc + shaping, 1.5))

    return {
        "score":                     acc,
        "acc":                       acc,
        # "real_turn_bonus":           turn_bon,
        # "real_tool_execution_bonus": tool_bon,
        # "shaping_total":             shaping,
    }