"""
compute_score_simple.py
=======================
Minimal shaped reward for candlestick stock identification.
Only rewards unfakeable signals from the agent loop.

Signals:
- acc_reward: correct answer (+1.0), wrong (0.0), no code (-0.1)
- real_turn_bonus: based on actual tool-execution turns (num_turns // 2)
- real_tool_execution_bonus: based on tool calls that returned real data
                             (tool_reward > 0 entries in tool_rewards)
"""

import re
from typing import Optional

_SIX_DIGIT_RE = re.compile(r"\b(\d{6})\b")
_FINISHED_LOOSE_RE = re.compile(r"<FINISHED>.*?(\d{6})", re.DOTALL)
_FORMAT_STRICT_RE = re.compile(
    r"<FINISHED>.*?股票代码候选[：:]\s*([\d,，\s]+)",
    re.DOTALL | re.IGNORECASE,
)


def extract_answers(predict_str: str) -> list[str]:
    m = _FORMAT_STRICT_RE.search(predict_str)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))
    m = _FINISHED_LOOSE_RE.search(predict_str)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))
    matches = _SIX_DIGIT_RE.findall(predict_str)
    return matches[-5:] if matches else []


def acc_reward(predict_str: str, ground_truth: str) -> float:
    """
    +1.0  ground truth in candidate list
     0.0  candidates found but wrong
    -0.1  no parseable 6-digit code at all
    """
    candidates = extract_answers(predict_str)
    if not candidates:
        return -0.1
    return 1.0 if str(ground_truth).strip() in candidates else 0.0


def real_turn_bonus(extra_info: Optional[dict]) -> float:
    """
    Reward for real tool-execution turns.
    
    num_turns counts all message turns (user + assistant pairs),
    so divide by 2 to get actual tool-call rounds, then subtract 1
    for the final FINISHED turn.

        tool_turns = max(0, num_turns // 2 - 1)

        0 tool turns -> 0.00
        1 tool turn  -> 0.10
        2 tool turns -> 0.20
        3+ tool turns-> 0.25 (capped)
    """
    if not extra_info:
        return 0.0
    num_turns = extra_info.get("num_turns")
    if num_turns is None:
        return 0.0
    tool_turns = max(0, int(num_turns) // 2 - 1)
    return min(0.25, tool_turns * 0.10)


def real_tool_execution_bonus(extra_info: Optional[dict]) -> float:
    """
    Reward for tool calls that actually returned data (tool_reward > 0).
    
    FinQueryTool now returns tool_reward=0.1 when results are found,
    0.0 when "找到0条数据". So we count entries where tool_reward > 0.

        +0.10 per productive call, capped at +0.25 (3+ calls).
    """
    if not extra_info:
        return 0.0
    tool_rewards = extra_info.get("tool_rewards", [])
    if not isinstance(tool_rewards, list):
        return 0.0
    productive_calls = sum(1 for r in tool_rewards if r > 0)
    return min(0.25, productive_calls * 0.10)


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
    turn_bon = real_turn_bonus(extra_info)
    tool_bon = real_tool_execution_bonus(extra_info)

    shaping = turn_bon + tool_bon
    total = max(-0.1, min(acc + shaping, 1.5))

    return {
        "score":                     total,
        "acc":                       acc,
        "real_turn_bonus":           turn_bon,
        "real_tool_execution_bonus": tool_bon,
        "shaping_total":             shaping,
    }