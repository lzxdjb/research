"""Fallback reward parser for PAG-formatted HotpotQA transcripts."""

import re
import string
from collections import Counter
from typing import Optional

_ATTEMPT_RE = re.compile(r"<ATTEMPT>(.*?)(?:</ATTEMPT>|$)", re.DOTALL | re.IGNORECASE)
_ANSWER_RE = re.compile(r"Answer\s*:\s*(.+)", re.IGNORECASE)
_VERIFY_RE = re.compile(r"<VERIFY>(.*?)(?:</VERIFY>|$)", re.DOTALL | re.IGNORECASE)
_JUDGMENT_RE = re.compile(
    r"Judg(?:e)?ment\s*:\s*(INCORRECT|WRONG|CORRECT|YES|NO|REJECTED|ACCEPTED)\b",
    re.IGNORECASE,
)
FORMAT_PENALTY = -0.1


def _normalise(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = text.translate(str.maketrans("", "", string.punctuation))
    return " ".join(text.split())


def _token_f1(prediction: str, ground_truth: str) -> float:
    pred_tokens = _normalise(prediction).split()
    gt_tokens = _normalise(ground_truth).split()
    if not pred_tokens or not gt_tokens:
        return 0.0
    common = Counter(pred_tokens) & Counter(gt_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gt_tokens)
    return 2 * precision * recall / (precision + recall)


def _extract_answer(block: str) -> str:
    m = _ANSWER_RE.search(block)
    if m:
        return m.group(1).strip()
    return ""


def _judgment_to_bool(block: str) -> bool | None:
    m = _JUDGMENT_RE.search(block)
    if m:
        label = m.group(1).lower()
        if label in {"correct", "yes", "accepted"}:
            return True
        if label in {"incorrect", "wrong", "no", "rejected"}:
            return False

    wrong = re.search(r"\b(incorrect|wrong|not\s+correct|rejected|reject)\b", block, re.IGNORECASE)
    correct = re.search(r"\b(correct|right|accepted|accept)\b", block, re.IGNORECASE)
    if wrong and (not correct or wrong.start() <= correct.start()):
        return False
    if correct:
        return True
    return None


def _is_validation(extra_info: Optional[dict]) -> bool:
    if not extra_info:
        return False
    for key in ("validate", "is_validate", "_agent_loop_validate", "pag_validate"):
        value = extra_info.get(key)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes", "y", "on"}:
                return True
        elif value:
            return True
    return False


def _format_penalty_result(
    ground_truth: str,
    prediction: str = "",
    pag_attempts: int = 0,
    pag_verifier_count: int = 0,
    mode: str = "train",
) -> dict:
    return {
        "score": FORMAT_PENALTY,
        "acc": FORMAT_PENALTY,
        "f1_partial": 0.0,
        "format_ok": 0.0,
        "pag_mode": mode,
        "pag_answer_reward_sum": 0.0,
        "pag_verifier_reward_sum": 0.0,
        "pag_attempts": pag_attempts,
        "pag_verifier_count": pag_verifier_count,
        "prediction": prediction,
        "ground_truth": ground_truth,
    }


def compute_validation_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    attempt_blocks = [m.group(1) for m in _ATTEMPT_RE.finditer(predict_str)]
    final_answer = _extract_answer(attempt_blocks[-1]) if attempt_blocks else ""
    if not final_answer:
        return _format_penalty_result(
            ground_truth=ground_truth,
            pag_attempts=len(attempt_blocks),
            pag_verifier_count=len([m.group(1) for m in _VERIFY_RE.finditer(predict_str)]),
            mode="validation",
        )

    acc = 1.0 if _normalise(final_answer) == _normalise(ground_truth) else 0.0
    return {
        "score": acc,
        "acc": acc,
        "f1_partial": 0.0,
        "format_ok": 1.0,
        "pag_mode": "validation",
        "pag_answer_reward_sum": acc,
        "pag_verifier_reward_sum": 0.0,
        "pag_attempts": len(attempt_blocks),
        "pag_verifier_count": len([m.group(1) for m in _VERIFY_RE.finditer(predict_str)]),
        "prediction": final_answer,
        "ground_truth": ground_truth,
    }


def compute_train_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    attempt_blocks = [m.group(1) for m in _ATTEMPT_RE.finditer(predict_str)]
    verify_blocks = [m.group(1) for m in _VERIFY_RE.finditer(predict_str)]
    attempts = [_extract_answer(block) for block in attempt_blocks]
    judgments = [_judgment_to_bool(block) for block in verify_blocks]

    final_answer = attempts[-1] if attempts else ""
    format_ok = (
        bool(attempts)
        and all(answer for answer in attempts)
        and len(verify_blocks) == len(attempts)
        and all(decision is not None for decision in judgments)
    )
    if not format_ok:
        return _format_penalty_result(
            ground_truth=ground_truth,
            prediction=final_answer,
            pag_attempts=len(attempt_blocks),
            pag_verifier_count=len(verify_blocks),
            mode="train",
        )

    attempt_correct = [_normalise(answer) == _normalise(ground_truth) for answer in attempts]

    answer_rewards = [1.0 if is_correct else 0.0 for is_correct in attempt_correct]
    verifier_rewards = []
    for i, decision in enumerate(judgments[: len(attempt_correct)]):
        verifier_rewards.append(1.0 if decision is not None and decision == attempt_correct[i] else 0.0)

    final_acc = 1.0 if _normalise(final_answer) == _normalise(ground_truth) and final_answer else 0.0
    final_f1 = _token_f1(final_answer, ground_truth) if final_answer else 0.0
    total = float(sum(answer_rewards) + sum(verifier_rewards))

    return {
        "score": total,
        "acc": final_acc,
        "f1_partial": 0.0 if final_acc else min(final_f1 * 0.5, 0.5),
        "format_ok": 1.0,
        "pag_mode": "train",
        "pag_answer_reward_sum": float(sum(answer_rewards)),
        "pag_verifier_reward_sum": float(sum(verifier_rewards)),
        "pag_attempts": len(attempts),
        "pag_verifier_count": len(verify_blocks),
        "prediction": final_answer,
        "ground_truth": ground_truth,
    }


def compute_score(
    predict_str: str,
    ground_truth: str,
    extra_info: Optional[dict] = None,
) -> dict:
    if _is_validation(extra_info):
        return compute_validation_score(predict_str, ground_truth, extra_info)
    return compute_train_score(predict_str, ground_truth, extra_info)
