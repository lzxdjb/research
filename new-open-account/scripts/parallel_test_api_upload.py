#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parallel version of the manual test_api.py upload flow.

Each case runs:
  send_code -> login with 123456 -> token -> upload

Unlike test_api.py, every case uses a unique session file so parallel workers do
not overwrite each other's .session data.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import statistics
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, os.fspath(SCRIPT_DIR))

from api import TradeAPI  # noqa: E402


MASKED_KEYS = {"token", "access_token", "session_id", "sessionid", "cookie", "authorization"}


@dataclass(frozen=True)
class UploadCase:
    index: int
    contact: str
    area_code: str
    verification_code: str
    upload_file: str
    need_thumbnail: bool
    environment: str
    session_file: str


class IsolatedTradeAPI(TradeAPI):
    def __init__(self, *, environment: str, session_file: str):
        self._isolated_session_file = session_file
        super().__init__(environment=environment)

    def _session_path(self) -> str:
        return self._isolated_session_file


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if str(key).lower() in MASKED_KEYS:
                sanitized[key] = "***"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _phase_record(phase: str, started: float, **kwargs: Any) -> dict[str, Any]:
    return {
        "phase": phase,
        "elapsed_s": time.monotonic() - started,
        **kwargs,
    }


def _send_code(api: TradeAPI, case: UploadCase) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = api.send_verification_code(
            contact=case.contact,
            contact_type="MOBILE",
            area_code=case.area_code,
        )
        ok = response.get("i18nMsg") == "success" or response.get("s") == "ok"
        return _phase_record("send_code", started, ok=ok, response=_sanitize(response))
    except Exception as exc:
        return _phase_record(
            "send_code",
            started,
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(limit=5),
        )


def _login(api: TradeAPI, case: UploadCase) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = api.login(
            contact=case.contact,
            verification_code=case.verification_code,
            contact_type="MOBILE",
            area_code=case.area_code,
        )
        ok = response.get("s") == "ok" or bool(response.get("data"))
        return _phase_record("login", started, ok=ok, response=_sanitize(response), user_id=api.userId)
    except Exception as exc:
        return _phase_record(
            "login",
            started,
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(limit=5),
        )


def _token(api: TradeAPI) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = api.get_trading_token()
        ok = response.get("s") == "ok" and bool(api.access_token)
        return _phase_record("token", started, ok=ok, response=_sanitize(response))
    except Exception as exc:
        return _phase_record(
            "token",
            started,
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(limit=5),
        )


def _upload_file_observed(api: TradeAPI, case: UploadCase) -> dict[str, Any]:
    started = time.monotonic()
    if not api.userId or not api.access_token:
        return _phase_record(
            "upload",
            started,
            ok=False,
            error_type="ValueError",
            error="Please complete login and get trading token first",
        )

    endpoint = f"{api.base_url}/api/oas/v1/application/file/upload"
    headers = api._headers(
        {
            "Cookie": api._cookie(),
            "userId": api.userId,
            "Token": api.token,
        }
    )
    headers.pop("Content-Type", None)

    file_path = Path(case.upload_file)
    filename = file_path.name
    try:
        file_bytes = file_path.read_bytes()
        files = {
            "file": (filename, file_bytes, "application/octet-stream"),
            "is_need_min": (None, 1 if case.need_thumbnail else 0),
        }
        response = api._request("POST", endpoint, headers=headers, files=files)
        info: dict[str, Any] = {
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "response_bytes": len(response.content or b""),
            "response_text_prefix": response.text[:500],
        }
        try:
            body = response.json()
        except Exception as exc:
            return _phase_record(
                "upload",
                started,
                ok=False,
                error_type=type(exc).__name__,
                error=str(exc),
                **info,
            )

        ok = body.get("s") == "ok" and bool(body.get("d"))
        return _phase_record("upload", started, ok=ok, response=_sanitize(body), **info)
    except Exception as exc:
        return _phase_record(
            "upload",
            started,
            ok=False,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(limit=5),
        )


def _run_case(case: UploadCase) -> dict[str, Any]:
    started = time.monotonic()
    api = IsolatedTradeAPI(environment=case.environment, session_file=case.session_file)
    api.clear_session()

    phases: list[dict[str, Any]] = []
    for step in (_send_code, _login, _token, _upload_file_observed):
        if step is _token:
            phase = step(api)  # type: ignore[misc]
        else:
            phase = step(api, case)  # type: ignore[misc]
        phases.append(phase)
        if not phase.get("ok"):
            break

    failed_phase = next((phase for phase in phases if not phase.get("ok")), None)
    upload_phase = next((phase for phase in phases if phase.get("phase") == "upload"), None)
    return {
        "index": case.index,
        "contact": case.contact,
        "area_code": case.area_code,
        "ok": failed_phase is None,
        "failed_phase": failed_phase.get("phase") if failed_phase else "",
        "error_type": failed_phase.get("error_type") if failed_phase else "",
        "error": failed_phase.get("error") if failed_phase else "",
        "upload_ok": bool(upload_phase and upload_phase.get("ok")),
        "upload_elapsed_s": upload_phase.get("elapsed_s") if upload_phase else None,
        "elapsed_s": time.monotonic() - started,
        "session_file": case.session_file,
        "phases": phases,
    }


def _resolve_upload_file(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_file():
        return path.resolve()
    script_relative = SCRIPT_DIR / path_text
    if script_relative.is_file():
        return script_relative.resolve()
    raise FileNotFoundError(f"Upload file not found: {path_text}")


def _reserve_unique_phones(
    *,
    count: int,
    digits: int,
    prefix: str,
    history_path: Path,
) -> list[str]:
    if not prefix.isdigit():
        raise ValueError(f"--phone-prefix must be numeric, got: {prefix}")
    if len(prefix) >= digits:
        raise ValueError("--phone-prefix must be shorter than --phone-digits")

    suffix_width = digits - len(prefix)
    capacity = 10**suffix_width
    if count > capacity:
        raise ValueError(f"Cannot allocate {count} numbers with prefix {prefix}; capacity is {capacity}")

    history_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = history_path.with_suffix(history_path.suffix + ".lock")
    start = int(time.time_ns() // 1_000_000) % capacity

    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if history_path.exists():
            used = {line.strip() for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()}
        else:
            used = set()

        phones: list[str] = []
        for offset in range(capacity):
            suffix = (start + offset) % capacity
            phone = f"{prefix}{suffix:0{suffix_width}d}"
            if phone in used:
                continue
            phones.append(phone)
            used.add(phone)
            if len(phones) == count:
                break

        if len(phones) != count:
            raise RuntimeError(f"Only allocated {len(phones)} unique phones with prefix {prefix}")

        with history_path.open("a", encoding="utf-8") as handle:
            for phone in phones:
                handle.write(phone + "\n")
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    return phones


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    upload_latencies = [
        float(row["upload_elapsed_s"])
        for row in records
        if row.get("upload_elapsed_s") is not None
    ]
    failed_phase_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for row in records:
        if row.get("ok"):
            continue
        failed_phase = str(row.get("failed_phase") or "unknown")
        error_type = str(row.get("error_type") or "response_error")
        failed_phase_counts[failed_phase] = failed_phase_counts.get(failed_phase, 0) + 1
        error_counts[error_type] = error_counts.get(error_type, 0) + 1

    return {
        "case_count": len(records),
        "case_ok": sum(1 for row in records if row.get("ok")),
        "case_error_count": sum(1 for row in records if not row.get("ok")),
        "upload_count": sum(1 for row in records if any(p.get("phase") == "upload" for p in row.get("phases", []))),
        "upload_ok": sum(1 for row in records if row.get("upload_ok")),
        "upload_latency_s": {
            "min": min(upload_latencies) if upload_latencies else None,
            "mean": statistics.fmean(upload_latencies) if upload_latencies else None,
            "p50": _percentile(upload_latencies, 0.50),
            "p90": _percentile(upload_latencies, 0.90),
            "p95": _percentile(upload_latencies, 0.95),
            "p99": _percentile(upload_latencies, 0.99),
            "max": max(upload_latencies) if upload_latencies else None,
        },
        "failed_phase_counts": dict(sorted(failed_phase_counts.items(), key=lambda item: item[1], reverse=True)),
        "error_counts": dict(sorted(error_counts.items(), key=lambda item: item[1], reverse=True)),
    }


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = [
        "index",
        "contact",
        "area_code",
        "ok",
        "failed_phase",
        "error_type",
        "error",
        "upload_ok",
        "upload_elapsed_s",
        "elapsed_s",
        "session_file",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in records:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", "--upload-file", dest="upload_file", default="test.png")
    parser.add_argument("--cases", type=int, default=128)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--area-code", default="+1")
    parser.add_argument("--verification-code", default="123456")
    parser.add_argument("--environment", default="test")
    parser.add_argument("--need-thumbnail", action="store_true")
    parser.add_argument("--phone-prefix", default=os.environ.get("PARALLEL_UPLOAD_PHONE_PREFIX", time.strftime("%Y%m")))
    parser.add_argument("--phone-digits", type=int, default=10)
    parser.add_argument(
        "--phone-history",
        default=os.environ.get("PARALLEL_UPLOAD_PHONE_HISTORY", "/tmp/real_bank_parallel_upload_used_phones.txt"),
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output-jsonl", default="")
    parser.add_argument("--connect-timeout", type=float, default=float(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT", "5")))
    parser.add_argument("--read-timeout", type=float, default=float(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT", "30")))
    args = parser.parse_args()

    upload_file = _resolve_upload_file(args.upload_file)
    run_id = time.strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir or tempfile.gettempdir()) / f"parallel_test_api_upload_{run_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_jsonl = Path(args.output_jsonl) if args.output_jsonl else output_dir / "cases.jsonl"
    output_csv = output_dir / "cases.csv"
    sessions_dir = output_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    os.environ["DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT"] = str(args.connect_timeout)
    os.environ["DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT"] = str(args.read_timeout)

    phones = _reserve_unique_phones(
        count=args.cases,
        digits=args.phone_digits,
        prefix=args.phone_prefix,
        history_path=Path(args.phone_history).expanduser(),
    )
    cases = [
        UploadCase(
            index=index,
            contact=phone,
            area_code=args.area_code,
            verification_code=args.verification_code,
            upload_file=os.fspath(upload_file),
            need_thumbnail=args.need_thumbnail,
            environment=args.environment,
            session_file=os.fspath(sessions_dir / f"case_{index:06d}_{phone}.session"),
        )
        for index, phone in enumerate(phones)
    ]

    print(
        json.dumps(
            {
                "event": "parallel_upload_config",
                "cases": args.cases,
                "workers": args.workers,
                "upload_file": os.fspath(upload_file),
                "upload_file_bytes": upload_file.stat().st_size,
                "area_code": args.area_code,
                "verification_code": args.verification_code,
                "phone_prefix": args.phone_prefix,
                "phone_history": os.fspath(Path(args.phone_history).expanduser()),
                "output_jsonl": os.fspath(output_jsonl),
                "output_csv": os.fspath(output_csv),
                "session_dir": os.fspath(sessions_dir),
                "connect_timeout": args.connect_timeout,
                "read_timeout": args.read_timeout,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    records: list[dict[str, Any]] = []
    with output_jsonl.open("w", encoding="utf-8") as out:
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [executor.submit(_run_case, case) for case in cases]
            for future in as_completed(futures):
                row = future.result()
                records.append(row)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()

    records.sort(key=lambda row: int(row.get("index", 0)))
    _write_csv(output_csv, records)
    summary = _summarize(records)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"event": "parallel_upload_summary", **summary}, ensure_ascii=False, indent=2), flush=True)
    print(f"Output dir: {output_dir}", flush=True)
    return 0 if summary["case_error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
