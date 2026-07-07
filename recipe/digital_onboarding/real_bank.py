"""Adapter helpers for running digital-onboarding tools against open-account.

The default digital-onboarding environment is synthetic. This module provides
the opt-in bridge to ``open-account/scripts/api.py`` plus small translation
helpers so existing scenario profiles can be used as real-bank test identities.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib.util
import inspect
import json
import os
import re
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback for local dev.
    fcntl = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPEN_ACCOUNT_SCRIPTS = PROJECT_ROOT / "open-account" / "scripts"
NEW_OPEN_ACCOUNT_SCRIPTS = PROJECT_ROOT / "new-open-account" / "scripts"
DEFAULT_SESSION_DIRNAME = ".digital_onboarding_sessions"
DEFAULT_DOCUMENT_FILENAME = "test.png"

REAL_BANK_BACKENDS = {"real_bank", "bank", "open_account", "open-account"}
SENSITIVE_KEY_RE = re.compile(r"(token|session|password|secret|authorization|cookie)", re.IGNORECASE)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def backend_name(tools_kwargs: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> str:
    tools_kwargs = tools_kwargs or {}
    config = config or {}
    return str(
        tools_kwargs.get("__onboarding_tool_backend__")
        or tools_kwargs.get("tool_backend")
        or config.get("backend")
        or os.environ.get("DIGITAL_ONBOARDING_TOOL_BACKEND")
        or "simulator"
    ).strip()


def real_bank_enabled(tools_kwargs: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> bool:
    return backend_name(tools_kwargs, config).lower() in REAL_BANK_BACKENDS


def real_bank_environment() -> str:
    return os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_ENVIRONMENT", "test")


def real_bank_session_root() -> str:
    return os.environ.get(
        "DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT",
        str(real_bank_api_scripts_dir() / DEFAULT_SESSION_DIRNAME),
    )


def real_bank_verification_code() -> str:
    return os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_VERIFICATION_CODE", "123456")


def real_bank_fake_verification_wrapper_enabled() -> bool:
    """Whether local test code ownership should wrap the bank verification flow."""

    return env_bool("DIGITAL_ONBOARDING_REAL_BANK_FAKE_VERIFICATION_WRAPPER", True)


def real_bank_fake_upload_wrapper_enabled() -> bool:
    """Whether uploaded document bytes should be accepted locally in test mode."""

    return env_bool("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER", True)


def real_bank_upload_thumbnail_enabled() -> bool:
    """Whether real-bank uploads should ask the backend to generate thumbnails."""

    return env_bool("DIGITAL_ONBOARDING_REAL_BANK_UPLOAD_NEED_MIN", False)


def real_bank_send_rate_limit_bypass_enabled() -> bool:
    return env_bool("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT", True)


def real_bank_auth_bypass_enabled() -> bool:
    return env_bool("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS", True)


def real_bank_real_api_execution_enabled() -> bool:
    """Use the production-like API implementation only when test bypasses are off."""

    return (
        not real_bank_send_rate_limit_bypass_enabled()
        and not real_bank_fake_upload_wrapper_enabled()
    )


def real_bank_strict_production_execution_enabled() -> bool:
    """Whether all training/simulation bypasses are disabled."""

    return (
        real_bank_real_api_execution_enabled()
        and not real_bank_auth_bypass_enabled()
    )


def real_bank_api_scripts_dir() -> Path:
    override = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if real_bank_real_api_execution_enabled():
        return NEW_OPEN_ACCOUNT_SCRIPTS
    return OPEN_ACCOUNT_SCRIPTS


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe[:160] or "trajectory"


def trajectory_id_for(scenario: dict[str, Any], request_id: str | None = None) -> str:
    scenario_id = str(scenario.get("scenario_id") or "scenario")
    if request_id and env_bool("DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_PER_ROLLOUT", True):
        return _safe_id(f"{scenario_id}_{request_id}")
    return _safe_id(scenario_id)


def _numeric_suffix(value: str, width: int = 6) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()
    number = int(digest[:14], 16) % (10**width)
    return f"{number:0{width}d}"


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _identity_ledger_enabled() -> bool:
    return env_bool("DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_LEDGER_ENABLED", True)


def _identity_namespace() -> str:
    namespace = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_NAMESPACE")
    return _safe_id(namespace or "default")


def _identity_ledger_path(namespace: str) -> Path:
    override = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_LEDGER")
    if override:
        return Path(override).expanduser().resolve()
    root = Path(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_LEDGER_ROOT", real_bank_session_root()))
    return root.expanduser().resolve() / f"identity_ledger_{namespace}.json"


def real_bank_clean_identity_preflight_enabled() -> bool:
    return env_bool("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_CLEAN_IDENTITIES", False)


def _env_csv_set(name: str, default: str) -> set[str]:
    value = os.environ.get(name, default)
    return {item.strip().upper() for item in str(value).split(",") if item.strip()}


@contextlib.contextmanager
def _locked_json_ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
            yield data
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, path)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _lease_identity_number(kind: str, trajectory_id: str, *, capacity: int, start: int = 0) -> int:
    if capacity <= 0:
        raise ValueError(f"Invalid {kind} identity capacity: {capacity}")
    start = start % capacity
    if not _identity_ledger_enabled():
        return (start + int(_numeric_suffix(f"{kind}:{trajectory_id}", 12))) % capacity

    namespace = _identity_namespace()
    ledger_path = _identity_ledger_path(namespace)
    bucket_key = f"{kind}:capacity={capacity}:start={start}"
    trajectory_key = _safe_id(trajectory_id)
    with _locked_json_ledger(ledger_path) as ledger:
        ledger.setdefault("namespace", namespace)
        ledger.setdefault("version", 1)
        leases = ledger.setdefault("leases", {})
        next_values = ledger.setdefault("next", {})
        bucket = leases.setdefault(bucket_key, {})
        if trajectory_key in bucket:
            return int(bucket[trajectory_key])
        used = {int(value) for value in bucket.values()}
        next_value = int(next_values.get(bucket_key, start)) % capacity
        for offset in range(capacity):
            candidate = (next_value + offset) % capacity
            if candidate in used:
                continue
            bucket[trajectory_key] = candidate
            next_values[bucket_key] = (candidate + 1) % capacity
            return candidate

    raise RuntimeError(
        f"Exhausted {kind} identity namespace capacity={capacity}. "
        "Increase DIGITAL_ONBOARDING_REAL_BANK_MOBILE_DIGITS, change DIGITAL_ONBOARDING_REAL_BANK_MOBILE_PREFIX, "
        "set DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_OFFSET to a fresh range, or provide "
        "DIGITAL_ONBOARDING_REAL_BANK_ACCOUNTS_JSON."
    )


def _candidate_record(
    *,
    trajectory_id: str,
    candidate: int,
    mobile: str,
    clean: bool,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "trajectory_id": _safe_id(trajectory_id),
        "candidate": int(candidate),
        "mobile": mobile,
        "clean": bool(clean),
        "reason": reason,
        "detail": sanitize_bank_response(detail or {}),
        "time": time.time(),
    }


def _stale_pending_cutoff() -> float:
    ttl = _env_int("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_PENDING_TTL_SECONDS", 900)
    return time.time() - max(1, ttl)


def _drop_stale_pending(pending_bucket: dict[str, Any]) -> None:
    cutoff = _stale_pending_cutoff()
    stale = []
    for key, value in pending_bucket.items():
        if not isinstance(value, dict):
            stale.append(key)
            continue
        try:
            timestamp = float(value.get("time", 0))
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp < cutoff:
            stale.append(key)
    for key in stale:
        pending_bucket.pop(key, None)


def _pick_preflight_candidate(
    ledger: dict[str, Any],
    *,
    bucket_key: str,
    trajectory_key: str,
    capacity: int,
    start: int,
) -> int | None:
    leases = ledger.setdefault("leases", {})
    next_values = ledger.setdefault("next", {})
    dirty = ledger.setdefault("dirty", {})
    pending = ledger.setdefault("pending", {})
    bucket = leases.setdefault(bucket_key, {})
    dirty_bucket = dirty.setdefault(bucket_key, {})
    pending_bucket = pending.setdefault(bucket_key, {})
    _drop_stale_pending(pending_bucket)

    used = {int(value) for value in bucket.values()}
    next_value = int(next_values.get(bucket_key, start)) % capacity
    for offset in range(capacity):
        candidate = (next_value + offset) % capacity
        candidate_key = str(candidate)
        if candidate in used or candidate_key in dirty_bucket or candidate_key in pending_bucket:
            continue
        pending_bucket[candidate_key] = {
            "trajectory_id": trajectory_key,
            "time": time.time(),
        }
        next_values[bucket_key] = (candidate + 1) % capacity
        return candidate
    return None


def _release_preflight_pending(
    ledger: dict[str, Any],
    *,
    bucket_key: str,
    trajectory_key: str,
    candidate: int,
) -> None:
    pending_bucket = ledger.setdefault("pending", {}).setdefault(bucket_key, {})
    candidate_key = str(candidate)
    pending_record = pending_bucket.get(candidate_key)
    if not isinstance(pending_record, dict) or pending_record.get("trajectory_id") == trajectory_key:
        pending_bucket.pop(candidate_key, None)


def _lease_preflighted_mobile_number(
    trajectory_id: str,
    *,
    prefix: str,
    suffix_len: int,
    capacity: int,
    start: int,
    area_code: str = "1",
) -> int:
    """Lease a mobile suffix only after the real bank reports a clean account.

    The normal ledger guarantees local uniqueness, but public test-bank phone
    ranges can contain already-submitted accounts. This opt-in path probes the
    real bank and records dirty candidates so later rollouts skip them.
    """

    if capacity <= 0:
        raise ValueError(f"Invalid mobile identity capacity: {capacity}")
    start = start % capacity
    if not _identity_ledger_enabled():
        raise RuntimeError(
            "DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_CLEAN_IDENTITIES requires "
            "DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_LEDGER_ENABLED=1."
        )

    namespace = _identity_namespace()
    ledger_path = _identity_ledger_path(namespace)
    bucket_key = f"mobile:capacity={capacity}:start={start}"
    trajectory_key = _safe_id(trajectory_id)
    max_attempts = max(1, _env_int("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_MAX_ATTEMPTS", 128))
    recheck_existing = env_bool("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_RECHECK_EXISTING", True)

    for _ in range(max_attempts):
        with _locked_json_ledger(ledger_path) as ledger:
            ledger.setdefault("namespace", namespace)
            ledger.setdefault("version", 1)
            bucket = ledger.setdefault("leases", {}).setdefault(bucket_key, {})
            clean_bucket = ledger.setdefault("preflight_clean", {}).setdefault(bucket_key, {})
            dirty_bucket = ledger.setdefault("dirty", {}).setdefault(bucket_key, {})

            if trajectory_key in bucket:
                candidate = int(bucket[trajectory_key])
                candidate_key = str(candidate)
                if candidate_key in clean_bucket or not recheck_existing:
                    return candidate
                if candidate_key in dirty_bucket:
                    bucket.pop(trajectory_key, None)
                    continue
            else:
                candidate = _pick_preflight_candidate(
                    ledger,
                    bucket_key=bucket_key,
                    trajectory_key=trajectory_key,
                    capacity=capacity,
                    start=start,
                )
                if candidate is None:
                    break

        mobile = f"{prefix}{candidate:0{suffix_len}d}"
        is_clean, reason, detail = _preflight_real_bank_mobile(mobile, area_code=area_code)

        with _locked_json_ledger(ledger_path) as ledger:
            ledger.setdefault("namespace", namespace)
            ledger.setdefault("version", 1)
            bucket = ledger.setdefault("leases", {}).setdefault(bucket_key, {})
            clean_bucket = ledger.setdefault("preflight_clean", {}).setdefault(bucket_key, {})
            dirty_bucket = ledger.setdefault("dirty", {}).setdefault(bucket_key, {})
            _release_preflight_pending(
                ledger,
                bucket_key=bucket_key,
                trajectory_key=trajectory_key,
                candidate=candidate,
            )

            candidate_key = str(candidate)
            if is_clean:
                other_owner = next((key for key, value in bucket.items() if int(value) == candidate), None)
                if other_owner and other_owner != trajectory_key:
                    continue
                bucket[trajectory_key] = candidate
                clean_bucket[candidate_key] = _candidate_record(
                    trajectory_id=trajectory_id,
                    candidate=candidate,
                    mobile=mobile,
                    clean=True,
                    reason=reason,
                    detail=detail,
                )
                dirty_bucket.pop(candidate_key, None)
                return candidate

            if bucket.get(trajectory_key) == candidate:
                bucket.pop(trajectory_key, None)
            dirty_bucket[candidate_key] = _candidate_record(
                trajectory_id=trajectory_id,
                candidate=candidate,
                mobile=mobile,
                clean=False,
                reason=reason,
                detail=detail,
            )
            clean_bucket.pop(candidate_key, None)

    raise RuntimeError(
        "Could not lease a clean real-bank mobile identity after "
        f"{max_attempts} preflight attempts for namespace={namespace!r}, start={start}. "
        "Use a fresh DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_NAMESPACE/OFFSET, "
        "increase DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_MAX_ATTEMPTS, or disable "
        "DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_CLEAN_IDENTITIES."
    )


def _response_find(response: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(response, dict):
        for key in keys:
            value = response.get(key)
            if value not in (None, ""):
                return value
        for nested_key in ("bank_response", "response", "result", "data", "d", "payload", "body"):
            nested = response.get(nested_key)
            if nested is None:
                continue
            value = _response_find(nested, keys)
            if value not in (None, ""):
                return value
    elif isinstance(response, list):
        for item in response:
            value = _response_find(item, keys)
            if value not in (None, ""):
                return value
    return None


def _bank_error_code(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    for nested_key in ("bank_response", "response", "result", "data", "d", "payload", "body"):
        nested = response.get(nested_key)
        if nested is None:
            continue
        nested_code = _bank_error_code(nested)
        if nested_code:
            return nested_code
    code = _response_find(response, ("errorCode", "error_code", "s", "status"))
    return str(code or "").upper()


def _mobile_for(trajectory_id: str) -> str:
    # The bank test environment accepts numbers in the 202604xxxx range; more
    # arbitrary 2026xxxxxx values may be rejected before login as invalid phone
    # numbers. Keep the prefix configurable for local bank fixtures.
    prefix = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_MOBILE_PREFIX", "202604")
    prefix = re.sub(r"\D", "", prefix) or "2026"
    total_digits = int(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_MOBILE_DIGITS", "10"))
    suffix_len = max(1, total_digits - len(prefix))
    if len(prefix) >= total_digits:
        prefix = prefix[: total_digits - 1]
        suffix_len = 1
    capacity = 10**suffix_len
    start = _env_int(
        "DIGITAL_ONBOARDING_REAL_BANK_MOBILE_OFFSET",
        _env_int("DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_OFFSET", 0),
    )
    area_code = str(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_AREA_CODE", "1")).lstrip("+") or "1"
    if real_bank_clean_identity_preflight_enabled():
        suffix = _lease_preflighted_mobile_number(
            trajectory_id,
            prefix=prefix,
            suffix_len=suffix_len,
            capacity=capacity,
            start=start,
            area_code=area_code,
        )
    else:
        suffix = _lease_identity_number("mobile", trajectory_id, capacity=capacity, start=start)
    return f"{prefix}{suffix:0{suffix_len}d}"


def _email_for(trajectory_id: str) -> str:
    domain = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_EMAIL_DOMAIN", "gmail.com")
    prefix = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_EMAIL_PREFIX", "do")
    digest = hashlib.sha1(trajectory_id.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}{digest}@{domain}"


def _ssn_for(trajectory_id: str) -> str:
    if env_bool("DIGITAL_ONBOARDING_REAL_BANK_LEGACY_SSN_NAMESPACE", False):
        suffix = _numeric_suffix(trajectory_id, 6)
        return f"999-{int(suffix[:2]):02d}-{int(suffix[2:]):04d}"

    area_min = int(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_SSN_AREA_MIN", "500"))
    area_max = int(os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_SSN_AREA_MAX", "899"))
    if area_min > area_max:
        area_min, area_max = area_max, area_min
    areas = [area for area in range(max(1, area_min), min(899, area_max) + 1) if area != 666]
    if not areas:
        areas = [700]
    capacity = len(areas) * 99 * 9999
    start = _env_int(
        "DIGITAL_ONBOARDING_REAL_BANK_SSN_OFFSET",
        _env_int("DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_OFFSET", 0),
    )
    number = _lease_identity_number("ssn", trajectory_id, capacity=capacity, start=start)
    area = areas[number % len(areas)]
    group = ((number // len(areas)) % 99) + 1
    serial = ((number // (len(areas) * 99)) % 9999) + 1
    return f"{area:03d}-{group:02d}-{serial:04d}"


def _bank_send_rate_limit(response: Any) -> bool:
    code = _bank_error_code(response)
    return "MAC_SEND_OVER_MAX" in code or "MAC_SEND_LIMIT" in code or "MAC_SEND_LOCK" in code


def _real_bank_finishable_goal() -> str:
    return "authenticate_collect_required_kyc_and_submit_after_confirmation"


def _align_real_bank_behavior(scenario: dict[str, Any], behavior: str, auth_method: str) -> str:
    """Remove synthetic behaviors that contradict the selected real-bank flow.

    The public test bank currently behaves as a mobile-first account-opening
    environment. Keeping "email-only" or "passport-only" samples marked as
    finishable makes the policy optimize against impossible bank states.
    """
    if not env_bool("DIGITAL_ONBOARDING_REAL_BANK_ALIGN_SCENARIOS", True):
        return behavior

    normalized_auth = auth_method.upper()
    aligned = behavior
    if normalized_auth == "MOBILE" and behavior == "forgot_mobile_use_email":
        aligned = "forgot_email_use_mobile"
    elif normalized_auth == "EMAIL" and behavior == "forgot_email_use_mobile":
        aligned = "forgot_mobile_use_email"
    elif (
        behavior == "passport_only"
        and str(scenario.get("branch") or scenario.get("profile", {}).get("branch") or "").upper() != "FOREIGNER"
        and env_bool("DIGITAL_ONBOARDING_REAL_BANK_ALIGN_PASSPORT_ONLY", False)
    ):
        aligned = "cooperative"

    if aligned != behavior:
        scenario["user_behavior"] = aligned
        scenario["goal"] = _real_bank_finishable_goal()
        if behavior == "passport_only":
            scenario["required_fields"] = [
                "drivers_license" if field == "passport_photo" else field
                for field in scenario.get("required_fields", [])
            ]
    return aligned


@lru_cache(maxsize=1)
def _account_overrides() -> dict[str, dict[str, Any]]:
    """Load optional contact overrides keyed by trajectory_id or scenario_id."""
    path = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_ACCOUNTS_JSON")
    if not path:
        return {}
    with open(Path(path).expanduser(), "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "accounts" in data:
        data = data["accounts"]
    if not isinstance(data, list):
        raise ValueError("DIGITAL_ONBOARDING_REAL_BANK_ACCOUNTS_JSON must contain a list or {'accounts': [...]}.")
    rows: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        keys = [item.get("trajectory_id"), item.get("scenario_id"), item.get("id")]
        for key in keys:
            if key:
                rows[str(key)] = item
    return rows


def prepare_real_bank_scenario(
    scenario: dict[str, Any],
    *,
    request_id: str | None = None,
    force_unique_identity: bool | None = None,
) -> dict[str, Any]:
    """Return a scenario whose hidden profile has real-bank test identity data."""
    scenario = copy.deepcopy(scenario or {})
    profile = copy.deepcopy(scenario.get("profile", {}))
    scenario_id = str(scenario.get("scenario_id") or "scenario")
    behavior = str(scenario.get("user_behavior") or "")
    trajectory_id = trajectory_id_for(scenario, request_id)

    if force_unique_identity is None:
        force_unique_identity = env_bool("DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES", True)

    override = _account_overrides().get(trajectory_id) or _account_overrides().get(scenario_id) or {}
    if force_unique_identity:
        mobile = str(override.get("mobile") or override.get("contact") or _mobile_for(trajectory_id))
        email = str(override.get("email") or _email_for(trajectory_id))
        auth_method = str(
            override.get("auth_method")
            or os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_AUTH_METHOD")
            or "MOBILE"
        ).upper()
        behavior = _align_real_bank_behavior(scenario, behavior, auth_method)
        if auth_method in {"BOTH", "SCENARIO"}:
            contact_type = str(override.get("contact_type") or profile.get("contact_type") or "MOBILE").upper()
            available_auth_methods = ["MOBILE", "EMAIL"] if auth_method == "BOTH" else list(profile.get("available_auth_methods", ["MOBILE"]))
        else:
            contact_type = "EMAIL" if auth_method == "EMAIL" else "MOBILE"
            available_auth_methods = [contact_type]
        if "@" in mobile and not override.get("mobile"):
            email = mobile
            mobile = _mobile_for(trajectory_id)
            contact_type = "EMAIL"
        contact = email if contact_type == "EMAIL" else mobile

        profile.update(
            {
                "contact_type": contact_type,
                "contact": contact,
                "mobile": mobile,
                "email": email,
                "email_address": email,
                "area_code": str(override.get("area_code") or profile.get("area_code") or "1").lstrip("+"),
                "verification_code": str(override.get("verification_code") or real_bank_verification_code()),
                "social_security_number": str(override.get("social_security_number") or _ssn_for(trajectory_id)),
            }
        )
        profile["auth_contacts"] = {
            "MOBILE": {"contact": profile["mobile"], "area_code": profile["area_code"]},
            "EMAIL": {"contact": profile["email"]},
        }
        profile["available_auth_methods"] = available_auth_methods
        profile["required_auth_methods"] = [contact_type] if auth_method in {"MOBILE", "EMAIL"} else list(profile.get("required_auth_methods", []))

        # Keep behavior-level impossibility intact. The generated bank identity
        # can exist privately, but the simulated customer should not reveal a
        # contact method that the scenario says is unavailable.
        if behavior == "no_auth_contact":
            profile["available_auth_methods"] = []
            profile["required_auth_methods"] = []
        elif behavior == "mobile_required_user_will_return":
            profile["contact_type"] = "MOBILE"
            profile["contact"] = profile["mobile"]
            profile["available_auth_methods"] = ["MOBILE"]
            profile["required_auth_methods"] = ["MOBILE"]

    if "given_name" not in profile and "gvie_name" in profile:
        profile["given_name"] = profile["gvie_name"]
    if "gvie_name" not in profile and "given_name" in profile:
        profile["gvie_name"] = profile["given_name"]
    if profile.get("citizenship_country") == "US":
        profile["citizenship_country"] = "USA"
    if profile.get("birth_country") == "US":
        profile["birth_country"] = "USA"
    if isinstance(profile.get("home_address"), dict) and profile["home_address"].get("country") == "US":
        profile["home_address"]["country"] = "USA"

    required = []
    for field in scenario.get("required_fields", []):
        required.append("given_name" if field == "gvie_name" else field)
    scenario["required_fields"] = required

    initial_collected = {}
    for key, value in (scenario.get("initial_collected") or {}).items():
        initial_collected["given_name" if key == "gvie_name" else key] = value
    scenario["initial_collected"] = initial_collected

    scenario["profile"] = profile
    scenario["real_bank"] = {
        "enabled": True,
        "trajectory_id": trajectory_id,
        "scenario_id": scenario_id,
        "identity_is_unique": bool(force_unique_identity),
    }
    return scenario


@lru_cache(maxsize=4)
def get_trade_api_class(scripts_dir: str | None = None):
    scripts_path = Path(scripts_dir).resolve() if scripts_dir else real_bank_api_scripts_dir()
    api_path = scripts_path / "api.py"
    module_name = f"_digital_onboarding_trade_api_{hashlib.sha1(str(api_path).encode('utf-8')).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, api_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load TradeAPI from {api_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.TradeAPI


def make_trade_api(trajectory_id: str):
    scripts_dir = real_bank_api_scripts_dir()
    try:
        TradeAPI = get_trade_api_class(str(scripts_dir))
    except TypeError:
        TradeAPI = get_trade_api_class()
    kwargs = {"environment": real_bank_environment()}
    signature = inspect.signature(TradeAPI)
    if "session_id" in signature.parameters:
        kwargs["session_id"] = trajectory_id
    if "session_dir" in signature.parameters:
        kwargs["session_dir"] = real_bank_session_root()
    elif "session_path" in signature.parameters:
        kwargs["session_path"] = os.path.join(real_bank_session_root(), f"{_safe_id(trajectory_id)}.json")
    api = TradeAPI(**kwargs)
    if "session_id" not in signature.parameters and "session_path" not in signature.parameters:
        api = _SessionScopedTradeAPI(api, trajectory_id, real_bank_session_root())
    return api


class _SessionScopedTradeAPI:
    """Add per-trajectory sessions around the simple new-open-account TradeAPI."""

    def __init__(self, api: Any, trajectory_id: str, session_root: str):
        object.__setattr__(self, "_api", api)
        object.__setattr__(self, "_session_file", os.path.join(session_root, f"{_safe_id(trajectory_id)}.json"))
        self._api._session_path = self._session_path
        self._api._load_session = self._load_session
        self._api._save_session = self._save_session
        self._load_session()

    def _session_path(self) -> str:
        return self._session_file

    def _load_session(self) -> None:
        session_file = self._session_path()
        if os.path.exists(session_file):
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError):
                data = {}
            self._api.userId = data.get("userId")
            self._api.token = data.get("token")
            self._api.access_token = data.get("access_token")
        else:
            self._api.userId = None
            self._api.token = None
            self._api.access_token = None

    def _save_session(self) -> None:
        session_file = self._session_path()
        os.makedirs(os.path.dirname(session_file), exist_ok=True)
        data = {
            "userId": self._api.userId,
            "token": self._api.token,
            "access_token": self._api.access_token,
        }
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def clear_session(self) -> None:
        self._api.clear_session()
        session_file = self._session_path()
        if os.path.exists(session_file):
            os.remove(session_file)

    def login(self, *args: Any, **kwargs: Any) -> Any:
        result = _json_response(self._api.login(*args, **kwargs))
        self._save_session()
        return result

    def get_trading_token(self, *args: Any, **kwargs: Any) -> Any:
        result = _json_response(self._api.get_trading_token(*args, **kwargs))
        self._save_session()
        return result

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._api, name)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            return _json_response(value(*args, **kwargs))

        return call

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_api", "_session_file"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._api, name, value)


def _json_response(value: Any) -> Any:
    if hasattr(value, "json") and callable(value.json):
        try:
            return value.json()
        except ValueError:
            return value
    return value


def sanitize_bank_response(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = sanitize_bank_response(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_bank_response(item) for item in value]
    return value


def bank_response_ok(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    return response.get("s") == "ok" or response.get("i18nMsg") == "success" or response.get("data") is True


def bank_progress_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("d")
    return data if isinstance(data, dict) else {}


def bank_progress_summary(response: Any) -> dict[str, Any]:
    data = bank_progress_data(response)
    missing = data.get("missing_fields")
    collected = data.get("collected_fields")
    return {
        "status": data.get("status"),
        "app_no": data.get("app_no"),
        "missing_fields": missing if isinstance(missing, list) else [],
        "collected_fields": collected if isinstance(collected, list) else [],
        "completion_percentage": data.get("completion_percentage"),
    }


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _preflight_progress_clean(summary: dict[str, Any]) -> tuple[bool, str]:
    status = str(summary.get("status") or "").strip().upper()
    app_no = summary.get("app_no")
    completion = _float_or_none(summary.get("completion_percentage"))
    missing_fields = summary.get("missing_fields") if isinstance(summary.get("missing_fields"), list) else []
    collected_fields = summary.get("collected_fields") if isinstance(summary.get("collected_fields"), list) else []

    dirty_statuses = _env_csv_set(
        "DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_DIRTY_STATUSES",
        "COLLECTING,AUDITING,APPROVED,REJECTED,SUBMITTED",
    )
    clean_statuses = _env_csv_set(
        "DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_CLEAN_STATUSES",
        "NOT_APPLIED,NOT_STARTED,NEW",
    )
    max_completion = float(_env_int("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_MAX_COMPLETION", 0))

    if status in dirty_statuses:
        return False, f"dirty_status:{status}"
    if app_no and env_bool("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_DIRTY_IF_APP_NO", True):
        return False, "dirty_app_no"
    if completion is not None and completion > max_completion:
        return False, f"dirty_completion:{completion:g}"
    if collected_fields and env_bool("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_DIRTY_IF_COLLECTED_FIELDS", True):
        return False, "dirty_collected_fields"
    if missing_fields and env_bool("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_DIRTY_IF_MISSING_FIELDS", False):
        return False, "dirty_missing_fields"
    if status and status not in clean_statuses and env_bool("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_REQUIRE_CLEAN_STATUS", True):
        return False, f"unknown_status:{status}"
    return True, "clean"


def _preflight_real_bank_mobile(mobile: str, *, area_code: str = "1") -> tuple[bool, str, dict[str, Any]]:
    probe_id = _safe_id(f"identity_preflight_{_identity_namespace()}_{area_code}_{mobile}")
    detail: dict[str, Any] = {
        "mobile": mobile,
        "area_code": area_code,
        "probe_id": probe_id,
    }
    try:
        api = make_trade_api(probe_id)
        clear_session = getattr(api, "clear_session", None)
        if callable(clear_session):
            clear_session()

        send_response = api.send_verification_code(mobile, "MOBILE", area_code)
        detail["send_response"] = sanitize_bank_response(send_response)
        send_ok = bank_response_ok(send_response)
        if not send_ok and _bank_send_rate_limit(send_response) and real_bank_send_rate_limit_bypass_enabled():
            send_ok = True
            detail["send_rate_limit_bypassed"] = True
        if not send_ok:
            return False, "send_code_failed", detail

        login_response = api.login(
            mobile,
            real_bank_verification_code(),
            contact_type="MOBILE",
            area_code=area_code,
        )
        detail["login_response"] = sanitize_bank_response(login_response)
        if not bank_response_ok(login_response):
            return False, "login_failed", detail

        token_response = api.get_trading_token()
        detail["token_response"] = sanitize_bank_response(token_response)
        if not bank_response_ok(token_response):
            return False, "token_failed", detail

        progress_response = api.query_progress()
        detail["progress_response"] = sanitize_bank_response(progress_response)
        if not bank_response_ok(progress_response):
            return False, "query_progress_failed", detail

        summary = bank_progress_summary(progress_response)
        detail["progress_summary"] = sanitize_bank_response(summary)
        is_clean, reason = _preflight_progress_clean(summary)
        return is_clean, reason, detail
    except Exception as exc:
        detail["exception"] = str(exc)[:512]
        return False, "probe_exception", detail
    finally:
        if env_bool("DIGITAL_ONBOARDING_REAL_BANK_PREFLIGHT_CLEAR_SESSION", True):
            try:
                clear_session = getattr(locals().get("api", None), "clear_session", None)
                if callable(clear_session):
                    clear_session()
            except Exception:
                pass


def _file_obj(file_id: str | None, min_file_id: str | None, *, expire_date: str | None = None) -> dict[str, Any]:
    obj = {"file_id": file_id, "min_file_id": min_file_id}
    if expire_date:
        obj["expire_date"] = expire_date
    return {k: v for k, v in obj.items() if v}


def normalize_file_result(response: Any) -> dict[str, Any]:
    data = response.get("d") if isinstance(response, dict) else {}
    data = data if isinstance(data, dict) else {}
    file_id = data.get("fileId") or data.get("file_id")
    min_file_id = data.get("minFileId") or data.get("min_file_id")
    return _file_obj(file_id, min_file_id)


def normalize_document_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    expire_date = str(value.get("expire_date") or os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_DOCUMENT_EXPIRE_DATE", "2030-12-31"))
    if "front" in value or "back" in value:
        normalized = copy.deepcopy(value)
        if isinstance(normalized.get("front"), dict) and "expire_date" not in normalized["front"]:
            normalized["front"]["expire_date"] = expire_date
        return normalized
    file_id = value.get("file_id") or value.get("fileId")
    min_file_id = value.get("min_file_id") or value.get("minFileId")
    front = _file_obj(file_id, min_file_id, expire_date=expire_date)
    back = _file_obj(file_id, min_file_id)
    return {"front": front, "back": back}


def normalize_single_file_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    if "front" in value and isinstance(value.get("front"), dict):
        value = value["front"]
    file_id = value.get("file_id") or value.get("fileId")
    min_file_id = value.get("min_file_id") or value.get("minFileId")
    return _file_obj(file_id, min_file_id)


def _normalize_country(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    compact = re.sub(r"[^A-Za-z]", "", value).upper()
    if compact in {"US", "USA", "UNITEDSTATES", "UNITEDSTATESOFAMERICA"}:
        return "USA"
    return value


def _normalize_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"true", "yes", "y", "1"}:
            return True
        if lower in {"false", "no", "n", "0"}:
            return False
    return value


def _normalize_funding_source(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    mapping = {
        "SAVINGS": "Savings",
        "INHERITANCE": "Inheritance",
        "PENSION": "Pension",
        "RENTAL_INCOME": "Rental Income",
        "RENTAL INCOME": "Rental Income",
        "SOCIAL_SECURITY": "Social Security",
        "SOCIAL SECURITY": "Social Security",
        "OTHER": "Other",
    }
    return mapping.get(value.strip().upper(), value)


def _normalize_file_type(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return {"permanent_resident_card": "id_card"}.get(value, value)


def _document_field_for_file_type(file_type: str) -> str:
    lowered = (file_type or "").lower()
    if "passport" in lowered:
        return "passport_photo"
    if "utility" in lowered or "statement" in lowered or "address" in lowered or "bill" in lowered:
        return "address_proof"
    if "visa" in lowered:
        return "visa"
    if "id_card" in lowered or "permanent_resident" in lowered or "card" in lowered:
        return "card_photo"
    if "licence" in lowered or "license" in lowered:
        return "drivers_license"
    return "government_issued_id"


def _merge_driver_document(existing: Any, file_obj: dict[str, Any], file_type: str) -> dict[str, Any]:
    document = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    side = "back" if "back" in (file_type or "").lower() else "front"
    side_obj = copy.deepcopy(file_obj)
    if side == "front":
        side_obj.setdefault("expire_date", os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_DOCUMENT_EXPIRE_DATE", "2030-12-31"))
    document[side] = side_obj
    return document


def _normalize_documents(documents: Any, payload: dict[str, Any]) -> list[Any]:
    if not isinstance(documents, list):
        return documents
    normalized_documents: list[Any] = []
    for item in documents:
        if not isinstance(item, dict):
            normalized_documents.append(item)
            continue
        document = copy.deepcopy(item)
        file_type = _normalize_file_type(document.get("file_type") or document.get("document_type"))
        if file_type:
            document["file_type"] = file_type

        doc_key = _document_field_for_file_type(str(file_type or ""))
        file_obj = {
            "file_id": document.get("file_id") or document.get("fileId"),
            "min_file_id": document.get("min_file_id") or document.get("minFileId"),
        }
        file_obj = {key: value for key, value in file_obj.items() if value}
        if file_obj:
            if doc_key == "drivers_license":
                payload[doc_key] = _merge_driver_document(payload.get(doc_key), file_obj, str(file_type or ""))
            else:
                payload.setdefault(doc_key, file_obj)

        if file_type == "passport":
            if document.get("passport_number"):
                payload.setdefault("passport_no", document["passport_number"])
            if document.get("expiration_date"):
                payload.setdefault("passport_expire_date", document["expiration_date"])
        elif file_type == "id_card" and document.get("id_number"):
            payload.setdefault("tax_id", document["id_number"])
            payload.setdefault("tax_id_country", document.get("issuing_country") or "CHN")
        elif file_type == "visa":
            if document.get("visa_type"):
                payload.setdefault("visa_type", document["visa_type"])
            if document.get("expiration_date"):
                payload.setdefault("visa_expiration_date", document["expiration_date"])
        normalized_documents.append(document)
    return normalized_documents


def _parse_home_address(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) < 3:
        return value
    state_zip = parts[-1]
    state = ""
    postal_code = ""
    match = re.match(r"([A-Za-z]{2,})(?:\s+(\d{4,10}(?:-\d{4})?))?$", state_zip)
    if match:
        state = match.group(1).upper()
        postal_code = match.group(2) or ""
    address = {
        "street_address1": parts[0],
        "city": parts[-2],
        "state": state or state_zip,
        "postal_code": postal_code,
        "country": "USA",
    }
    if len(parts) > 3:
        address["street_address2"] = ", ".join(parts[1:-2])
    return address


def normalize_collect_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in (data or {}).items():
        out_key = "given_name" if key == "gvie_name" else key
        alias_map = {
            "email": "email_address",
            "emailAddress": "email_address",
            "address": "home_address",
            "drivers_license_front": "drivers_licence_front",
            "drivers_license_back": "drivers_licence_back",
            "is_affiliated_exchange_or_finra": "is_affiliated_exchangeorfinra",
        }
        out_key = alias_map.get(out_key, out_key)
        if out_key in {"citizenship_country", "birth_country", "tax_id_country", "issuing_country"}:
            value = _normalize_country(value)
        if out_key == "funding_source":
            value = _normalize_funding_source(value)
        if out_key == "documents":
            value = _normalize_documents(value, payload)
        if out_key == "home_address":
            value = _parse_home_address(value)
            if isinstance(value, dict):
                value = copy.deepcopy(value)
                value["country"] = _normalize_country(value.get("country"))
        if (
            out_key.startswith("is_")
            or out_key in {"agreements_accepted", "permanent_resident", "is_open_crypto"}
        ):
            value = _normalize_bool(value)
        if out_key == "tax_id":
            tax_id_country = data.get("tax_id_country") or payload.get("tax_id_country")
            payload["weight_form"] = {"tax_id": value}
            if tax_id_country:
                payload["weight_form"]["tax_id_country"] = _normalize_country(tax_id_country)
            continue
        if out_key == "tax_id_country" and "weight_form" in payload:
            payload["weight_form"]["tax_id_country"] = _normalize_country(value)
            continue
        if out_key in {"drivers_license", "government_issued_id"}:
            document = normalize_document_value(value)
            payload[out_key] = document
            if out_key == "drivers_license":
                if document.get("front"):
                    payload.setdefault("drivers_licence_front", document["front"])
                if document.get("back"):
                    payload.setdefault("drivers_licence_back", document["back"])
            continue
        if out_key in {"passport_photo", "card_photo", "address_proof", "visa"}:
            payload[out_key] = normalize_single_file_value(value)
            continue
        payload[out_key] = value
    if "tax_id" in payload:
        tax_id = payload.pop("tax_id")
        tax_id_country = payload.pop("tax_id_country", None)
        payload["weight_form"] = {"tax_id": tax_id}
        if tax_id_country:
            payload["weight_form"]["tax_id_country"] = _normalize_country(tax_id_country)
    elif "weight_form" in payload and "tax_id_country" in payload:
        payload["weight_form"]["tax_id_country"] = _normalize_country(payload.pop("tax_id_country"))
    return payload


def write_tool_upload_file(
    *,
    trajectory_id: str,
    filename: str,
    file_data: str | None,
    session_root: str | None = None,
) -> str:
    if not file_data:
        if filename and Path(filename).expanduser().is_file():
            return os.fspath(Path(filename).expanduser())
        return os.fspath(real_bank_api_scripts_dir() / DEFAULT_DOCUMENT_FILENAME)

    root = Path(session_root or real_bank_session_root()) / "uploads" / _safe_id(trajectory_id)
    root.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_id(filename or "document.bin")
    path = root / safe_name
    if "," in file_data and file_data.split(",", 1)[0].startswith("data:"):
        file_data = file_data.split(",", 1)[1]
    path.write_bytes(base64.b64decode(file_data))
    return os.fspath(path)


def bank_rule_score_from_tool_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    bank_states: list[dict[str, Any]] = []
    submit_attempted = False
    submit_success = False
    for item in results:
        if item.get("tool") == "submit_application":
            submit_attempted = True
            if item.get("bank_submit_success"):
                submit_success = True
        state = item.get("state", {})
        if not isinstance(state, dict):
            state = {}
        merged = dict(state)
        if item.get("bank_submit_success"):
            merged["bank_submit_success"] = True
            merged["submitted"] = True
        for key in ("bank_auth_bypass", "bank_send_rate_limit_bypass"):
            if key in item:
                merged[key] = item[key]
        progress = item.get("bank_progress_response")
        progress_data = progress.get("d") if isinstance(progress, dict) else None
        if isinstance(progress_data, dict):
            if progress_data.get("status"):
                merged["bank_status"] = progress_data.get("status")
            if "missing_fields" in progress_data:
                merged["bank_missing_fields"] = progress_data.get("missing_fields") or []
            if "completion_percentage" in progress_data:
                merged["bank_completion_percentage"] = progress_data.get("completion_percentage")
            merged["bank_query_ok"] = True
        if merged.get("backend") != "real_bank" and any(str(key).startswith("bank_") for key in merged):
            merged["backend"] = "real_bank"
        if merged.get("backend") == "real_bank":
            bank_states.append(merged)
    if not bank_states:
        return {"available": False, "score": 0.0}
    last = bank_states[-1]
    status = str(last.get("bank_status") or "").upper()
    missing = last.get("bank_missing_fields") or []
    if not missing and status not in {"AUDITING", "OPENED"}:
        missing = last.get("missing_fields") or []
    completion = last.get("bank_completion_percentage")
    submitted = bool(submit_success)
    stale_terminal = status in {"AUDITING", "OPENED"} and not submit_success
    score = 1.0 if submit_success else 0.0
    return {
        "available": True,
        "score": score,
        "bank_status": status,
        "bank_missing_count": len(missing),
        "bank_completion_percentage": completion,
        "bank_submit_success": submit_success,
        "bank_submitted": submitted,
        "bank_submit_attempted": submit_attempted,
        "bank_stale_terminal_status": stale_terminal,
    }
