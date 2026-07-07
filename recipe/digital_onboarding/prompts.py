"""Prompt loading helpers for the digital onboarding recipe."""

from __future__ import annotations

from pathlib import Path


RECIPE_DIR = Path(__file__).resolve().parent
PROMPT_DIR = RECIPE_DIR / "prompts"


def _read_prompt(path: Path, fallback: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback.strip()
    return text or fallback.strip()


SERVICE_SYSTEM_PROMPT = _read_prompt(
    RECIPE_DIR / "service_system_prompt.md",
    "You are a voice-first onboarding specialist for a US brokerage account-opening flow.",
)

CUSTOMER_SIMULATOR_ROLE_PROMPT = _read_prompt(
    PROMPT_DIR / "customer_simulator_system_prompt.md",
    'You are a simulated customer. Return JSON only: {"response": "..."}',
)

REWARD_MODEL_ROLE_PROMPT = _read_prompt(
    PROMPT_DIR / "reward_model_system_prompt.md",
    'You are a reward model. Return JSON only: {"score": 0.0, "reason": ""}',
)

TEACHER_122B_ROLE_PROMPT = _read_prompt(
    PROMPT_DIR / "teacher_122b_system_prompt.md",
    'You are a 122B teacher. Return valid JSON only.',
)


def _compose_with_service_rules(role_prompt: str) -> str:
    return (
        "Below is the complete workflow and requirement set that the service model should obey.\n"
        "Use it as the rule book for your assigned role.\n\n"
        "===== SERVICE WORKFLOW AND REQUIREMENTS =====\n"
        f"{SERVICE_SYSTEM_PROMPT}\n\n"
        "===== ASSIGNED ROLE INSTRUCTIONS =====\n"
        f"{role_prompt}"
    )


CUSTOMER_SIMULATOR_SYSTEM_PROMPT = _compose_with_service_rules(CUSTOMER_SIMULATOR_ROLE_PROMPT)
REWARD_MODEL_SYSTEM_PROMPT = _compose_with_service_rules(REWARD_MODEL_ROLE_PROMPT)
TEACHER_122B_SYSTEM_PROMPT = _compose_with_service_rules(TEACHER_122B_ROLE_PROMPT)
