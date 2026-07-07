#!/usr/bin/env python3
"""Stress-test the real-bank document upload endpoint.

This script isolates the endpoint used by the training upload path. It creates
real-bank scenarios, optionally authenticates each trajectory, then releases a
concurrent burst of upload_file requests and writes per-request timing records.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recipe.digital_onboarding.image_verification import real_bank_upload_image_path
from recipe.digital_onboarding.real_bank import (
    bank_response_ok,
    make_trade_api,
    normalize_file_result,
    prepare_real_bank_scenario,
    sanitize_bank_response,
)
from recipe.digital_onboarding.scenario import make_scenario


UPLOAD_MODES = {"normal", "empty-file", "metadata-only", "query-progress"}
UPLOAD_RELEASE_MODES = {"burst", "immediate"}


@dataclass(frozen=True)
class UploadCase:
    index: int
    trajectory_id: str
    scenario_id: str
    contact: str
    contact_type: str
    area_code: str
    verification_code: str


def _set_env_if_value(name: str, value: str | None) -> None:
    if value not in (None, ""):
        os.environ[name] = str(value)


def _make_upload_cases(args: argparse.Namespace) -> list[UploadCase]:
    run_id = args.run_id or time.strftime("%Y%m%d_%H%M%S")
    cases = []
    for offset in range(args.trajectories):
        index = args.start_index + offset
        request_id = f"stress_upload_{run_id}_{index:06d}"
        scenario = make_scenario(
            index,
            split="stress",
            seed=args.seed,
            behavior_mode=args.behavior_mode,
            branch_mode=args.branch_mode,
        )
        scenario = prepare_real_bank_scenario(scenario, request_id=request_id, force_unique_identity=True)
        profile = scenario.get("profile", {})
        real_bank = scenario.get("real_bank", {})
        cases.append(
            UploadCase(
                index=index,
                trajectory_id=str(real_bank.get("trajectory_id") or request_id),
                scenario_id=str(scenario.get("scenario_id") or f"stress_{index:06d}"),
                contact=str(profile.get("contact") or profile.get("mobile") or profile.get("email") or ""),
                contact_type=str(profile.get("contact_type") or "MOBILE").upper(),
                area_code=str(profile.get("area_code") or "1").lstrip("+"),
                verification_code=str(profile.get("verification_code") or os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_VERIFICATION_CODE", "123456")),
            )
        )
    return cases


def _resized_upload_path(path: Path, *, max_edge: int, quality: int) -> Path:
    if max_edge <= 0:
        return path
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for --resize-max-edge.") from exc

    with Image.open(path) as image:
        image = image.copy()
        image.thumbnail((max_edge, max_edge))
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        out = Path(tempfile.gettempdir()) / f"digital_onboarding_bank_upload_{path.stem}_{max_edge}.jpg"
        image.save(out, format="JPEG", quality=quality, optimize=True)
    return out


def _auth_case(case: UploadCase, *, clear_session: bool, skip_send_code: bool) -> dict[str, Any]:
    api = make_trade_api(case.trajectory_id)
    if clear_session and hasattr(api, "clear_session"):
        api.clear_session()
    started = time.monotonic()
    result: dict[str, Any] = {
        "phase": "auth",
        "index": case.index,
        "trajectory_id": case.trajectory_id,
        "scenario_id": case.scenario_id,
        "ok": False,
    }
    try:
        send_response = {}
        if not skip_send_code:
            send_response = api.send_verification_code(case.contact, case.contact_type, case.area_code)
        login_response = api.login(
            case.contact,
            case.verification_code,
            contact_type=case.contact_type,
            area_code=case.area_code,
        )
        token_response = api.get_trading_token()
        ok = bool(login_response.get("data")) and bank_response_ok(token_response)
        result.update(
            {
                "ok": ok,
                "elapsed_s": time.monotonic() - started,
                "send_response": sanitize_bank_response(send_response),
                "login_response": sanitize_bank_response(login_response),
                "token_response": sanitize_bank_response(token_response),
            }
        )
    except Exception as exc:
        result.update(
            {
                "elapsed_s": time.monotonic() - started,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    return result


def _raw_trade_api(api: Any) -> Any:
    return getattr(api, "_api", api)


def _upload_file_type_supported(raw_api: Any) -> bool:
    try:
        return "file_type" in inspect.signature(raw_api.upload_file).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def _upload_request_without_image(
    api: Any,
    *,
    mode: str,
    filename: str,
    file_type: str,
    is_need_min: bool,
) -> dict[str, Any]:
    raw_api = _raw_trade_api(api)
    if not getattr(raw_api, "userId", None) or not getattr(raw_api, "access_token", None):
        raise ValueError("Please complete login and get trading token first")

    collect_supported = bool(file_type) and _upload_file_type_supported(raw_api)
    endpoint = (
        f"{raw_api.base_url}/api/oas/v1/application/file/collect"
        if collect_supported
        else f"{raw_api.base_url}/api/oas/v1/application/file/upload"
    )
    headers = raw_api._headers(
        {
            "Cookie": raw_api._cookie(),
            "userId": raw_api.userId,
            "Token": raw_api.token,
        }
    )
    headers.pop("Content-Type", None)

    files: dict[str, Any] = {"is_need_min": (None, 1 if is_need_min else 0)}
    if collect_supported:
        files["file_type"] = (None, file_type)
    if mode == "empty-file":
        file_field = "front" if collect_supported else "file"
        files[file_field] = (filename or "empty.png", b"", "application/octet-stream")
    elif mode != "metadata-only":
        raise ValueError(f"Unsupported no-image upload mode: {mode}")

    response = raw_api._request("POST", endpoint, headers=headers, files=files)
    return response.json()


def _upload_file_with_response_details(
    api: Any,
    *,
    upload_path: Path,
    file_type: str,
    is_need_min: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_api = _raw_trade_api(api)
    if not getattr(raw_api, "userId", None) or not getattr(raw_api, "access_token", None):
        raise ValueError("Please complete login and get trading token first")

    collect_supported = bool(file_type) and _upload_file_type_supported(raw_api)
    endpoint = (
        f"{raw_api.base_url}/api/oas/v1/application/file/collect"
        if collect_supported
        else f"{raw_api.base_url}/api/oas/v1/application/file/upload"
    )
    headers = raw_api._headers(
        {
            "Cookie": raw_api._cookie(),
            "userId": raw_api.userId,
            "Token": raw_api.token,
        }
    )
    headers.pop("Content-Type", None)

    file_field = "front" if collect_supported else "file"
    with upload_path.open("rb") as handle:
        files: dict[str, Any] = {
            file_field: (upload_path.name, handle.read(), "application/octet-stream"),
            "is_need_min": (None, 1 if is_need_min else 0),
        }
        if collect_supported:
            files["file_type"] = (None, file_type)
        response = raw_api._request("POST", endpoint, headers=headers, files=files)

    details = {
        "status_code": getattr(response, "status_code", None),
        "content_type": response.headers.get("content-type", "") if hasattr(response, "headers") else "",
        "response_bytes": len(getattr(response, "content", b"") or b""),
        "response_text_prefix": getattr(response, "text", "")[:500],
    }
    try:
        return response.json(), details
    except Exception as exc:
        setattr(exc, "response_details", details)
        raise


def _upload_once(
    *,
    case: UploadCase,
    upload_index: int,
    upload_path: Path,
    file_type: str,
    upload_mode: str,
    is_need_min: bool,
    query_progress: bool,
    raw_upload_response: bool,
    start_event: threading.Event,
) -> dict[str, Any]:
    api = make_trade_api(case.trajectory_id)
    start_event.wait()
    started = time.monotonic()
    result: dict[str, Any] = {
        "phase": "upload",
        "index": case.index,
        "upload_index": upload_index,
        "trajectory_id": case.trajectory_id,
        "scenario_id": case.scenario_id,
        "ok": False,
    }
    try:
        upload_response_details = {}
        if upload_mode == "query-progress":
            progress_started = time.monotonic()
            response = api.query_progress()
            upload_elapsed = time.monotonic() - progress_started
            file_obj = {}
            ok = bank_response_ok(response)
            progress_response = response
            progress_elapsed = upload_elapsed
        elif upload_mode in {"empty-file", "metadata-only"}:
            response = _upload_request_without_image(
                api,
                mode=upload_mode,
                filename=upload_path.name,
                file_type=file_type,
                is_need_min=is_need_min,
            )
            upload_elapsed = time.monotonic() - started
            file_obj = normalize_file_result(response)
            ok = bank_response_ok(response) and bool(file_obj.get("file_id") or response.get("d") is True)
            progress_response = {}
            progress_elapsed = 0.0
        elif raw_upload_response:
            response, upload_response_details = _upload_file_with_response_details(
                api,
                upload_path=upload_path,
                file_type=file_type,
                is_need_min=is_need_min,
            )
            upload_elapsed = time.monotonic() - started
            file_obj = normalize_file_result(response)
            ok = bank_response_ok(response) and bool(file_obj.get("file_id") or response.get("d") is True)
            progress_response = {}
            progress_elapsed = 0.0
        elif file_type:
            try:
                response = api.upload_file(os.fspath(upload_path), is_need_min=is_need_min, file_type=file_type)
            except TypeError:
                response = api.upload_file(os.fspath(upload_path), is_need_min=is_need_min)
            upload_elapsed = time.monotonic() - started
            file_obj = normalize_file_result(response)
            ok = bank_response_ok(response) and bool(file_obj.get("file_id") or response.get("d") is True)
            progress_response = {}
            progress_elapsed = 0.0
        else:
            response = api.upload_file(os.fspath(upload_path), is_need_min=is_need_min)
            upload_elapsed = time.monotonic() - started
            file_obj = normalize_file_result(response)
            ok = bank_response_ok(response) and bool(file_obj.get("file_id") or response.get("d") is True)
            progress_response = {}
            progress_elapsed = 0.0
        if query_progress and upload_mode != "query-progress":
            progress_started = time.monotonic()
            progress_response = api.query_progress()
            progress_elapsed = time.monotonic() - progress_started
        result.update(
            {
                "ok": ok,
                "elapsed_s": time.monotonic() - started,
                "upload_elapsed_s": upload_elapsed,
                "progress_elapsed_s": progress_elapsed,
                "upload_mode": upload_mode,
                "file_obj": file_obj,
                "response": sanitize_bank_response(response),
                "progress_response": sanitize_bank_response(progress_response),
                "upload_response_details": sanitize_bank_response(upload_response_details),
            }
        )
    except Exception as exc:
        result.update(
            {
                "elapsed_s": time.monotonic() - started,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "upload_response_details": sanitize_bank_response(getattr(exc, "response_details", {})),
            }
        )
    return result


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    uploads = [row for row in records if row.get("phase") == "upload"]
    auth = [row for row in records if row.get("phase") == "auth"]
    upload_latencies = [float(row["upload_elapsed_s"]) for row in uploads if row.get("upload_elapsed_s") is not None]
    error_counts: dict[str, int] = {}
    for row in records:
        if row.get("ok"):
            continue
        key = str(row.get("error_type") or row.get("response", {}).get("s") or row.get("phase") or "unknown")
        error_counts[key] = error_counts.get(key, 0) + 1
    return {
        "auth_count": len(auth),
        "auth_ok": sum(1 for row in auth if row.get("ok")),
        "upload_count": len(uploads),
        "upload_ok": sum(1 for row in uploads if row.get("ok")),
        "upload_error_count": sum(1 for row in uploads if not row.get("ok")),
        "upload_latency_s": {
            "min": min(upload_latencies) if upload_latencies else None,
            "mean": statistics.fmean(upload_latencies) if upload_latencies else None,
            "p50": _percentile(upload_latencies, 0.50),
            "p90": _percentile(upload_latencies, 0.90),
            "p95": _percentile(upload_latencies, 0.95),
            "p99": _percentile(upload_latencies, 0.99),
            "max": max(upload_latencies) if upload_latencies else None,
        },
        "error_counts": dict(sorted(error_counts.items(), key=lambda item: item[1], reverse=True)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-scripts-dir", default=os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR", ""))
    parser.add_argument("--session-root", default=os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT", ""))
    parser.add_argument("--environment", default=os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_ENVIRONMENT", "test"))
    parser.add_argument("--connect-timeout", type=float, default=float(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT", "10")))
    parser.add_argument("--read-timeout", type=float, default=float(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT", "60")))
    parser.add_argument("--upload-image-path", default=os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_UPLOAD_IMAGE_PATH", real_bank_upload_image_path()))
    parser.add_argument(
        "--upload-mode",
        choices=sorted(UPLOAD_MODES),
        default="normal",
        help=(
            "normal sends the configured image. empty-file sends the multipart file field with zero bytes. "
            "metadata-only hits the upload endpoint with no file field. query-progress skips upload and calls query_progress."
        ),
    )
    parser.add_argument("--resize-max-edge", type=int, default=0, help="Create a temporary smaller JPEG before upload when > 0.")
    parser.add_argument("--resize-quality", type=int, default=85)
    parser.add_argument("--trajectories", type=int, default=128)
    parser.add_argument("--uploads-per-trajectory", type=int, default=8)
    parser.add_argument("--upload-workers", type=int, default=128)
    parser.add_argument("--auth-workers", type=int, default=16)
    parser.add_argument("--skip-auth", action="store_true", help="Reuse existing per-trajectory sessions instead of logging in.")
    parser.add_argument("--skip-send-code", action="store_true", help="Call login/token without first calling sendMac.")
    parser.add_argument("--clear-session", action="store_true", help="Clear per-trajectory session files before auth.")
    parser.add_argument("--query-progress-after-upload", action="store_true", help="Also call query_progress after each upload, matching the tool path.")
    parser.add_argument(
        "--upload-release-mode",
        choices=sorted(UPLOAD_RELEASE_MODES),
        default="burst",
        help="burst submits all upload tasks, then releases them together. immediate lets tasks start as soon as workers pick them up.",
    )
    parser.add_argument(
        "--raw-upload-response",
        action="store_true",
        help="Perform the upload request in this script so non-JSON responses include status/body details.",
    )
    parser.add_argument("--file-type", default="", help="Optional bank file_type. Empty hits /file/upload; set e.g. drivers_licence_front for /file/collect when supported.")
    parser.add_argument("--no-min-file", action="store_true")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--behavior-mode", default="phase1")
    parser.add_argument("--branch-mode", default=os.environ.get("DIGITAL_ONBOARDING_BRANCH_MODE", "us_market"))
    parser.add_argument("--output-jsonl", default="/tmp/real_bank_upload_stress.jsonl")
    args = parser.parse_args()

    _set_env_if_value("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR", args.api_scripts_dir)
    _set_env_if_value("DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT", args.session_root)
    os.environ["DIGITAL_ONBOARDING_REAL_BANK_ENVIRONMENT"] = args.environment
    os.environ["DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT"] = str(args.connect_timeout)
    os.environ["DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT"] = str(args.read_timeout)

    upload_path = Path(args.upload_image_path).expanduser().resolve()
    if args.upload_mode == "normal":
        if not upload_path.is_file():
            raise FileNotFoundError(f"Upload image not found: {upload_path}")
        upload_path = _resized_upload_path(upload_path, max_edge=args.resize_max_edge, quality=args.resize_quality)

    cases = _make_upload_cases(args)
    records: list[dict[str, Any]] = []
    output_path = Path(args.output_jsonl).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        json.dumps(
            {
                "event": "stress_config",
                "upload_image_path": os.fspath(upload_path),
                "upload_image_bytes": upload_path.stat().st_size if upload_path.is_file() else 0,
                "upload_mode": args.upload_mode,
                "trajectories": args.trajectories,
                "uploads_per_trajectory": args.uploads_per_trajectory,
                "total_uploads": args.trajectories * args.uploads_per_trajectory,
                "upload_workers": args.upload_workers,
                "auth_workers": 0 if args.skip_auth else args.auth_workers,
                "is_need_min": not args.no_min_file,
                "upload_release_mode": args.upload_release_mode,
                "raw_upload_response": args.raw_upload_response,
                "api_scripts_dir": os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR", ""),
                "session_root": os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT", ""),
                "connect_timeout": args.connect_timeout,
                "read_timeout": args.read_timeout,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    with output_path.open("w", encoding="utf-8") as out:
        if not args.skip_auth:
            with ThreadPoolExecutor(max_workers=max(1, args.auth_workers)) as executor:
                futures = [
                    executor.submit(_auth_case, case, clear_session=args.clear_session, skip_send_code=args.skip_send_code)
                    for case in cases
                ]
                for future in as_completed(futures):
                    row = future.result()
                    records.append(row)
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    out.flush()
            ok_cases = {row["trajectory_id"] for row in records if row.get("phase") == "auth" and row.get("ok")}
            cases = [case for case in cases if case.trajectory_id in ok_cases]
            if not cases:
                summary = _summarize(records)
                print(json.dumps({"event": "stress_summary", **summary}, ensure_ascii=False, indent=2), flush=True)
                return 2

        start_event = threading.Event()
        if args.upload_release_mode == "immediate":
            start_event.set()
        with ThreadPoolExecutor(max_workers=max(1, args.upload_workers)) as executor:
            futures = []
            for case in cases:
                for upload_index in range(args.uploads_per_trajectory):
                    futures.append(
                        executor.submit(
                            _upload_once,
                            case=case,
                            upload_index=upload_index,
                            upload_path=upload_path,
                            file_type=args.file_type,
                            upload_mode=args.upload_mode,
                            is_need_min=not args.no_min_file,
                            query_progress=args.query_progress_after_upload,
                            raw_upload_response=args.raw_upload_response,
                            start_event=start_event,
                        )
                    )
            if args.upload_release_mode == "burst":
                print(json.dumps({"event": "upload_burst_releasing", "requests": len(futures)}, ensure_ascii=False), flush=True)
                start_event.set()
            else:
                print(json.dumps({"event": "upload_immediate_submitted", "requests": len(futures)}, ensure_ascii=False), flush=True)
            for future in as_completed(futures):
                row = future.result()
                records.append(row)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()

    summary = _summarize(records)
    print(json.dumps({"event": "stress_summary", **summary}, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["upload_error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
