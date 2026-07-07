"""Reward used to train the small reward model R_phi.

R_phi is trained as a generative judge: it reads a trajectory and returns JSON.
This reward compares R_phi's JSON with labels produced by the local 122B
teacher. It supports both score-regression examples and pairwise-preference
examples.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        parsed = json.loads(match.group(0)) if match else {}
    return parsed if isinstance(parsed, dict) else {}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _score_regression(candidate: dict[str, Any], gt: dict[str, Any]) -> dict[str, Any]:
    teacher = _as_dict(gt.get("teacher_label"))
    target_score = _float(gt.get("score", teacher.get("score", 0.0)))
    pred_score = _float(candidate.get("score", 0.0))
    score_error = abs(pred_score - target_score)
    score_fit = max(0.0, 1.0 - score_error)

    metric_names = ["safety", "task_success", "tool_use", "customer_helpfulness"]
    metric_fits = []
    for name in metric_names:
        if name in teacher and name in candidate:
            metric_fits.append(max(0.0, 1.0 - abs(_float(candidate[name]) - _float(teacher[name]))))
    metric_fit = sum(metric_fits) / len(metric_fits) if metric_fits else 0.5

    reason = str(candidate.get("reason", "")).strip()
    has_reason = bool(reason and len(reason.split()) >= 3)
    valid_range = -1.0 <= pred_score <= 1.0

    reward = 0.65 * score_fit + 0.20 * metric_fit + 0.10 * float(has_reason) + 0.05 * float(valid_range)
    return {
        "score": max(-1.0, min(1.0, reward)),
        "score_error": score_error,
        "score_fit": score_fit,
        "metric_fit": metric_fit,
        "has_reason": float(has_reason),
    }


def _score_pairwise(candidate: dict[str, Any], gt: dict[str, Any]) -> dict[str, Any]:
    target = str(gt.get("winner", "")).upper()
    pred = str(candidate.get("winner") or candidate.get("choice") or "").upper()
    if pred not in {"A", "B", "TIE"}:
        score_a = _float(candidate.get("score_a", candidate.get("score_A", 0.0)))
        score_b = _float(candidate.get("score_b", candidate.get("score_B", 0.0)))
        margin = score_a - score_b
        pred = "A" if margin > 0.05 else "B" if margin < -0.05 else "TIE"
    correct = pred == target
    return {"score": 1.0 if correct else -0.5, "pairwise_correct": float(correct), "winner": pred}


def _score_one(solution_str: str, ground_truth: Any) -> dict[str, Any]:
    gt = _as_dict(ground_truth)
    candidate = _extract_json(solution_str)
    if not candidate:
        return {"score": -1.0, "valid_json": 0.0}
    task = gt.get("task", "score")
    if task == "pairwise":
        result = _score_pairwise(candidate, gt)
    else:
        result = _score_regression(candidate, gt)
    result["valid_json"] = 1.0
    result["reward_backend"] = "teacher_label_agreement"
    return result


def compute_score(
    data_source: str | None = None,
    solution_str: str | None = None,
    ground_truth: Any | None = None,
    extra_info: dict[str, Any] | None = None,
    solution_strs: list[str] | None = None,
    ground_truths: list[Any] | None = None,
    **kwargs,
):
    if solution_strs is not None:
        ground_truths = ground_truths or [None] * len(solution_strs)
        return [_score_one(solution, gt) for solution, gt in zip(solution_strs, ground_truths, strict=False)]
    return _score_one(solution_str or "", ground_truth)
