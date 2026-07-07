"""Shared debug logging for digital onboarding rollouts."""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None


DEBUG_CSV_FIELDS = [
    "time",
    "request_id",
    "scenario_id",
    "turn",
    "event_type",
    "role",
    "backend",
    "model",
    "endpoint",
    "content",
    "prompt",
    "response",
    "raw_response",
    "tool_calls",
    "tool_responses",
    "reward_score",
    "metadata",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if os.environ.get("DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }:
        text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return text


def debug_enabled() -> bool:
    value = os.environ.get("DIGITAL_ONBOARDING_DEBUG_ENABLED")
    if value is None:
        value = os.environ.get("DIGITAL_ONBOARDING_DEBUG_LOGS")
    if value is None:
        return True
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_filename(value: Any) -> str:
    text = str(value or "unknown")
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return text or "unknown"


def resolve_debug_csv_path(path: str, row: dict[str, Any]) -> Path:
    base_path = Path(path).expanduser()
    if base_path.suffix.lower() == ".csv":
        base_path = base_path.with_suffix("")

    request_id = safe_filename(row.get("request_id"))
    scenario_id = safe_filename(row.get("scenario_id"))
    group_by = os.environ.get("DIGITAL_ONBOARDING_DEBUG_GROUP_BY", "scenario").strip().lower()
    if group_by == "scenario" and scenario_id != "unknown":
        filename = f"{scenario_id}.csv"
    elif scenario_id != "unknown":
        filename = f"{scenario_id}__{request_id}.csv"
    else:
        filename = f"{request_id}.csv"
    return base_path / filename


def append_debug_csv(path: str | None, row: dict[str, Any]) -> None:
    if not path or not debug_enabled():
        return

    output_path = resolve_debug_csv_path(path, row)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("a+", encoding="utf-8", newline="") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0, os.SEEK_END)
            should_write_header = f.tell() == 0
            writer = csv.DictWriter(f, fieldnames=DEBUG_CSV_FIELDS, extrasaction="ignore")
            if should_write_header:
                writer.writeheader()
            normalized = {field: stringify(row.get(field, "")) for field in DEBUG_CSV_FIELDS}
            if not normalized["time"]:
                normalized["time"] = utc_now()
            writer.writerow(normalized)
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
