"""
reward_function.py  —  ALFWorld RL reward function.

compute_score(predict_str, ground_truth, extra_info) is the main entry point,
called by the verl trainer after each rollout.

Reward structure
────────────────
┌───────────────────────────────┬───────────┬─────────────────────────────────┐
│ Component                     │ Range     │ Signal source                   │
├───────────────────────────────┼───────────┼─────────────────────────────────┤
│ completion_reward  (primary)  │ -0.1→+1.0 │ env "Task completed" in history │
├───────────────────────────────┼───────────┼─────────────────────────────────┤
│ step_efficiency_bonus         │ 0.0→+0.10 │ fewer steps = higher bonus      │
│ action_diversity_bonus        │ 0.0→+0.10 │ variety of unique action verbs  │
└───────────────────────────────┴───────────┴─────────────────────────────────┘

Total is clipped to [-0.1, 1.2].
"""

import re
from typing import Optional

# ── regex patterns ─────────────────────────────────────────────────────────────

# Matches the model's <FINISHED> block
_FINISHED_RE = re.compile(r"<FINISHED>", re.IGNORECASE)

# Matches a tool_response / observation block
_TOOL_RESPONSE_RE = re.compile(
    r"<tool_response>(.*?)</tool_response>",
    re.DOTALL | re.IGNORECASE,
)

# Matches tool_call blocks (to strip them when analysing final answer)
_TOOL_CALL_RE = re.compile(
    r"<tool_call>(.*?)</tool_call>",
    re.DOTALL | re.IGNORECASE,
)

# Phrase the environment outputs when a task is done
_COMPLETED_PHRASES = [
    "task completed",
    "you have completed",
    "congratulations",
    "you won"
]

# Action verbs we reward for diversity
_ACTION_VERBS = {
    "go", "open", "close", "take", "put", "heat", "cool", "clean",
    "use", "examine", "look", "inventory",
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _extract_observations(predict_str: str) -> list[str]:
    """Pull all environment observations from tool_response blocks."""
    return [m.group(1).strip() for m in _TOOL_RESPONSE_RE.finditer(predict_str)]


def _extract_actions(predict_str: str) -> list[str]:
    """Pull all action strings from tool_call blocks."""
    actions = []
    for m in _TOOL_CALL_RE.finditer(predict_str):
        block = m.group(1)
        # Matches <parameter=action> look </parameter>
        am = re.search(r'<parameter=action>\s*(.*?)\s*</parameter>', block, re.IGNORECASE | re.DOTALL)
        if am:
            actions.append(am.group(1).strip().lower())
    return actions

def _task_completed(observations: list[str]) -> bool:
    """Return True if any observation indicates task completion."""
    for obs in observations:
        obs_lower = obs.lower()
        if any(phrase in obs_lower for phrase in _COMPLETED_PHRASES):
            return True
    return False


def _has_finished_tag(predict_str: str) -> bool:
    return bool(_FINISHED_RE.search(predict_str))


# ── reward components ──────────────────────────────────────────────────────────

def completion_reward(predict_str: str) -> float:
    """
    Primary reward:
      +1.0  if environment confirmed task completion
       0.0  if <FINISHED> was emitted but task was not completed
      -0.1  if <FINISHED> was never emitted (agent gave up / ran out of turns)
    """
    if not _has_finished_tag(predict_str):
        return -0.1

    observations = _extract_observations(predict_str)
    if _task_completed(observations):
        return 1.0

    return 0.0


def step_efficiency_bonus(predict_str: str, max_bonus: float = 0.10) -> float:
    """
    Small bonus for completing the task in fewer steps.
    Only awarded if the task was actually completed.
    Scale: 0 steps → max_bonus; 50+ steps → 0.0
    """
    observations = _extract_observations(predict_str)
    if not _task_completed(observations):
        return 0.0

    num_steps = len(_extract_actions(predict_str))
    if num_steps == 0:
        return 0.0

    # Linear decay: full bonus at ≤5 steps, zero at ≥50
    fraction = max(0.0, 1.0 - (num_steps - 5) / 45)
    return round(max_bonus * fraction, 4)


def action_diversity_bonus(predict_str: str, max_bonus: float = 0.10) -> float:
    """
    Small bonus for using a variety of action types.
    Rewards agents that explore the full action space rather than repeating
    the same action.  Scaled by fraction of distinct verbs used.
    Only awarded if the agent actually attempted the task.
    """
    actions = _extract_actions(predict_str)
    if not actions:
        return 0.0

    verbs_used = set()
    for action in actions:
        verb = action.split()[0] if action else ""
        if verb in _ACTION_VERBS:
            verbs_used.add(verb)

    fraction = len(verbs_used) / len(_ACTION_VERBS)
    return round(max_bonus * fraction, 4)


# ── main entry point ───────────────────────────────────────────────────────────

def compute_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    """
    Compute the full reward for one ALFWorld rollout.

    Parameters
    ----------
    predict_str  : Full model output for the episode (all turns concatenated).
    ground_truth : The task_type string (e.g. "pick_and_place_simple").
                   Not directly used for scoring — completion is inferred from
                   the environment observations embedded in predict_str.
    extra_info   : Optional dict with runtime metadata.

    Returns
    -------
    dict with keys:
      score              — total clipped reward (primary metric)
      completion_reward  — primary signal
      step_bonus         — efficiency shaping
      diversity_bonus    — exploration shaping
      shaping_total      — sum of shaping terms
    """
    # breakpoint()
    comp = completion_reward(predict_str)
    # step = step_efficiency_bonus(predict_str)
    # div  = action_diversity_bonus(predict_str)

    # shaping = step + div
    # total   = max(-0.1, min(comp + shaping, 1.2))

    return {
        "score":             comp,
        "completion_reward": comp,
        # "step_bonus":        step,
        # "diversity_bonus":   div,
        # "shaping_total":     shaping,
    }