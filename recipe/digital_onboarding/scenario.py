"""Synthetic account-opening scenarios for tool-use RL.

The scenario is the hidden user goal. The policy model only sees the initial
user request; tools and the simulated user can see the scenario through
``extra_info`` so they can behave like a deterministic environment.
"""

from __future__ import annotations

import copy
import os
import random
from pathlib import Path
from typing import Any


DEFAULT_REQUIRED_FIELDS = [
    "account_type",
    "given_name",
    "family_name",
    "date_of_birth",
    "gender",
    "marital_status",
    "num_dependents",
    "citizenship_country",
    "birth_country",
    "home_address",
    "employment_status",
    "funding_source",
    "annual_income_usd_min",
    "annual_income_usd_max",
    "liquid_net_worth_usd_min",
    "liquid_net_worth_usd_max",
    "total_net_worth_usd_min",
    "total_net_worth_usd_max",
    "investment_experience",
    "investment_objective",
    "time_horizon",
    "risk_tolerance",
    "liquidity_needs",
    "is_control_person",
    "is_affiliated_exchangeorfinra",
    "is_politically_exposed",
    "is_trade_authorization",
    "is_identify",
    "agreements_accepted",
    "drivers_license",
]

DOMESTIC_REQUIRED_FIELDS = [
    "account_type",
    "given_name",
    "family_name",
    "date_of_birth",
    "gender",
    "marital_status",
    "num_dependents",
    "citizenship_country",
    "birth_country",
    "permanent_resident",
    "social_security_number",
    "home_address",
    "employment_status",
    "funding_source",
    "annual_income_usd_min",
    "annual_income_usd_max",
    "liquid_net_worth_usd_min",
    "liquid_net_worth_usd_max",
    "total_net_worth_usd_min",
    "total_net_worth_usd_max",
    "investment_experience",
    "investment_objective",
    "time_horizon",
    "risk_tolerance",
    "liquidity_needs",
    "is_control_person",
    "is_affiliated_exchangeorfinra",
    "is_politically_exposed",
    "is_trade_authorization",
    "is_identify",
    "agreements_accepted",
    "drivers_license",
]

US_CITIZEN_REQUIRED_FIELDS = [field for field in DOMESTIC_REQUIRED_FIELDS if field != "permanent_resident"]

US_PERMANENT_RESIDENT_REQUIRED_FIELDS = [
    field for field in DOMESTIC_REQUIRED_FIELDS if field != "drivers_license"
] + [
    "passport_photo",
    "passport_no",
    "passport_expire_date",
    "card_photo",
]

US_VISA_REQUIRED_FIELDS = [
    field for field in DOMESTIC_REQUIRED_FIELDS if field != "drivers_license"
] + [
    "passport_photo",
    "passport_no",
    "passport_expire_date",
    "visa",
    "visa_type",
    "visa_expiration_date",
]

FOREIGNER_REQUIRED_FIELDS = [
    "account_type",
    "given_name",
    "family_name",
    "date_of_birth",
    "gender",
    "marital_status",
    "num_dependents",
    "citizenship_country",
    "birth_country",
    "tax_id",
    "tax_id_country",
    "passport_photo",
    "address_proof",
    "home_address",
    "employment_status",
    "funding_source",
    "annual_income_usd_min",
    "annual_income_usd_max",
    "liquid_net_worth_usd_min",
    "liquid_net_worth_usd_max",
    "total_net_worth_usd_min",
    "total_net_worth_usd_max",
    "investment_experience",
    "investment_objective",
    "time_horizon",
    "risk_tolerance",
    "liquidity_needs",
    "is_control_person",
    "is_affiliated_exchangeorfinra",
    "is_politically_exposed",
    "is_trade_authorization",
    "is_identify",
    "agreements_accepted",
]

USER_BEHAVIORS = [
    "cooperative",
    "forgot_mobile_use_email",
    "forgot_email_use_mobile",
    "no_auth_contact",
    "mobile_required_user_will_return",
    "wrong_code_once",
    "passport_only",
]

FINISHABLE_USER_BEHAVIORS = [
    "cooperative",
    "forgot_mobile_use_email",
    "forgot_email_use_mobile",
    "wrong_code_once",
]

UNFINISHABLE_USER_BEHAVIORS = [
    "no_auth_contact",
    "mobile_required_user_will_return",
]

FALLBACK_SYSTEM_PROMPT = """You are a voice-first onboarding specialist for a US brokerage account-opening flow.

Your job is to complete the user's account-opening application by using tools correctly.

Rules:
- Ask one question at a time.
- Use send_verification_code only after the user gives a real phone number or email.
- If the user cannot provide one login method, offer another valid login method such as email or mobile.
- If no valid login/contact method is available, politely explain that account opening cannot continue without authentication.
- Use login_and_get_token only after the user gives the verification code.
- Call query_progress after login, after each successful submit_* section, after document submission, and before final submission.
- Treat query_progress missing_fields/bank_missing_fields as the source of truth. Continue from those fields and do not ask for fields already collected.
- Call collect_information after each useful user answer.
- For dates, phones, emails, disclosures, account type, employment status, income, risk, and other fixed choices, call the UI widget tool before speaking.
- For documents, call capture_document, wait for CAPTURE_RESULT, call extract_document_info, show readable fields in a normal assistant message, wait for user confirmation/correction, then submit the required document metadata.
- Never claim a tool action succeeded until the tool result says it succeeded.
- Never call submit_application until query_progress shows no missing fields and the user has confirmed submission.
- If submit_application fails with missing_fields, do not submit again immediately; query progress and collect the reported missing fields first.

Use tools with the tool-call format required by the chat template. The hidden goal is successful and compliant account opening, not a long conversation."""


def _prompt_candidates() -> list[Path]:
    here = Path(__file__).resolve()
    candidates = []
    override = os.environ.get("DIGITAL_ONBOARDING_SYSTEM_PROMPT_PATH")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(here.with_name("service_system_prompt.md"))
    return candidates


def _load_system_prompt() -> str:
    for path in _prompt_candidates():
        if path.is_file():
            prompt = path.read_text(encoding="utf-8").strip()
            if prompt:
                return prompt
    return FALLBACK_SYSTEM_PROMPT


SYSTEM_PROMPT = _load_system_prompt()


def _profile(i: int) -> dict[str, Any]:
    names = [
        ("Maya", "Chen"),
        ("Daniel", "Rivera"),
        ("Alex", "Morgan"),
        ("Priya", "Shah"),
        ("Jordan", "Lee"),
        ("Sofia", "Garcia"),
    ]
    given, family = names[i % len(names)]
    income_pairs = [(0, 25000), (25001, 50000), (50001, 100000), (100001, 200000), (200001, 300000), (300001, 500000)]
    income_min, income_max = income_pairs[i % len(income_pairs)]
    account_type = "CASH"
    mobile = f"202604{i % 10000:04d}"
    email = f"user{i:04d}@gmail.com"
    contact_type = "EMAIL" if i % 2 else "MOBILE"
    contact = email if contact_type == "EMAIL" else mobile
    employment_statuses = ["EMPLOYED", "SELF_EMPLOYED", "UNEMPLOYED", "RETIRED", "STUDENT"]
    employment_status = employment_statuses[i % len(employment_statuses)]
    funding_sources = ["Savings", "Inheritance", "Pension", "Rental Income", "Social Security", "Other"]
    funding_source = funding_sources[i % len(funding_sources)]
    if employment_status == "EMPLOYED":
        employer = "Acme Analytics"
        position = "Product Manager"
        industry = "Technology"
    elif employment_status == "SELF_EMPLOYED":
        employer = "Self-employed"
        position = "Independent Consultant"
        industry = "Business Management"
    else:
        employer = "N/A"
        position = "N/A"
        industry = "N/A"
    profile = {
        "contact_type": contact_type,
        "contact": contact,
        "mobile": mobile,
        "email": email,
        "area_code": "1",
        "auth_contacts": {
            "MOBILE": {"contact": mobile, "area_code": "1"},
            "EMAIL": {"contact": email},
        },
        "available_auth_methods": ["MOBILE", "EMAIL"],
        "required_auth_methods": [],
        "verification_code": "123456",
        "branch": "DOMESTIC",
        "account_type": account_type,
        "is_open_crypto": False,
        "given_name": given,
        "gvie_name": given,
        "middle_name": "",
        "family_name": family,
        "date_of_birth": f"19{80 + (i % 18):02d}-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
        "gender": ["FEMALE", "MALE", "OTHER"][i % 3],
        "marital_status": ["SINGLE", "MARRIED", "DIVORCED"][i % 3],
        "num_dependents": i % 3,
        "citizenship_country": "USA",
        "birth_country": "USA",
        "permanent_resident": True,
        "social_security_number": f"999-12-{i % 10000:04d}",
        "home_address": {
            "country": "USA",
            "state": ["CA", "NY", "TX", "WA"][i % 4],
            "city": ["San Mateo", "Brooklyn", "Austin", "Seattle"][i % 4],
            "postal_code": f"94{i % 1000:03d}",
            "street_address1": f"{100 + i} Market St",
            "street_address2": "",
        },
        "employment_status": employment_status,
        "employer": employer,
        "position_employed": position,
        "years_employed": 3 + (i % 8),
        "industry": industry,
        "funding_source": funding_source,
        "annual_income_usd_min": income_min,
        "annual_income_usd_max": income_max,
        "liquid_net_worth_usd_min": income_min,
        "liquid_net_worth_usd_max": income_max,
        "total_net_worth_usd_min": income_min,
        "total_net_worth_usd_max": income_max * 2 if income_max < 999999999 else income_max,
        "investment_experience": ["NONE", "LIMITED", "GOOD", "EXTENSIVE"][i % 4],
        "investment_objective": ["GROWTH", "INCOME", "CAPITAL_PRESERVATION", "SPECULATION"][i % 4],
        "time_horizon": ["SHORT", "AVERAGE", "LONGEST"][i % 3],
        "risk_tolerance": ["LOW", "MEDIUM", "HIGH"][i % 3],
        "liquidity_needs": ["NOT_IMPORTANT", "SOMEWHAT_IMPORTANT", "VERY_IMPORTANT"][i % 3],
        "is_control_person": False,
        "is_affiliated_exchangeorfinra": False,
        "is_politically_exposed": False,
        "is_trade_authorization": False,
        "is_identify": True,
        "agreements_accepted": True,
        "drivers_license": {
            "front": {"file_id": f"file_dl_front_{i:04d}", "min_file_id": f"min_dl_front_{i:04d}", "expire_date": "2030-12-31"},
            "back": {"file_id": f"file_dl_back_{i:04d}", "min_file_id": f"min_dl_back_{i:04d}"},
        },
        "passport_photo": {"file_id": f"file_passport_{i:04d}", "min_file_id": f"min_passport_{i:04d}"},
        "address_proof": {"file_id": f"file_address_{i:04d}", "min_file_id": f"min_address_{i:04d}"},
        "passport_no": f"P{i:08d}",
        "passport_expire_date": "2032-12-31",
    }
    if funding_source == "Other":
        profile["other_source"] = "Family support and occasional consulting income"
    return profile


def _foreigner_profile(i: int) -> dict[str, Any]:
    profile = _profile(i)
    country_rows = [
        ("CHN", "86", "Shanghai", "Shanghai", "200120", "88 Century Ave", "CN"),
        ("IND", "91", "Bengaluru", "Karnataka", "560001", "42 MG Road", "IN"),
        ("MEX", "52", "Mexico City", "CDMX", "06000", "15 Reforma", "MX"),
        ("CAN", "1", "Toronto", "Ontario", "M5H 2N2", "100 King St W", "CA"),
    ]
    country, area_code, city, state, postal, street, tax_prefix = country_rows[i % len(country_rows)]
    mobile = f"{700000000 + (i % 1000000):09d}" if area_code != "1" else f"416555{i % 10000:04d}"
    email = f"foreigner{i:04d}@gmail.com"
    contact_type = "EMAIL" if i % 2 else "MOBILE"
    profile.update(
        {
            "branch": "FOREIGNER",
            "contact_type": contact_type,
            "contact": email if contact_type == "EMAIL" else mobile,
            "mobile": mobile,
            "email": email,
            "email_address": email,
            "area_code": area_code,
            "auth_contacts": {
                "MOBILE": {"contact": mobile, "area_code": area_code},
                "EMAIL": {"contact": email},
            },
            "citizenship_country": country,
            "birth_country": country,
            "permanent_resident": False,
            "tax_id": f"{tax_prefix}-TAX-{i:06d}",
            "tax_id_country": country,
            "weight_form": {"tax_id": f"{tax_prefix}-TAX-{i:06d}", "tax_id_country": country},
            "home_address": {
                "country": country,
                "state": state,
                "city": city,
                "postal_code": postal,
                "street_address1": street,
                "street_address2": "",
            },
            "passport_photo": {"file_id": f"file_passport_{i:04d}", "min_file_id": f"min_passport_{i:04d}"},
            "address_proof": {"file_id": f"file_address_{i:04d}", "min_file_id": f"min_address_{i:04d}"},
            "passport_no": f"F{i:08d}",
            "passport_expire_date": "2032-12-31",
            "social_security_number": "",
        }
    )
    if country == "CHN":
        profile["card_photo"] = {"file_id": f"file_idcard_{i:04d}", "min_file_id": f"min_idcard_{i:04d}"}
        profile["tax_id"] = f"11010119{80 + (i % 18):02d}{(i % 12) + 1:02d}{(i % 27) + 1:02d}{i % 10000:04d}"
        profile["weight_form"] = {"tax_id": profile["tax_id"], "tax_id_country": "CHN"}
    return profile


def _us_resident_noncitizen_profile(i: int, *, permanent_resident: bool) -> dict[str, Any]:
    """Profile for the U.S.-market non-citizen branches.

    The product launches only in the U.S. market, so these customers live in
    the United States and authenticate with U.S. phone numbers even when their
    citizenship country is not USA.
    """

    profile = _profile(i)
    countries = ["CAN", "MEX", "IND", "CHN"]
    country = countries[i % len(countries)]
    profile.update(
        {
            "branch": "DOMESTIC",
            "residency_category": "US_PERMANENT_RESIDENT" if permanent_resident else "US_VISA",
            "citizenship_country": country,
            "birth_country": country,
            "permanent_resident": bool(permanent_resident),
            "passport_photo": {"file_id": f"file_passport_{i:04d}", "min_file_id": f"min_passport_{i:04d}"},
            "passport_no": f"N{i:08d}",
            "passport_expire_date": "2032-12-31",
        }
    )
    # Keep residence in the U.S. for both non-citizen sub-branches.
    if isinstance(profile.get("home_address"), dict):
        profile["home_address"] = copy.deepcopy(profile["home_address"])
        profile["home_address"]["country"] = "USA"
    if permanent_resident:
        profile["card_photo"] = {"file_id": f"file_green_card_{i:04d}", "min_file_id": f"min_green_card_{i:04d}"}
    else:
        profile["visa"] = {"file_id": f"file_visa_{i:04d}", "min_file_id": f"min_visa_{i:04d}"}
        profile["visa_type"] = ["H1B", "F1", "L1", "E2"][i % 4]
        profile["visa_expiration_date"] = "2031-12-31"
    return profile


def _select_behavior(index: int, behavior_mode: str) -> str:
    behavior_mode = (behavior_mode or "mixed").strip().lower()
    if behavior_mode == "cooperative":
        return "cooperative"
    if behavior_mode == "mixed":
        return USER_BEHAVIORS[index % len(USER_BEHAVIORS)]
    if behavior_mode in {"phase1", "phase_1"}:
        return "cooperative"
    if behavior_mode in {"finishable", "can_finish", "can-finish"}:
        return FINISHABLE_USER_BEHAVIORS[index % len(FINISHABLE_USER_BEHAVIORS)]
    if behavior_mode in {"unfinishable", "cannot_finish", "cannot-finish", "cant_finish", "cant-finish", "impossible", "phase2", "phase_2"}:
        return UNFINISHABLE_USER_BEHAVIORS[index % len(UNFINISHABLE_USER_BEHAVIORS)]
    if behavior_mode in USER_BEHAVIORS:
        return behavior_mode
    raise ValueError(
        f"Unknown behavior_mode={behavior_mode!r}. Use cooperative, mixed, finishable, "
        f"unfinishable, or one of {USER_BEHAVIORS}."
    )


def _select_residency_category(index: int, branch_mode: str | None) -> str:
    branch_mode = (branch_mode or os.environ.get("DIGITAL_ONBOARDING_BRANCH_MODE") or "mixed").strip().lower()
    if branch_mode in {"domestic", "us", "usa", "us_citizen", "citizen", "citizenship_usa"}:
        return "US_CITIZEN"
    if branch_mode in {"us_pr", "pr", "green_card", "permanent_resident", "us_permanent_resident"}:
        return "US_PERMANENT_RESIDENT"
    if branch_mode in {"us_visa", "visa", "non_pr", "non-pr", "non_permanent_resident", "work_visa"}:
        return "US_VISA"
    if branch_mode in {"foreigner", "foreign", "international", "non_us", "non-us", "non_us_resident", "non-us-resident"}:
        return "INTERNATIONAL"
    if branch_mode in {"legacy_mixed", "global_mixed"}:
        return "INTERNATIONAL" if index % 2 else "US_CITIZEN"
    if branch_mode in {"mixed", "all", "us_market", "us-only", "us_only", ""}:
        return ["US_CITIZEN", "US_PERMANENT_RESIDENT", "US_VISA"][index % 3]
    raise ValueError(
        "Unknown branch_mode={!r}. Use mixed/us_market, domestic, us_pr, us_visa, or foreigner.".format(branch_mode)
    )


def _select_branch(index: int, branch_mode: str | None) -> str:
    return "FOREIGNER" if _select_residency_category(index, branch_mode) == "INTERNATIONAL" else "DOMESTIC"


def make_scenario(
    index: int,
    split: str = "train",
    seed: int = 0,
    behavior_mode: str = "mixed",
    branch_mode: str | None = None,
) -> dict[str, Any]:
    rng = random.Random(seed + index)
    residency_category = _select_residency_category(index, branch_mode)
    branch = "FOREIGNER" if residency_category == "INTERNATIONAL" else "DOMESTIC"
    if residency_category == "INTERNATIONAL":
        profile = _foreigner_profile(index)
        required = copy.deepcopy(FOREIGNER_REQUIRED_FIELDS)
    elif residency_category == "US_PERMANENT_RESIDENT":
        profile = _us_resident_noncitizen_profile(index, permanent_resident=True)
        required = copy.deepcopy(US_PERMANENT_RESIDENT_REQUIRED_FIELDS)
    elif residency_category == "US_VISA":
        profile = _us_resident_noncitizen_profile(index, permanent_resident=False)
        required = copy.deepcopy(US_VISA_REQUIRED_FIELDS)
    else:
        profile = _profile(index)
        profile["residency_category"] = "US_CITIZEN"
        profile["permanent_resident"] = False
        required = copy.deepcopy(US_CITIZEN_REQUIRED_FIELDS)
    if branch == "FOREIGNER" and profile.get("citizenship_country") == "CHN":
        required = required[:]
        required.insert(required.index("home_address"), "card_photo")
    if profile.get("funding_source") == "Other" and profile.get("other_source") and "other_source" not in required:
        required = required[:]
        required.append("other_source")
    behavior = _select_behavior(index, behavior_mode)
    phase1_mode = (behavior_mode or "").strip().lower() in {"phase1", "phase_1"}

    if behavior == "forgot_mobile_use_email":
        profile["contact_type"] = "MOBILE"
        profile["contact"] = profile["mobile"]
        profile["available_auth_methods"] = ["EMAIL"]
    elif behavior == "forgot_email_use_mobile":
        profile["contact_type"] = "EMAIL"
        profile["contact"] = profile["email"]
        profile["available_auth_methods"] = ["MOBILE"]
    elif behavior == "no_auth_contact":
        profile["available_auth_methods"] = []
    elif behavior == "mobile_required_user_will_return":
        profile["contact_type"] = "MOBILE"
        profile["contact"] = profile["mobile"]
        profile["available_auth_methods"] = ["MOBILE"]
        profile["required_auth_methods"] = ["MOBILE"]
        profile["mobile_temporarily_unavailable"] = True
    elif behavior == "passport_only" and branch == "DOMESTIC" and residency_category != "US_CITIZEN":
        behavior = "cooperative"
        profile["user_behavior"] = behavior

    initial_collected = {}

    if not phase1_mode and rng.random() < 0.35:
        for field in rng.sample(required, k=rng.randint(2, 5)):
            if field not in {"drivers_license", "passport_photo", "address_proof", "card_photo", "agreements_accepted"} and field in profile:
                initial_collected[field] = copy.deepcopy(profile[field])

    domestic_utterances = [
        "Hi, I want to open a brokerage account.",
        "Can you help me finish account opening?",
        "I need to start a Light Horse investing account.",
        "I'd like to complete KYC for a new account.",
    ]
    us_noncitizen_utterances = [
        "Hi, I live in the US and want to open a Light Horse brokerage account.",
        "Can you help me open an account as a US resident?",
        "I'd like to complete KYC for a new account. I live in the United States.",
        "I live in the US and want help finishing account opening.",
    ]
    foreigner_utterances = [
        "Hi, I live outside the US and want to open a Light Horse brokerage account.",
        "Can you help me open a non-US resident brokerage account?",
        "I'd like to complete KYC as an international customer.",
        "I don't have a US SSN, but I want to open an account.",
    ]
    if residency_category == "INTERNATIONAL":
        initial_user_utterance = rng.choice(foreigner_utterances)
    elif residency_category in {"US_PERMANENT_RESIDENT", "US_VISA"}:
        initial_user_utterance = rng.choice(us_noncitizen_utterances)
    else:
        initial_user_utterance = rng.choice(domestic_utterances)

    return {
        "scenario_id": f"{split}_{index:06d}",
        "split": split,
        "branch": branch,
        "residency_category": residency_category,
        "initial_user_utterance": initial_user_utterance,
        "required_fields": required,
        "initial_collected": initial_collected,
        "profile": profile,
        "user_behavior": behavior,
        "goal": (
            "gracefully_stop_if_authentication_is_impossible"
            if behavior == "no_auth_contact"
            else "gracefully_pause_until_required_mobile_is_available"
            if behavior == "mobile_required_user_will_return"
            else "gracefully_stop_if_required_driver_license_unavailable"
            if behavior == "passport_only" and residency_category == "US_CITIZEN"
            else "authenticate_collect_required_kyc_and_submit_after_confirmation"
        ),
    }


def make_scenarios(
    count: int,
    split: str,
    seed: int = 0,
    behavior_mode: str = "mixed",
    branch_mode: str | None = None,
) -> list[dict[str, Any]]:
    return [make_scenario(i, split=split, seed=seed, behavior_mode=behavior_mode, branch_mode=branch_mode) for i in range(count)]
