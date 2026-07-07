"""Deterministic option hints for fixed-choice onboarding fields."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ChoiceHint:
    field_key: str
    label: str
    options: tuple[str, ...]
    patterns: tuple[str, ...]


INCOME_RANGE_OPTIONS = (
    "0-100K USD",
    "100K-200K USD",
    "200K-500K USD",
    "500K-1M USD",
    "1M-5M USD",
    "Greater than 5M USD",
)


CHOICE_HINTS: tuple[ChoiceHint, ...] = (
    ChoiceHint("account_type", "account type", ("CASH", "MARGIN"), (r"\baccount\s+type\b", r"\bcash\s+or\s+margin\b")),
    ChoiceHint("is_open_crypto", "crypto trading", ("Yes", "No"), (r"\bcrypto\b", r"\bopen\s+crypto\b")),
    ChoiceHint("gender", "gender", ("MALE", "FEMALE", "OTHER"), (r"\bgender\b",)),
    ChoiceHint("marital_status", "marital status", ("MARRIED", "SINGLE", "DIVORCED", "WIDOWED"), (r"\bmarital\s+status\b",)),
    ChoiceHint(
        "employment_status",
        "employment status",
        ("EMPLOYED", "SELF_EMPLOYED", "UNEMPLOYED", "RETIRED", "STUDENT"),
        (r"\bemployment\s+status\b",),
    ),
    ChoiceHint(
        "industry",
        "industry",
        (
            "Agriculture",
            "Business Management",
            "Construction",
            "Education",
            "Environmental",
            "Finance",
            "Food & Hospitality",
            "Gaming",
            "Health Services",
            "Information Technology",
            "Insurance",
            "Legal",
            "Motor Vehicle",
            "Real Estate",
            "Security",
            "Telecom",
            "Transportation",
            "Utilities",
            "Other",
        ),
        (r"\bindustry\b",),
    ),
    ChoiceHint(
        "funding_source",
        "funding source",
        ("Savings", "Inheritance", "Pension", "Rental Income", "Social Security", "Other"),
        (
            r"\bfunding\s+source\b",
            r"\bsource\s+of\s+(?:your\s+)?(?:funds|funding|investment\s+funds)\b",
            r"\bprimary\s+source\b",
        ),
    ),
    ChoiceHint(
        "annual_income_usd",
        "annual income",
        INCOME_RANGE_OPTIONS,
        (r"\bannual\s+income\b", r"\bannual\s+incoming\b", r"\bincome\s+range\b"),
    ),
    ChoiceHint("liquid_net_worth_usd", "liquid net worth", INCOME_RANGE_OPTIONS, (r"\bliquid\s+net\s+worth\b",)),
    ChoiceHint("total_net_worth_usd", "total net worth", INCOME_RANGE_OPTIONS, (r"\btotal\s+net\s+worth\b",)),
    ChoiceHint(
        "investment_experience",
        "investment experience",
        ("EXTENSIVE", "GOOD", "LIMITED", "NONE"),
        (r"\binvestment\s+experience\b", r"\btrading\s+experience\b"),
    ),
    ChoiceHint(
        "investment_objective",
        "investment objective",
        ("GROWTH", "INCOME", "CAPITAL_PRESERVATION", "SPECULATION", "OTHER"),
        (r"\binvestment\s+objective\b", r"\binvestment\s+goal\b"),
    ),
    ChoiceHint("time_horizon", "time horizon", ("LONGEST", "AVERAGE", "SHORT"), (r"\btime\s+horizon\b",)),
    ChoiceHint("risk_tolerance", "risk tolerance", ("HIGH", "MEDIUM", "LOW"), (r"\brisk\s+tolerance\b",)),
    ChoiceHint(
        "liquidity_needs",
        "liquidity needs",
        ("VERY_IMPORTANT", "SOMEWHAT_IMPORTANT", "NOT_IMPORTANT"),
        (r"\bliquidity\s+needs?\b", r"\bliquidity\b"),
    ),
    ChoiceHint("is_control_person", "control person status", ("Yes", "No"), (r"\bcontrol\s+person\b",)),
    ChoiceHint("is_affiliated_exchangeorfinra", "FINRA or exchange affiliation", ("Yes", "No"), (r"\bfinra\b", r"\bexchange\b")),
    ChoiceHint("is_politically_exposed", "politically exposed status", ("Yes", "No"), (r"\bpolitically\s+exposed\b",)),
    ChoiceHint("is_trade_authorization", "trade authorization", ("Yes", "No"), (r"\btrade\s+authorization\b",)),
    ChoiceHint("is_identify", "identity confirmation", ("Yes", "No"), (r"\bidentity\b", r"\bidentify\b")),
    ChoiceHint("agreements_accepted", "agreement acceptance", ("Yes", "No"), (r"\bagreements?\b", r"\baccept\b")),
)


QUESTION_RE = re.compile(
    r"\?|(?:could|can|would)\s+you\b|\bplease\b|\bconfirm\b|\bselect\b|\bchoose\b|\bprovide\b|"
    r"\btell\s+me\b|\bwhat(?:'s|\s+is)\s+your\b|\bwhich\b",
    re.IGNORECASE,
)
TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.IGNORECASE | re.DOTALL)
THINK_RE = re.compile(r"<think>.*?</think>|<think>.*$", re.IGNORECASE | re.DOTALL)


def choice_hints_enabled() -> bool:
    return os.environ.get("DIGITAL_ONBOARDING_APPEND_CHOICE_OPTIONS", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _visible_text(text: str) -> str:
    text = THINK_RE.sub("", text or "")
    text = TOOL_CALL_RE.sub("", text)
    return text.strip()


def _question_segments(text: str) -> list[str]:
    visible = _visible_text(text)
    if not visible:
        return []
    segments = [part.strip() for part in re.findall(r"[^.!?\n]+[.!?\n]?", visible) if part.strip()]
    question_segments = [segment for segment in segments if QUESTION_RE.search(segment)]
    if question_segments:
        return question_segments
    return [visible] if QUESTION_RE.search(visible) else []


def _field_already_has_options(text: str, hint: ChoiceHint) -> bool:
    lowered = _visible_text(text).lower()
    hits = 0
    for option in hint.options:
        pattern = r"(?<![a-z0-9_])" + re.escape(option.lower()) + r"(?![a-z0-9_])"
        if re.search(pattern, lowered):
            hits += 1
            if hits >= 2:
                return True
    return "available options" in lowered and hint.label in lowered


def choice_hints_for_text(text: str) -> list[ChoiceHint]:
    if not choice_hints_enabled() or TOOL_CALL_RE.search(text or ""):
        return []
    found: list[ChoiceHint] = []
    seen: set[str] = set()
    for segment in _question_segments(text):
        for hint in CHOICE_HINTS:
            if hint.field_key in seen or _field_already_has_options(text, hint):
                continue
            if any(re.search(pattern, segment, re.IGNORECASE) for pattern in hint.patterns):
                found.append(hint)
                seen.add(hint.field_key)
    return found


def append_choice_options(text: str) -> str:
    hints = choice_hints_for_text(text)
    if not hints:
        return text

    blocks: list[str] = []
    for hint in hints:
        title = "Available options" if len(hints) == 1 else f"Available {hint.label} options"
        lines = [f"{title}:"] + [f"* {option}" for option in hint.options]
        blocks.append("\n".join(lines))
    return text.rstrip() + "\n\n" + "\n\n".join(blocks)


def choice_hint_widgets(text: str) -> list[dict[str, object]]:
    widgets: list[dict[str, object]] = []
    for hint in choice_hints_for_text(text):
        widgets.append(
            {
                "kind": "options",
                "tool": "auto_choice_hint",
                "question": f"Choose {hint.label}",
                "options": list(hint.options),
                "choice_type": "single",
                "layout": "buttons",
                "field_key": hint.field_key,
            }
        )
    return widgets
