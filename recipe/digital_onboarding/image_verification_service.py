"""Tiny HTTP image verifier for digital-onboarding upload tests."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from recipe.digital_onboarding.image_verification import verify_image_upload_locally


class VerifyImageRequest(BaseModel):
    file_data: str = Field(..., min_length=1)
    filename: str = "document.png"
    trajectory_id: str = ""
    doc_type: str = "drivers_license_front"


app = FastAPI(title="Digital Onboarding Image Verification")


@app.post("/verify-image")
async def verify_image(request: VerifyImageRequest) -> dict[str, Any]:
    try:
        return verify_image_upload_locally(
            file_data=request.file_data,
            filename=request.filename,
            trajectory_id=request.trajectory_id,
            doc_type=request.doc_type,
        )
    except Exception as exc:
        return {
            "verified": False,
            "message": str(exc),
            "error_code": "invalid_image",
        }

