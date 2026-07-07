"""Deterministic terms disclosure text for onboarding agreement requests."""

from __future__ import annotations

import os
import re


TERMS_AND_CONDITIONS_TEXT = """Light Horse brokerage account terms and conditions:

1. Account opening: You certify that the information you provide is accurate, complete, and current.
2. Identity verification: You authorize Light Horse to verify your identity and perform KYC, AML, sanctions, and fraud-prevention checks.
3. Brokerage services: You understand that brokerage accounts involve market risk, including possible loss of principal.
4. Investment profile: You agree to provide accurate investment objectives, risk tolerance, financial information, and experience information.
5. Funding source: You certify that account funds come from lawful sources and are not connected to prohibited activity.
6. Documents and records: You consent to electronic delivery, electronic signatures, and electronic account records where permitted by law.
7. Privacy and data use: Your information may be used to open, maintain, service, supervise, and comply with legal obligations for your account.
8. Customer agreements: You agree to the applicable customer agreement, privacy policy, margin or options disclosures if applicable, and all platform rules shown during account opening.

Please confirm whether you have read and agree to these terms and conditions."""

TERMS_MARKER = "Light Horse brokerage account terms and conditions:"
TERMS_REQUEST_RE = re.compile(
    r"("
    r"\b(?:do|can|could|would)\s+you\s+(?:please\s+)?(?:confirm\s+)?(?:agree|accept)\b.{0,120}\b(?:terms|conditions|agreements?|disclosures?)\b"
    r"|\b(?:confirm|accept|agree)\b.{0,120}\b(?:terms|conditions|agreements?|disclosures?)\b"
    r"|\b(?:terms\s+and\s+conditions|customer\s+agreement|privacy\s+policy)\b.{0,80}\b(?:agree|accept|confirm)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def disclosure_terms_enabled() -> bool:
    return os.environ.get("DIGITAL_ONBOARDING_APPEND_DISCLOSURE_TERMS", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def assistant_requests_terms_agreement(text: str) -> bool:
    if not text:
        return False
    if "?" not in text and not re.search(r"\b(?:please|could|can|would|do\s+you|next)\b", text, re.IGNORECASE):
        return False
    return bool(TERMS_REQUEST_RE.search(text))


def append_terms_if_needed(text: str) -> str:
    if not disclosure_terms_enabled() or not assistant_requests_terms_agreement(text):
        return text
    if TERMS_MARKER.lower() in (text or "").lower():
        return text
    return (text or "").rstrip() + "\n\n" + TERMS_AND_CONDITIONS_TEXT
