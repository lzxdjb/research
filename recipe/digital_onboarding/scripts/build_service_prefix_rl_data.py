#!/usr/bin/env python3
"""Build service-model prefix RL data from completed onboarding trajectories.

Each completed trajectory can produce several RL prompts:

- turn 1: original system/user prompt;
- turn 2: original prompt + previous service turn + customer/tool replies;
- turn 3: same idea, and so on.

Previous service turns live in the prompt, so VERL masks them as prompt tokens.
Only the newly generated continuation is trained.  The builder also stores a
``reward_prefix`` so reward functions can judge prefix + continuation, and a
tool-state snapshot so the onboarding backend resumes consistently mid-dialog.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any, Iterable

from recipe.digital_onboarding.scenario import DEFAULT_REQUIRED_FIELDS, SYSTEM_PROMPT

MARKER = "ONBOARDING_TOOL_RESULT"
ROLE_MARKER_RE = re.compile(r"(?m)^(system|user|assistant|tool)\n")
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _iter_paths(values: list[str]) -> list[Path]:
    paths: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.rglob("*.jsonl")))
        else:
            paths.append(path)
    return paths


def _iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if line:
                yield line_no, json.loads(line)


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


def _score_from_row(row: dict[str, Any], default: float = 0.0) -> float:
    candidates = [
        row.get("score"),
        row.get("reward"),
        row.get("teacher_label", {}).get("score") if isinstance(row.get("teacher_label"), dict) else None,
        row.get("reward_model", {}).get("score") if isinstance(row.get("reward_model"), dict) else None,
    ]
    for value in candidates:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _scenario_from_row(row: dict[str, Any]) -> dict[str, Any]:
    reward_model = _as_dict(row.get("reward_model"))
    extra_info = _as_dict(row.get("extra_info"))
    candidates = [
        row.get("scenario_json"),
        row.get("scenario"),
        row.get("gts"),
        row.get("ground_truth"),
        extra_info.get("scenario_json"),
        extra_info.get("scenario"),
        reward_model.get("ground_truth"),
    ]
    for value in candidates:
        parsed = _as_dict(value)
        if parsed:
            return parsed
    return {}


def _coerce_messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            text = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        else:
            text = str(content)
        if MARKER in text:
            role = "tool"
        if text.strip():
            messages.append({"role": role, "content": text.strip()})
    return messages


def _normalize_role_markers(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Some decoded chat templates glue the next role marker directly after the
    # previous content, e.g. "</tool_call>user\n".  Put known role markers on a
    # fresh line before splitting.  This is intentionally heuristic; structured
    # `messages` logs are preferred when available.
    return re.sub(r"(?<!\n)(?=(?:system|user|assistant|tool)\n)", "\n", text)


def _messages_from_text(text: str) -> list[dict[str, str]]:
    text = _normalize_role_markers(text)
    parts = ROLE_MARKER_RE.split(text)
    messages: list[dict[str, str]] = []
    # parts[0] is any preamble before the first role marker.
    for i in range(1, len(parts), 2):
        role = parts[i]
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if MARKER in content:
            role = "tool"
        if content:
            messages.append({"role": role, "content": content})
    return messages


def _messages_from_row(row: dict[str, Any]) -> list[dict[str, str]]:
    for key in ("messages", "trajectory_messages", "conversation", "full_messages"):
        messages = _coerce_messages(row.get(key))
        if messages:
            return messages

    if row.get("input") is not None and row.get("output") is not None:
        return _messages_from_text(str(row.get("input", "")) + str(row.get("output", "")))

    for key in ("trajectory", "solution_str", "response", "text", "completion"):
        if row.get(key):
            messages = _messages_from_text(str(row[key]))
            if messages:
                return messages
    return []


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _initial_state(scenario: dict[str, Any]) -> dict[str, Any]:
    profile = copy.deepcopy(scenario.get("profile", {}))
    required_fields = copy.deepcopy(scenario.get("required_fields", DEFAULT_REQUIRED_FIELDS))
    collected = copy.deepcopy(scenario.get("initial_collected", {}))
    return {
        "scenario_id": scenario.get("scenario_id", "unknown"),
        "profile": profile,
        "required_fields": required_fields,
        "collected_fields": collected,
        "authenticated": False,
        "verification_sent": False,
        "verification_contact": None,
        "verification_contact_type": None,
        "trading_token": None,
        "submitted": False,
        "submission_attempted": False,
        "document_captured": False,
        "document_extracted": False,
        "used_widgets": [],
        "events": [],
        "errors": [],
    }


def _profile_value_for_key(state: dict[str, Any], key: str) -> Any:
    if key in state.get("profile", {}):
        return copy.deepcopy(state["profile"][key])
    return copy.deepcopy(state.get("collected_fields", {}).get(key, True))


def _extract_json_after_marker(text: str) -> dict[str, Any]:
    marker_pos = text.find(MARKER)
    if marker_pos < 0:
        return {}
    raw = text[marker_pos + len(MARKER) :].strip()
    start = raw.find("{")
    if start < 0:
        return {}
    raw = raw[start:]
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        depth = 0
        end = None
        for i, ch in enumerate(raw):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                parsed = json.loads(raw[:end])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _extract_tool_calls(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in TOOL_CALL_RE.finditer(text or ""):
        try:
            call = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(call, dict):
            continue
        args = call.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": str(call.get("name", "")), "arguments": args})
    return calls


def _apply_tool_result_summary(
    state: dict[str, Any],
    tool_text: str,
    pending_call: dict[str, Any] | None,
) -> dict[str, Any]:
    result = _extract_json_after_marker(tool_text)
    summary = result.get("state") if isinstance(result.get("state"), dict) else {}
    if not summary:
        return state

    next_state = copy.deepcopy(state)
    for key in (
        "scenario_id",
        "authenticated",
        "verification_sent",
        "submitted",
        "submission_attempted",
        "document_captured",
        "document_extracted",
        "used_widgets",
        "errors",
    ):
        if key in summary:
            next_state[key] = copy.deepcopy(summary[key])

    collected_keys = summary.get("collected_fields")
    if isinstance(collected_keys, dict):
        next_state["collected_fields"] = copy.deepcopy(collected_keys)
    elif isinstance(collected_keys, list):
        next_state["collected_fields"] = {key: _profile_value_for_key(next_state, key) for key in collected_keys}

    if next_state.get("authenticated") and not next_state.get("trading_token"):
        next_state["trading_token"] = f"token_{next_state.get('scenario_id', 'unknown')}"

    if pending_call and pending_call.get("name") == "send_verification_code" and next_state.get("verification_sent"):
        args = pending_call.get("arguments", {})
        next_state["verification_contact"] = args.get("contact")
        next_state["verification_contact_type"] = str(args.get("contact_type", "")).upper() or None

    return next_state


def _messages_to_transcript(messages: list[dict[str, str]], include_system: bool = False) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        if role == "system" and not include_system:
            continue
        lines.append(f"{role}: {message.get('content', '')}")
    return "\n".join(lines)


def _assistant_turn_count(messages: list[dict[str, str]]) -> int:
    return sum(1 for message in messages if message.get("role") == "assistant")


def _state_signature(state: dict[str, Any]) -> str:
    compact = {
        "auth": state.get("authenticated"),
        "sent": state.get("verification_sent"),
        "submitted": state.get("submitted"),
        "collected": sorted(state.get("collected_fields", {}).keys()),
        "widgets": sorted(state.get("used_widgets", [])),
        "doc": [state.get("document_captured"), state.get("document_extracted")],
    }
    return hashlib.sha1(_json_dumps(compact).encode("utf-8")).hexdigest()[:12]


def _assistant_is_meaningful(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 8:
        return False
    if "<tool_call>" in stripped:
        return True
    if "?" in stripped:
        return True
    action_words = (
        "verify",
        "verification",
        "confirm",
        "submit",
        "account",
        "kyc",
        "document",
        "license",
        "email",
        "mobile",
        "phone",
    )
    return any(word in stripped.lower() for word in action_words)


def _select_prefix(
    *,
    policy: str,
    source_score: float,
    min_score: float,
    success_score: float,
    assistant_text: str,
    repeated_assistant: bool,
) -> tuple[bool, str]:
    if policy == "all":
        return True, "all"

    meaningful = _assistant_is_meaningful(assistant_text)
    if not meaningful:
        return False, "not_meaningful"
    if repeated_assistant:
        return False, "repeated_assistant"

    if policy == "successful":
        return source_score >= success_score, "successful" if source_score >= success_score else "below_success_score"
    if policy == "recoverable":
        keep = min_score <= source_score < success_score
        return keep, "recoverable" if keep else "outside_recoverable_score"

    keep = source_score >= min_score
    return keep, "useful" if keep else "below_min_score"


def _prefix_rows_from_trajectory(
    *,
    row: dict[str, Any],
    messages: list[dict[str, str]],
    scenario: dict[str, Any],
    source_score: float,
    source_ref: str,
    selection_policy: str,
    min_score: float,
    success_score: float,
    max_prefixes_per_trajectory: int,
    min_prefix_turn: int,
    max_prefix_turn: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    scenario_json = _json_dumps(scenario)
    state = _initial_state(scenario)
    pending_tool_calls: list[dict[str, Any]] = []
    previous_assistant_texts: set[str] = set()
    rows: list[dict[str, Any]] = []
    counters = {"candidate_prefixes": 0, "kept_prefixes": 0, "dropped_prefixes": 0}
    total_assistant_turns = _assistant_turn_count(messages)

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "assistant":
            counters["candidate_prefixes"] += 1
            assistant_turn = counters["candidate_prefixes"]
            assistant_text = message.get("content", "")
            assistant_fingerprint = hashlib.sha1(assistant_text.strip().encode("utf-8")).hexdigest()
            repeated = assistant_fingerprint in previous_assistant_texts
            previous_assistant_texts.add(assistant_fingerprint)

            keep, reason = _select_prefix(
                policy=selection_policy,
                source_score=source_score,
                min_score=min_score,
                success_score=success_score,
                assistant_text=assistant_text,
                repeated_assistant=repeated,
            )
            if assistant_turn < min_prefix_turn:
                keep, reason = False, "before_min_prefix_turn"
            if max_prefix_turn and assistant_turn > max_prefix_turn:
                keep, reason = False, "after_max_prefix_turn"
            if max_prefixes_per_trajectory and len(rows) >= max_prefixes_per_trajectory:
                keep, reason = False, "trajectory_prefix_limit"

            if keep:
                prefix_messages = copy.deepcopy(messages[:index])
                if not any(item.get("role") == "system" for item in prefix_messages):
                    prefix_messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

                reward_prefix = _messages_to_transcript(prefix_messages, include_system=False)
                state_snapshot = copy.deepcopy(state)
                state_snapshot["events"] = []
                row_id = hashlib.sha1(
                    f"{source_ref}:{assistant_turn}:{_state_signature(state_snapshot)}".encode("utf-8")
                ).hexdigest()[:16]
                rows.append(
                    {
                        "data_source": "digital_onboarding_service_prefix_rl",
                        "prompt": prefix_messages,
                        "ability": "tool_use_onboarding",
                        "reward_model": {"style": "rule", "ground_truth": scenario_json},
                        "extra_info": {
                            "split": "all",
                            "index": 0,
                            "need_tools_kwargs": True,
                            "tools_kwargs": {
                                "__onboarding_scenario_json__": scenario_json,
                                "__onboarding_state__": _json_dumps(state_snapshot),
                            },
                            "interaction_kwargs": {"name": "onboarding_user", "scenario_json": scenario_json},
                            "scenario_json": scenario_json,
                            "scenario_id": scenario.get("scenario_id"),
                            "source_ref": source_ref,
                            "source_score": source_score,
                            "selection_reason": reason,
                            "prefix_turn": assistant_turn,
                            "total_assistant_turns": total_assistant_turns,
                            "reward_prefix": reward_prefix,
                            "state_signature": _state_signature(state_snapshot),
                            "row_id": row_id,
                        },
                        "agent_name": "tool_agent",
                    }
                )
                counters["kept_prefixes"] += 1
            else:
                counters["dropped_prefixes"] += 1

            pending_tool_calls.extend(_extract_tool_calls(assistant_text))

        elif role == "tool" or MARKER in message.get("content", ""):
            pending = pending_tool_calls.pop(0) if pending_tool_calls else None
            state = _apply_tool_result_summary(state, message.get("content", ""), pending)

    return rows, counters


def _split_rows(
    rows: list[dict[str, Any]],
    val_ratio: float,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    rows = list(rows)
    random.Random(seed).shuffle(rows)
    if len(rows) == 1:
        train_row = rows[0]
        val_row = copy.deepcopy(rows[0])
        train_row["extra_info"]["split"] = "train"
        train_row["extra_info"]["index"] = 0
        val_row["extra_info"]["split"] = "val"
        val_row["extra_info"]["index"] = 0
        return [train_row], [val_row]
    val_count = max(1, int(len(rows) * val_ratio)) if val_ratio > 0 else 0
    val_rows = rows[:val_count]
    train_rows = rows[val_count:]
    for index, item in enumerate(train_rows):
        item["extra_info"]["split"] = "train"
        item["extra_info"]["index"] = index
    for index, item in enumerate(val_rows):
        item["extra_info"]["split"] = "val"
        item["extra_info"]["index"] = index
    return train_rows, val_rows


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    import datasets

    datasets.Dataset.from_list(rows).to_parquet(str(path))


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        prompt_signature = hashlib.sha1(_json_dumps(row["prompt"]).encode("utf-8")).hexdigest()
        if prompt_signature in seen:
            continue
        seen.add(prompt_signature)
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        action="append",
        required=True,
        help="Trajectory JSONL file or directory. Repeat for multiple sources.",
    )
    parser.add_argument("--output-dir", default="data/digital_onboarding/service_prefix_rl")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=23)
    parser.add_argument(
        "--selection-policy",
        choices=["useful", "all", "successful", "recoverable"],
        default="useful",
        help="Prefix filter. useful keeps meaningful non-repeated prefixes above --min-score.",
    )
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--success-score", type=float, default=0.75)
    parser.add_argument("--max-prefixes-per-trajectory", type=int, default=8)
    parser.add_argument("--min-prefix-turn", type=int, default=1)
    parser.add_argument("--max-prefix-turn", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--max-rows", type=int, default=0, help="0 means no global limit.")
    parser.add_argument("--no-dedupe", action="store_true")
    parser.add_argument("--jsonl-only", action="store_true")
    args = parser.parse_args()

    all_rows: list[dict[str, Any]] = []
    stats = {
        "source_rows": 0,
        "rows_without_scenario": 0,
        "rows_without_messages": 0,
        "candidate_prefixes": 0,
        "kept_prefixes": 0,
        "dropped_prefixes": 0,
    }

    for path in _iter_paths(args.input):
        for line_no, source_row in _iter_jsonl(path):
            stats["source_rows"] += 1
            scenario = _scenario_from_row(source_row)
            if not scenario:
                stats["rows_without_scenario"] += 1
                continue
            messages = _messages_from_row(source_row)
            if not messages:
                stats["rows_without_messages"] += 1
                continue
            rows, counters = _prefix_rows_from_trajectory(
                row=source_row,
                messages=messages,
                scenario=scenario,
                source_score=_score_from_row(source_row),
                source_ref=f"{path}:{line_no}",
                selection_policy=args.selection_policy,
                min_score=args.min_score,
                success_score=args.success_score,
                max_prefixes_per_trajectory=args.max_prefixes_per_trajectory,
                min_prefix_turn=args.min_prefix_turn,
                max_prefix_turn=args.max_prefix_turn,
            )
            all_rows.extend(rows)
            for key, value in counters.items():
                stats[key] += value

    if not args.no_dedupe:
        before = len(all_rows)
        all_rows = _dedupe_rows(all_rows)
        stats["deduped_prefixes"] = before - len(all_rows)

    if args.max_rows and len(all_rows) > args.max_rows:
        random.Random(args.seed).shuffle(all_rows)
        all_rows = all_rows[: args.max_rows]
        stats["max_rows_applied"] = args.max_rows

    if not all_rows:
        raise RuntimeError("No prefix rows were selected. Try --selection-policy all or lower --min-score.")

    train_rows, val_rows = _split_rows(all_rows, args.val_ratio, args.seed)
    out = Path(args.output_dir).expanduser()
    _write_jsonl(train_rows, out / "service_prefix_rl_train.jsonl")
    _write_jsonl(val_rows, out / "service_prefix_rl_val.jsonl")
    if not args.jsonl_only:
        _write_parquet(train_rows, out / "service_prefix_rl_train.parquet")
        _write_parquet(val_rows, out / "service_prefix_rl_val.parquet")

    stats.update(
        {
            "train_rows": len(train_rows),
            "val_rows": len(val_rows),
            "output_dir": str(out),
            "selection_policy": args.selection_policy,
        }
    )
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
