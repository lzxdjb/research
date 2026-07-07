"""
compute_score_force_thought.py
==============================
Shaped reward for candlestick stock identification.
Variant: rewards the forced Thought + ActionList + <tool_call> output structure.

Differences from compute_score.py (the baseline without forced thought):
  - Adds thought_structure_bonus:  rewards exactly one Thought sentence per turn.
  - Adds actionlist_structure_bonus: rewards a matching ActionList line per turn.
  - Adds paired_turn_bonus: rewards (Thought + ActionList + tool_call) triples
    found together in the text, combined with real tool execution from extra_info.
  - Adds multi_finished_penalty: penalises printing <FINISHED> more than once.

Real-signal shaping (real_turn_bonus, real_tool_execution_bonus) is carried over
unchanged from compute_score.py — these remain the primary unfakeable signals.

Text-pattern shaping here rewards STRUCTURE (did the model write the right
skeleton?) not LENGTH (how many characters did it write?).
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Answer extraction regexes
# ---------------------------------------------------------------------------

_FORMAT_STRICT_RE = re.compile(
    r"<FINISHED>.*?股票代码候选[：:]\s*([\d,，\s]+)",
    re.DOTALL | re.IGNORECASE,
)
_FORMAT_LOOSE_RE = re.compile(
    r"<FINISHED>.*?(\d{6}(?:[,，\s]+\d{6})*)",
    re.DOTALL,
)
_SIX_DIGIT_RE = re.compile(r"\b(\d{6})\b")

# ---------------------------------------------------------------------------
# Structure detection regexes  (must match the format taught in the prompt)
# ---------------------------------------------------------------------------

# "Thought: <anything on one line>"
_THOUGHT_LINE_RE = re.compile(r"^Thought:\s*(.+)$", re.MULTILINE)

# "ActionList: 尝试使用工具 FinQuery，查询语句「...」"
# Flexible: accepts FinQuery with or without surrounding punctuation/spaces
_ACTIONLIST_RE = re.compile(
    r"^ActionList\s*[:：]\s*尝试使用工具\s*FinQuery[，,].*$",
    re.MULTILINE | re.IGNORECASE,
)

# Well-formed tool call block (the format the chat template injects)
_WELL_FORMED_CALL_RE = re.compile(
    r"<tool_call>\s*<function=\w+>.*?</function>\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)

# Opening tag only — used for the anti-hack comparison
_TOOL_CALL_OPEN_RE = re.compile(r"<tool_call>", re.IGNORECASE)

# Multiple <FINISHED> in one response
_MULTI_FINISHED_RE = re.compile(r"<FINISHED>.*?<FINISHED>", re.DOTALL)

# ---------------------------------------------------------------------------
# Answer extraction
# ---------------------------------------------------------------------------

def extract_answers(predict_str: str) -> list[str]:
    m = _FORMAT_STRICT_RE.search(predict_str)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))
    m = _FORMAT_LOOSE_RE.search(predict_str)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))
    matches = _SIX_DIGIT_RE.findall(predict_str)
    return matches[-5:] if matches else []

# ---------------------------------------------------------------------------
# Primary signals
# ---------------------------------------------------------------------------

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


def format_reward(predict_str: str) -> float:
    """1.0 if a valid <FINISHED> block with a 6-digit code is present."""
    return 1.0 if _FORMAT_LOOSE_RE.search(predict_str) else 0.0

# ---------------------------------------------------------------------------
# Shaping — real agent-loop signals (unfakeable)
# ---------------------------------------------------------------------------

def real_turn_bonus(extra_info: Optional[dict]) -> float:
    """
    Reward for completing multiple REAL generate→tool→response cycles.
    Source: extra_info["num_turns"] — set by agent loop, not printable by model.

    Scaling (subtract 1 for the final FINISHED turn):
        0 tool turns -> 0.00
        1 tool turn  -> 0.05
        2 tool turns -> 0.10
        3 tool turns -> 0.15
        4+ tool turns-> 0.20  (capped)
    """
    if not extra_info:
        return 0.0
    num_turns = extra_info.get("num_turns")
    if num_turns is None:
        return 0.0
    tool_turns = max(0, int(num_turns) - 1)
    return min(0.20, tool_turns * 0.05)


def real_tool_execution_bonus(extra_info: Optional[dict]) -> float:
    """
    Reward for tool calls that were ACTUALLY EXECUTED (HTTP round-trip done).
    Source: extra_info["tool_rewards"] — one entry per real execution. Unfakeable.

    +0.05 per executed call, capped at +0.20 (4 calls).
    """
    if not extra_info:
        return 0.0
    tool_rewards = extra_info.get("tool_rewards", [])
    if not isinstance(tool_rewards, list):
        return 0.0
    return min(0.20, len(tool_rewards) * 0.05)

# ---------------------------------------------------------------------------
# Shaping — structural text signals (forced-thought variant)
# ---------------------------------------------------------------------------

def thought_structure_bonus(predict_str: str) -> float:
    """
    Reward for Thought lines that are exactly one sentence.
    Penalise multi-sentence Thought lines (verbose reasoning).

    Per Thought line:
        1 sentence  -> +0.03
        2 sentences -> +0.01
        3+ sentences-> -0.03

    Returns [-0.12, +0.09] (capped).
    """
    thought_contents = _THOUGHT_LINE_RE.findall(predict_str)
    if not thought_contents:
        return 0.0

    total = 0.0
    for content in thought_contents:
        sentences = [s for s in re.split(r'[。！？.!?]+', content.strip()) if s.strip()]
        n = len(sentences)
        if n == 1:
            total += 0.03
        elif n == 2:
            total += 0.01
        else:
            total -= 0.03

    return max(-0.12, min(0.09, total))


def actionlist_structure_bonus(predict_str: str) -> float:
    """
    Reward for ActionList lines that match the required format:
        ActionList: 尝试使用工具 FinQuery，查询语句「...」

    +0.03 per valid ActionList line, capped at +0.09 (3 turns).
    -0.05 if there are zero ActionList lines but there are tool calls
          (model skipped the ActionList step).
    """
    actionlist_count = len(_ACTIONLIST_RE.findall(predict_str))
    tool_call_count  = len(_TOOL_CALL_OPEN_RE.findall(predict_str))

    if actionlist_count == 0:
        # Model made tool calls but skipped ActionList — penalise
        if tool_call_count > 0:
            return -0.05
        return 0.0

    return min(0.09, actionlist_count * 0.03)


def paired_turn_bonus(predict_str: str, extra_info: Optional[dict]) -> float:
    """
    Reward for complete, well-formed turns:
        Thought line + ActionList line + well-formed <tool_call> block

    We count how many such triples appear in the TEXT, then cross-check
    against real executions from extra_info to catch fake triples.

    Logic:
        text_triples  = min(thought_count, actionlist_count, well_formed_calls)
        real_executed = len(extra_info["tool_rewards"])
        credited      = min(text_triples, real_executed + 1)
                        ↑ allow 1 pending (current turn not yet executed)
        bonus = credited * 0.04, capped at 0.12

    Returns [0.0, +0.12].
    """
    thought_count     = len(_THOUGHT_LINE_RE.findall(predict_str))
    actionlist_count  = len(_ACTIONLIST_RE.findall(predict_str))
    well_formed_calls = len(_WELL_FORMED_CALL_RE.findall(predict_str))

    text_triples = min(thought_count, actionlist_count, well_formed_calls)
    if text_triples == 0:
        return 0.0

    real_executed = 0
    if extra_info:
        tr = extra_info.get("tool_rewards", [])
        if isinstance(tr, list):
            real_executed = len(tr)

    # Credit at most (real_executed + 1) triples — the +1 is the pending turn
    credited = min(text_triples, real_executed + 1)
    return min(0.12, credited * 0.04)


def multi_finished_penalty(predict_str: str) -> float:
    """
    -0.15 if the model printed <FINISHED> more than once in one response.
    This directly catches the most common hacking pattern observed.
    """
    return -0.15 if _MULTI_FINISHED_RE.search(predict_str) else 0.0


def finished_structure_bonus(predict_str: str) -> float:
    """
    +0.04 if the final FINISHED block uses the strict 股票代码候选 format.
    Encourages the model to follow the exact output spec for the final answer.
    """
    return 0.04 if _FORMAT_STRICT_RE.search(predict_str) else 0.0

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def compute_score(
    predict_str: str,
    ground_truth: str,
    format_score: float = 0.1,
    extra_info: Optional[dict] = None,
) -> dict:
    """
    Shaped reward for the forced-thought variant of the stock identification task.

    ┌────────────────────────────────┬───────────────┬──────────────────────────────────────┐
    │ Component                      │ Range         │ Source / hack-resistance             │
    ├────────────────────────────────┼───────────────┼──────────────────────────────────────┤
    │ acc_reward            (primary)│ -0.10 → +1.00 │ text — answer extraction             │
    │ format_reward         (primary)│  0.00 → +1.00 │ text — <FINISHED> present            │
    ├────────────────────────────────┼───────────────┼──────────────────────────────────────┤
    │ real_turn_bonus                │  0.00 → +0.20 │ extra_info["num_turns"]     ★ REAL  │
    │ real_tool_execution_bonus      │  0.00 → +0.20 │ extra_info["tool_rewards"]  ★ REAL  │
    ├────────────────────────────────┼───────────────┼──────────────────────────────────────┤
    │ thought_structure_bonus        │ -0.12 → +0.09 │ text — Thought line sentence count   │
    │ actionlist_structure_bonus     │ -0.05 → +0.09 │ text — ActionList line format        │
    │ paired_turn_bonus              │  0.00 → +0.12 │ text triples × real executions       │
    │ finished_structure_bonus       │  0.00 → +0.04 │ text — strict FINISHED format        │
    │ multi_finished_penalty         │ -0.15 →  0.00 │ text — duplicate FINISHED detection  │
    └────────────────────────────────┴───────────────┴──────────────────────────────────────┘

    Total clipped to [-0.3, 1.5].
    """
    acc = acc_reward(predict_str, ground_truth)
    fmt = format_reward(predict_str)
    primary = (1.0 - format_score) * acc + format_score * fmt

    # Real signals (unfakeable)
    turn_bon = real_turn_bonus(extra_info)
    tool_bon = real_tool_execution_bonus(extra_info)

    # Structural text signals (forced-thought specific)
    thought_b    = thought_structure_bonus(predict_str)
    actionlist_b = actionlist_structure_bonus(predict_str)
    paired_b     = paired_turn_bonus(predict_str, extra_info)
    # finished_b   = finished_structure_bonus(predict_str)
    # multi_fin_p  = multi_finished_penalty(predict_str)

    shaping = (
        turn_bon
        + tool_bon
        + thought_b
        + actionlist_b
        + paired_b
        # + finished_b
        # + multi_fin_p
    )
    total = max(-0.3, min(primary + shaping, 1.5))

    return {
        "score":                        total,
        # primary
        "acc":                          acc,
        "fmt":                          fmt,
        "primary":                      primary,
        # real signals
        "real_turn_bonus":              turn_bon,
        "real_tool_execution_bonus":    tool_bon,
        # structural text signals
        "thought_structure_bonus":      thought_b,
        "actionlist_structure_bonus":   actionlist_b,
        "paired_turn_bonus":            paired_b,
        # "finished_structure_bonus":     finished_b,
        # "multi_finished_penalty":       multi_fin_p,
        "shaping_total":                shaping,
    }