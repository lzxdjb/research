"""Runtime guidance for bank country-code fields."""

from __future__ import annotations

import re


CITIZENSHIP_CODE_GUIDANCE = (
    "Important: Please provide only the 3-letter country code, not the full country name.\n\n"
    "Examples:\n"
    "* United States -> USA\n"
    "* China -> CHN\n"
    "* Canada -> CAN\n\n"
    "If you are unsure of your country's code, you can look it up here:\n"
    "https://www.iban.com/country-codes"
)

_CITIZENSHIP_RE = re.compile(r"\bcitizenship(?:\s+country)?\b", re.IGNORECASE)
_QUESTIONISH_RE = re.compile(
    r"\?|"
    r"\b(?:provide|enter|type|give|share|tell|need|needs|ask|asking|confirm|format|formatted|issue|again)\b|"
    r"\bwhat(?:'s|\s+is)\b|"
    r"\b(?:could|can|would)\s+you\b",
    re.IGNORECASE,
)
_ALREADY_HAS_GUIDANCE_RE = re.compile(
    r"3-letter\s+country\s+code|iban\.com/country-codes|\bCHN\b",
    re.IGNORECASE,
)


def append_citizenship_country_code_guidance(text: str) -> str:
    """Append ISO alpha-3 examples when the service asks for citizenship."""

    if not text or _ALREADY_HAS_GUIDANCE_RE.search(text):
        return text
    if not _CITIZENSHIP_RE.search(text) or not _QUESTIONISH_RE.search(text):
        return text
    return text.rstrip() + "\n\n" + CITIZENSHIP_CODE_GUIDANCE
