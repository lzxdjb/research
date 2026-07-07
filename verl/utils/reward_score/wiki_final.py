"""Strict EM reward for Wikipedia-style QA with ``<FINAL>`` answers.

The checker is intentionally small and deterministic:
  - malformed or missing ``<FINAL>...</FINAL>`` block -> -0.1
  - normalized exact match with the ground truth or an alias -> 1.0
  - well-formed but incorrect answer -> 0.0
"""

from __future__ import annotations

import json
import re
import string
from typing import Any

_FINAL_RE = re.compile(r"<FINAL>(.*?)</FINAL>", re.DOTALL | re.IGNORECASE)
_ANSWER_PREFIX_RE = re.compile(r"^\s*Answer\s*:\s*", re.IGNORECASE)
_TOOL_RESPONSE_RE = re.compile(r"<tool_response>.*?</tool_response>", re.DOTALL | re.IGNORECASE)


def normalize_answer(text: Any) -> str:
    """HotpotQA/SQuAD-style answer normalization."""

    text = str(text or "").lower().strip()
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = text.translate(str.maketrans("", "", string.punctuation))
    return " ".join(text.split())


def extract_final_answer(solution_str: str) -> tuple[bool, str]:
    """Return ``(format_ok, answer)`` from the last ``<FINAL>`` block."""

    clean = _TOOL_RESPONSE_RE.sub("", solution_str or "")
    matches = list(_FINAL_RE.finditer(clean))
    if not matches:
        return False, ""

    block = matches[-1].group(1).strip()
    if not block:
        return False, ""

    # Allow either "<FINAL>answer</FINAL>" or an "Answer:" line inside.
    answer = ""
    for line in block.splitlines():
        if _ANSWER_PREFIX_RE.match(line):
            answer = _ANSWER_PREFIX_RE.sub("", line).strip()
            break
    if answer:
        return True, answer

    for line in block.splitlines():
        line = line.strip()
        if line:
            answer = _ANSWER_PREFIX_RE.sub("", line).strip()
            break
    if not answer:
        answer = _ANSWER_PREFIX_RE.sub("", block).strip()
    return bool(answer), answer


def _load_meta(extra_info: dict[str, Any] | None) -> dict[str, Any]:
    if not extra_info:
        return {}
    meta = extra_info.get("meta_json")
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str) and meta.strip():
        try:
            parsed = json.loads(meta)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _candidate_answers(ground_truth: Any, extra_info: dict[str, Any] | None) -> list[str]:
    answers: list[str] = []
    if isinstance(ground_truth, list):
        answers.extend(str(item) for item in ground_truth if str(item).strip())
    elif isinstance(ground_truth, dict):
        target = ground_truth.get("target") or ground_truth.get("answer") or ground_truth.get("ground_truth")
        if isinstance(target, list):
            answers.extend(str(item) for item in target if str(item).strip())
        elif target is not None:
            answers.append(str(target))
    elif ground_truth is not None:
        answers.append(str(ground_truth))

    meta = _load_meta(extra_info)
    aliases = meta.get("answer_aliases") or extra_info.get("answer_aliases") if extra_info else None
    if isinstance(aliases, list):
        answers.extend(str(item) for item in aliases if str(item).strip())

    # Preserve order while deduplicating normalized-equivalent strings.
    seen: set[str] = set()
    unique: list[str] = []
    for answer in answers:
        norm = normalize_answer(answer)
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(answer)
    return unique


def score_prediction(prediction: str, ground_truth: Any, extra_info: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_prediction = normalize_answer(prediction)
    candidates = _candidate_answers(ground_truth, extra_info)
    normalized_candidates = [normalize_answer(answer) for answer in candidates]
    correct = bool(normalized_prediction and normalized_prediction in normalized_candidates)
    return {
        "score": 1.0 if correct else 0.0,
        "acc": 1.0 if correct else 0.0,
        "format_ok": 1.0,
        "prediction": prediction,
        "ground_truth": ground_truth,
        "candidate_answers": candidates,
    }


def compute_score(
    data_source: str | None = None,
    solution_str: str | None = None,
    ground_truth: Any | None = None,
    extra_info: dict[str, Any] | None = None,
    **kwargs,
) -> dict[str, Any]:
    del data_source, kwargs
    format_ok, prediction = extract_final_answer(solution_str or "")
    if not format_ok:
        return {
            "score": -0.1,
            "acc": 0.0,
            "format_ok": 0.0,
            "prediction": "",
            "ground_truth": ground_truth,
            "candidate_answers": _candidate_answers(ground_truth, extra_info),
        }
    return score_prediction(prediction, ground_truth, extra_info)
