"""Customer-side document image verification for digital onboarding.

The service model should not decide whether a document was uploaded. This
module is the small external boundary used by the customer simulator and the
browser upload endpoint: it accepts bytes, verifies they are an actual image,
stores a copy, and returns upload metadata.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SAMPLE_IMAGE = PROJECT_ROOT / "Weixin Image_2026-06-01_155311_574.png"
DEFAULT_VERIFICATION_ROOT = PROJECT_ROOT / "open-account" / "scripts" / ".digital_onboarding_sessions" / "verified_uploads"


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    return safe[:160] or "upload"


def sample_upload_image_path() -> str:
    return os.environ.get("DIGITAL_ONBOARDING_CUSTOMER_UPLOAD_IMAGE_PATH", os.fspath(DEFAULT_SAMPLE_IMAGE))


def real_bank_upload_image_path() -> str:
    """Image path sent to the real-bank upload endpoint.

    By default this matches the customer upload image so existing runs keep the
    same behavior. Set DIGITAL_ONBOARDING_REAL_BANK_UPLOAD_IMAGE_PATH to send a
    smaller payload to the bank while leaving the customer/model image intact.
    """

    return os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_UPLOAD_IMAGE_PATH", sample_upload_image_path())


def verification_root() -> Path:
    return Path(os.environ.get("DIGITAL_ONBOARDING_IMAGE_VERIFICATION_ROOT", os.fspath(DEFAULT_VERIFICATION_ROOT)))


def _decode_file_data(file_data: str | bytes) -> bytes:
    if isinstance(file_data, bytes):
        return file_data
    data = file_data.strip()
    if "," in data and data.split(",", 1)[0].startswith("data:"):
        data = data.split(",", 1)[1]
    return base64.b64decode(data)


def _image_metadata(data: bytes) -> dict[str, Any]:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            image_format = image.format or "IMAGE"
            mode = image.mode
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Uploaded content is not a valid image.") from exc
    if width <= 0 or height <= 0:
        raise ValueError("Uploaded image has invalid dimensions.")
    return {"width": width, "height": height, "format": image_format, "mode": mode}


def _local_verify_image_upload(
    *,
    file_data: str | bytes,
    filename: str,
    trajectory_id: str = "",
    doc_type: str = "drivers_license_front",
) -> dict[str, Any]:
    data = _decode_file_data(file_data)
    metadata = _image_metadata(data)
    digest = hashlib.sha256(data).hexdigest()
    verification_id = f"imgver_{digest[:16]}"
    root = verification_root() / _safe_id(trajectory_id or "default")
    root.mkdir(parents=True, exist_ok=True)
    safe_filename = _safe_id(filename or "document.png")
    stored_path = root / f"{verification_id}_{safe_filename}"
    stored_path.write_bytes(data)
    file_id = f"file_{digest[:16]}"
    min_file_id = f"min_{digest[16:32]}"
    return {
        "verified": True,
        "verification_id": verification_id,
        "filename": filename or safe_filename,
        "stored_path": os.fspath(stored_path),
        "file_url": f"verified-upload://{verification_id}/{safe_filename}",
        "file_id": file_id,
        "min_file_id": min_file_id,
        "sha256": digest,
        "doc_type": doc_type,
        **metadata,
    }


def verify_image_upload_locally(
    *,
    file_data: str | bytes,
    filename: str,
    trajectory_id: str = "",
    doc_type: str = "drivers_license_front",
) -> dict[str, Any]:
    """Verify image bytes in-process without calling an HTTP endpoint."""

    return _local_verify_image_upload(
        file_data=file_data,
        filename=filename,
        trajectory_id=trajectory_id,
        doc_type=doc_type,
    )


def verify_image_upload(
    *,
    file_data: str | bytes,
    filename: str,
    trajectory_id: str = "",
    doc_type: str = "drivers_license_front",
) -> dict[str, Any]:
    """Verify an uploaded image, optionally through an HTTP verification service."""

    endpoint = os.environ.get("DIGITAL_ONBOARDING_IMAGE_VERIFICATION_ENDPOINT", "").strip()
    if endpoint:
        if isinstance(file_data, bytes):
            payload_file_data = base64.b64encode(file_data).decode("ascii")
        else:
            payload_file_data = file_data
        verify_url = endpoint if endpoint.rstrip("/").endswith("/verify-image") else endpoint.rstrip("/") + "/verify-image"
        payload = {
            "file_data": payload_file_data,
            "filename": filename,
            "trajectory_id": trajectory_id,
            "doc_type": doc_type,
        }
        request = urllib.request.Request(
            verify_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=float(os.environ.get("DIGITAL_ONBOARDING_IMAGE_VERIFICATION_TIMEOUT", "30"))) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("verified"):
            raise ValueError(str(result.get("message") or "Image verification failed."))
        return result

    return verify_image_upload_locally(
        file_data=file_data,
        filename=filename,
        trajectory_id=trajectory_id,
        doc_type=doc_type,
    )


def verify_sample_image_upload(*, trajectory_id: str = "", doc_type: str = "drivers_license_front") -> dict[str, Any]:
    path = Path(sample_upload_image_path()).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Sample upload image not found: {path}")
    return verify_image_upload(
        file_data=path.read_bytes(),
        filename=path.name,
        trajectory_id=trajectory_id,
        doc_type=doc_type,
    )
