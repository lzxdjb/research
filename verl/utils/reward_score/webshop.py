"""
webshop_reward.py
─────────────────
Reward function for the WebShop RL task.

Design
──────
The reward is read from the environment's "Task completed.<reward=X>" message
that EnvStepTool now appends to the final observation.  This is tamper-proof:
the model cannot inflate its reward by outputting a fake ASIN.

The model is still required to emit a <Finish>…</Finish> block — if it fails
to do so, a small penalty is applied.

Scoring table
─────────────
┌─────────────────────────────────────────────┬───────────┬───────────────────────┐
│ Outcome                                     │ Score     │ Notes                 │
├─────────────────────────────────────────────┼───────────┼───────────────────────┤
│ Task completed + <Finish> block present     │ env reward│ 0.0 – 1.0 from env    │
│ Task completed but no <Finish> block        │ -0.1      │ format error          │
│ Task failed / never done + <Finish> block   │  0.0      │ wrong product         │
│ Task failed / never done + no <Finish>      │ -0.1      │ gave up / timed out   │
└─────────────────────────────────────────────┴───────────┴───────────────────────┘
"""

import re
from typing import Optional


# ── Regex patterns ─────────────────────────────────────────────────────────────

# Model's termination block
_FINISH_RE = re.compile(
    r"<Finish>.*?</Finish>",
    re.IGNORECASE | re.DOTALL,
)

# Matches tool_response blocks (how the conversation history is stored)
_TOOL_RESPONSE_RE = re.compile(
    r"<tool_response>(.*?)</tool_response>",
    re.DOTALL | re.IGNORECASE,
)

# Matches the reward we embedded in the terminal observation:
#   "Task completed.<reward=0.444444>"
_TASK_COMPLETED_RE = re.compile(
    r"Task completed\.<reward=([\d.]+)>",
    re.IGNORECASE,
)

# Simple failure phrase
_TASK_FAILED_RE = re.compile(r"Task failed\.", re.IGNORECASE)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_observations(predict_str: str) -> list[str]:
    """Pull all environment observations from tool_response blocks."""
    return [m.group(1).strip() for m in _TOOL_RESPONSE_RE.finditer(predict_str)]


def _has_finish_block(predict_str: str) -> bool:
    return bool(_FINISH_RE.search(predict_str))


def _extract_env_reward(observations: list[str]) -> Optional[float]:
    """
    Scan observations (most recent first) for 'Task completed.<reward=X>'.
    Returns the float reward, or None if the task never completed.
    """
    for obs in reversed(observations):
        m = _TASK_COMPLETED_RE.search(obs)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def _task_failed(observations: list[str]) -> bool:
    return any(_TASK_FAILED_RE.search(obs) for obs in observations)


# ── Main reward computation ────────────────────────────────────────────────────

def acc_reward(predict_str: str) -> float:
    """
    Primary accuracy reward.

    Reads the true environment reward from the embedded 'Task completed.<reward=X>'
    string that EnvStepTool appends to the terminal observation, so the model
    cannot influence the score by writing a specific ASIN.

    Returns:
        float in [-0.1, 1.0]
    """
    observations  = _extract_observations(predict_str)
    env_reward    = _extract_env_reward(observations)
    has_finish    = _has_finish_block(predict_str)

    if env_reward is not None:
        # Task actually completed in the environment
        if not has_finish:
            # Model forgot the termination format — penalise
            return -0.1
        return float(env_reward)     # already in [0, 1]

    # Task never completed (wrong product bought, ran out of turns, etc.)
    # If the model at least tried to terminate cleanly, no extra penalty.
    return 0.0 if has_finish else -0.1


def compute_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    """
    Compute the full reward signal for a WebShop episode.

    Parameters
    ----------
    predict_str  : Full model output (all turns concatenated, including tool
                   responses wrapped in <tool_response>…</tool_response>).
    ground_truth : Target ASIN (kept for logging / debugging, not used for scoring).
    extra_info   : Optional runtime metadata dict.

    Returns
    -------
    dict with keys: score, acc, tool_bonus
    """
    acc = acc_reward(predict_str)

    # Optional small shaping bonus for making productive search calls
    # tool_bonus = 0.0
    # if extra_info:
    #     tool_rewards = extra_info.get("tool_rewards", [])
    #     productive   = sum(
    #         1 for r in tool_rewards if isinstance(r, (int, float)) and r > 0
    #     )
    #     tool_bonus = min(productive * 0.05, 0.15)

    # total = max(-0.1, min(acc + tool_bonus, 1.15))

    return {
        "score":      acc,
        "acc":        acc,
        # "tool_bonus": tool_bonus,
    }