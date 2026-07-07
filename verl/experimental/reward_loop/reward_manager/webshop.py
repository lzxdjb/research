"""
webshop_reward.py

Reward function for the WebShop RL task.

Scoring table:
┌─────────────────────────────────────────┬──────────┬─────────────────────────────┐
│ Outcome                                 │ Score    │ Notes                       │
├─────────────────────────────────────────┼──────────┼─────────────────────────────┤
│ Purchased correct product               │ +1.00    │ click[Buy Now] + correct ASIN│
│ Correct product identified, not bought  │ +0.50    │ correct ASIN in response     │
│ <FINISHED> but no valid action          │ -0.10    │ no click[] found             │
│ Wrong product purchased                 │  0.00    │ click[Buy Now], wrong ASIN   │
│ No <FINISHED> at all                    │ -0.10    │ incomplete episode           │
└─────────────────────────────────────────┴──────────┴─────────────────────────────┘
"""

import re
from typing import Optional

# ── Regex helpers ─────────────────────────────────────────────────────────────

_FINISHED_RE    = re.compile(r"<FINISHED>", re.IGNORECASE)
_CLICK_RE       = re.compile(r"click\[([^\]]+)\]", re.IGNORECASE)
_TOOL_RESP_RE   = re.compile(r"<tool_response>.*?</tool_response>", re.DOTALL | re.IGNORECASE)
# ASIN patterns: uppercase letters + digits, 10 chars (real Amazon),
# or 6-digit synthetic IDs used in our dataset
_ASIN_RE        = re.compile(r"\b([A-Z0-9]{6,10})\b")


def _strip_tool_responses(text: str) -> str:
    """Remove tool response blocks so tool-returned ASINs don't pollute scoring."""
    return _TOOL_RESP_RE.sub("", text)


def extract_final_action(predict_str: str) -> Optional[str]:
    """
    Extract the last click[] action from the model's <FINISHED> block.
    Returns the button text (e.g. 'Buy Now') or None.
    """
    # Only look inside / after the <FINISHED> tag
    finished_pos = predict_str.upper().rfind("<FINISHED>")
    if finished_pos == -1:
        return None
    after_finished = predict_str[finished_pos:]
    m = _CLICK_RE.search(after_finished)
    return m.group(1).strip() if m else None


def extract_mentioned_asins(predict_str: str) -> list[str]:
    """
    Return all ASIN-like tokens mentioned in the response (outside tool responses).
    Used as a secondary signal when the model names the product without buying.
    """
    clean = _strip_tool_responses(predict_str)
    # Only look after <FINISHED> if it exists
    finished_pos = clean.upper().rfind("<FINISHED>")
    if finished_pos != -1:
        clean = clean[finished_pos:]
    return _ASIN_RE.findall(clean)


def acc_reward(predict_str: str, ground_truth: str) -> float:
    """
    Primary accuracy reward.

    Returns:
      +1.0  — model clicked "Buy Now" AND the correct ASIN appeared in the response
      +0.5  — model mentioned the correct ASIN but did not complete the purchase
      -0.1  — model emitted <FINISHED> but took no recognisable action
      -0.1  — model never emitted <FINISHED>
       0.0  — model acted (clicked something) but got the wrong product
    """
    gt = str(ground_truth).strip().upper()

    has_finished = bool(_FINISHED_RE.search(predict_str))
    if not has_finished:
        return -0.1

    final_action = extract_final_action(predict_str)

    if final_action is None:
        # <FINISHED> but no click action
        return -0.1

    # Check whether the correct ASIN appears anywhere in the assistant's own text
    # (outside tool responses, after <FINISHED>)
    asins_mentioned = [a.upper() for a in extract_mentioned_asins(predict_str)]
    correct_product_mentioned = gt in asins_mentioned

    if final_action.strip().lower() == "buy now":
        # Full purchase — reward depends on whether it was the right product
        return 1.0 if correct_product_mentioned else 0.0

    if correct_product_mentioned:
        # Model identified the right product but stopped short of buying
        return 0.5

    return 0.0


def compute_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    """
    Compute the full reward signal for a WebShop episode.

    Components
    ──────────
    acc   (primary)   : -0.1 → +1.0   Accuracy of final action
    tool_bonus        : +0.0 → +0.15  Bonus for productive tool calls
                                       (sourced from extra_info["tool_rewards"])

    Total is clipped to [-0.1, 1.15].

    Incentive structure:
      No search, wrong product:          -0.10
      3 searches, wrong product:         +0.05  (tool bonus only)
      3 searches, correct + not bought:  +0.55
      3 searches, correct + bought:      +1.05
    """
    acc = acc_reward(predict_str, ground_truth)

    # Optional tool-use shaping from extra_info
    tool_bonus = 0.0
    if extra_info:
        tool_rewards = extra_info.get("tool_rewards", [])
        # Each productive search call (returned results) gave 0.05
        # Cap the bonus at 0.15 (3 useful calls)
        productive = sum(1 for r in tool_rewards if isinstance(r, (int, float)) and r > 0)
        tool_bonus = min(productive * 0.05, 0.15)

    total = max(-0.1, min(acc + tool_bonus, 1.15))

    return {
        "score":      total,
        "acc":        acc,
        "tool_bonus": tool_bonus,
    }