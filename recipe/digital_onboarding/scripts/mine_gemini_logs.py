#!/usr/bin/env python3
"""Mine existing Gemini/runtime logs into candidate SFT JSONL.

This is deliberately conservative: it only extracts obvious message arrays from
JSON/JSONL logs and writes conversations that contain at least one onboarding
tool marker or tool-call-like object. Use the output for inspection before SFT.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TOOL_NAMES = {
    "send_verification_code",
    "login_and_get_token",
    "query_progress",
    "collect_information",
    "submit_application",
    "capture_document",
    "extract_document_info",
}


def _load(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else [obj]
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows


def _find_messages(obj: Any) -> list[list[dict[str, Any]]]:
    found = []
    if isinstance(obj, dict):
        for key in ("messages", "conversation", "turns"):
            value = obj.get(key)
            if isinstance(value, list) and all(isinstance(x, dict) for x in value):
                if any("role" in x and "content" in x for x in value):
                    found.append(value)
        for value in obj.values():
            found.extend(_find_messages(value))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_find_messages(value))
    return found


def _contains_tool(messages: list[dict[str, Any]]) -> bool:
    raw = json.dumps(messages, ensure_ascii=False)
    return any(name in raw for name in TOOL_NAMES)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="data/digital_onboarding/mined_policy_sft.jsonl")
    args = parser.parse_args()

    rows = []
    for obj in _load(Path(args.input).expanduser()):
        for messages in _find_messages(obj):
            if len(messages) >= 2 and _contains_tool(messages):
                rows.append({"messages": messages, "enable_thinking": "</think>" in json.dumps(messages)})

    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Wrote {len(rows)} candidate SFT conversations to {output}")


if __name__ == "__main__":
    main()

