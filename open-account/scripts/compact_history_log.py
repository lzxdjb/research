#!/usr/bin/env python3
"""Compact streamed Gemini text events from a history.log file.

Example:
    python open-account/scripts/compact_history_log.py open-account/history.log
    python open-account/scripts/compact_history_log.py open-account/history.log \
        --output open-account/history.compacted.txt
    python open-account/scripts/compact_history_log.py open-account/history.log \
        --include-logs --output open-account/history.compacted.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


EVENT_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r".*? run_session event: (?P<payload>\{.*)$"
)
FALLBACK_TYPE_TEXT_RE = re.compile(
    r'"type"\s*:\s*"(?P<role>gemini|user)"\s*,\s*'
    r'"text"\s*:\s*"(?P<text>(?:\\.|[^"\\])*)"'
)
FALLBACK_TYPE_RE = re.compile(r'"type"\s*:\s*"(?P<event_type>[^"]+)"')
FALLBACK_NAME_RE = re.compile(r'"name"\s*:\s*"(?P<name>[^"]+)"')
LOG_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (?P<rest>.*)$"
)


@dataclass
class ParsedEvent:
    event_type: str | None
    payload: str
    data: dict[str, Any] | None


def _decode_json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def parse_event(line: str) -> ParsedEvent | None:
    """Return a parsed run_session event from a log line, if one is present."""
    match = EVENT_RE.match(line)
    if not match:
        return None

    payload = match.group("payload")
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        fallback = FALLBACK_TYPE_RE.search(payload)
        return ParsedEvent(
            event_type=fallback.group("event_type") if fallback else None,
            payload=payload,
            data=None,
        )

    event_type = event.get("type")
    return ParsedEvent(
        event_type=event_type if isinstance(event_type, str) else None,
        payload=payload,
        data=event,
    )


def event_text(event: ParsedEvent) -> str | None:
    if event.data is not None:
        text = event.data.get("text")
        return text if isinstance(text, str) else None

    fallback = FALLBACK_TYPE_TEXT_RE.search(event.payload)
    if not fallback:
        return None
    return _decode_json_string(fallback.group("text"))


def append_text_part(parts: list[str], text: str) -> None:
    if parts and parts[-1] and text:
        previous = parts[-1][-1]
        next_char = text[0]
        if (
            previous in ".!?,;:"
            and not previous.isspace()
            and not next_char.isspace()
            and next_char.isalnum()
        ):
            parts.append(" ")
    parts.append(text)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_event(event: ParsedEvent) -> str:
    event_type = event.event_type or "event"
    if event.data is None:
        name = FALLBACK_NAME_RE.search(event.payload)
        if event_type == "tool_call" and name:
            return f"tool_call: {name.group('name')} | raw={event.payload}"
        return f"{event_type}: {event.payload}"

    if event_type == "user":
        text = event_text(event)
        return f"user: {text}" if text is not None else f"user: {_json_dumps(event.data)}"

    if event_type == "tool_call":
        name = event.data.get("name")
        label = f"tool_call: {name}" if isinstance(name, str) else "tool_call"
        parts = [label]
        if "args" in event.data:
            parts.append(f"args={_json_dumps(event.data['args'])}")
        if "result" in event.data:
            parts.append(f"result={_json_dumps(event.data['result'])}")
        return " | ".join(parts)

    if event_type == "error":
        error = event.data.get("error")
        return f"error: {error}" if isinstance(error, str) else f"error: {_json_dumps(event.data)}"

    if event_type in {"interrupted", "turn_complete"}:
        return event_type

    return f"{event_type}: {_json_dumps(event.data)}"


def strip_timestamp(line: str) -> str:
    match = LOG_TIMESTAMP_RE.match(line)
    return match.group("rest") if match else line


def compact_log_lines(lines: Iterable[str], include_logs: bool = False) -> str:
    rows: list[str] = []
    gemini_parts: list[str] = []

    def flush() -> None:
        nonlocal gemini_parts
        if not gemini_parts:
            return
        rows.append(f"gemini: {''.join(gemini_parts).strip()}")
        gemini_parts = []

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        event = parse_event(line)
        if event is None:
            if include_logs and line:
                flush()
                rows.append(strip_timestamp(line))
            continue

        if event.event_type == "gemini":
            text = event_text(event)
            if text is not None:
                append_text_part(gemini_parts, text)
                continue

        flush()
        rows.append(render_event(event))

    flush()
    return "\n".join(rows) + ("\n" if rows else "")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compact streamed Gemini chunks while keeping other event information."
    )
    parser.add_argument("input", type=Path, help="Path to history.log")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file. Defaults to stdout.",
    )
    parser.add_argument(
        "--include-logs",
        action="store_true",
        help="Also keep non-run_session log lines, with timestamp prefixes removed.",
    )
    parser.add_argument(
        "--no-timestamps",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    with args.input.open("r", encoding="utf-8", errors="replace") as source:
        rendered = compact_log_lines(source, include_logs=args.include_logs)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
