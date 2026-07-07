import asyncio
import json
from typing import Any

from recipe.digital_onboarding.interactions import RuleBasedOnboardingUserInteraction
from recipe.digital_onboarding.real_bank import normalize_collect_payload
from recipe.digital_onboarding.scenario import make_scenarios
from recipe.digital_onboarding.tools import OnboardingTool, assistant_requests_document_upload
from verl.tools.schemas import OpenAIFunctionToolSchema


class DummyAgentData:
    def __init__(self, scenario: dict[str, Any]):
        self.request_id = "test_branch_flow"
        scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
        self.tools_kwargs = {"__onboarding_scenario_json__": scenario_json}
        self.extra_fields: dict[str, Any] = {}
        self.messages = [
            {"role": "user", "content": scenario["initial_user_utterance"]},
            {"role": "user", "content": "Yes, I confirm. Please submit."},
        ]


def _schema(name: str) -> OpenAIFunctionToolSchema:
    return OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    )


async def _call(agent_data: DummyAgentData, name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    tool = OnboardingTool(config={"type": "native"}, tool_schema=_schema(name))
    instance_id, _ = await tool.create()
    try:
        _response, _reward, result = await tool.execute(instance_id, parameters, agent_data=agent_data)
        return result
    finally:
        await tool.release(instance_id)


def test_mixed_scenarios_cover_us_market_categories() -> None:
    scenarios = make_scenarios(6, split="test", seed=3, behavior_mode="cooperative", branch_mode="mixed")
    branches = {scenario["branch"] for scenario in scenarios}
    categories = {scenario["residency_category"] for scenario in scenarios}

    assert branches == {"DOMESTIC"}
    assert categories == {"US_CITIZEN", "US_PERMANENT_RESIDENT", "US_VISA"}
    for scenario in scenarios:
        profile = scenario["profile"]
        assert profile["area_code"] == "1"
        assert profile["home_address"]["country"] == "USA"

    citizen = next(s for s in scenarios if s["residency_category"] == "US_CITIZEN")
    assert "drivers_license" in citizen["required_fields"]
    assert set(citizen["profile"]["drivers_license"]) == {"front", "back"}
    assert citizen["profile"]["permanent_resident"] is False

    permanent_resident = next(s for s in scenarios if s["residency_category"] == "US_PERMANENT_RESIDENT")
    assert permanent_resident["profile"]["citizenship_country"] != "USA"
    assert permanent_resident["profile"]["permanent_resident"] is True
    assert "passport_photo" in permanent_resident["required_fields"]
    assert "card_photo" in permanent_resident["required_fields"]

    visa_holder = next(s for s in scenarios if s["residency_category"] == "US_VISA")
    assert visa_holder["profile"]["citizenship_country"] != "USA"
    assert visa_holder["profile"]["permanent_resident"] is False
    assert "passport_photo" in visa_holder["required_fields"]
    assert "visa" in visa_holder["required_fields"]
    assert "visa_type" in visa_holder["required_fields"]


def test_us_market_scenarios_cover_employment_and_funding_variants() -> None:
    scenarios = make_scenarios(60, split="test", seed=7, behavior_mode="cooperative", branch_mode="us_market")

    assert {scenario["profile"]["employment_status"] for scenario in scenarios} == {
        "EMPLOYED",
        "SELF_EMPLOYED",
        "UNEMPLOYED",
        "RETIRED",
        "STUDENT",
    }
    assert {scenario["profile"]["funding_source"] for scenario in scenarios} == {
        "Savings",
        "Inheritance",
        "Pension",
        "Rental Income",
        "Social Security",
        "Other",
    }
    other_source_scenarios = [scenario for scenario in scenarios if scenario["profile"]["funding_source"] == "Other"]
    assert other_source_scenarios
    assert all(scenario["profile"].get("other_source") for scenario in other_source_scenarios)
    assert all("other_source" in scenario["required_fields"] for scenario in other_source_scenarios)


def test_rule_user_upload_target_advances_required_document_bundles() -> None:
    sim = RuleBasedOnboardingUserInteraction({"name": "onboarding_user"})

    state: dict[str, Any] = {"uploaded_doc_types": set(), "uploaded_doc_fields": set()}
    profile = {"branch": "DOMESTIC", "residency_category": "US_CITIZEN"}
    assert sim._document_upload_target("please upload your driver's license", profile, state) == (
        "drivers_license",
        "drivers_license_front",
    )
    sim._remember_uploaded_document(state, "drivers_license", "drivers_license_front")
    assert sim._document_upload_target("please upload your driver's license", profile, state) == (
        "drivers_license",
        "drivers_license_back",
    )

    state = {"uploaded_doc_types": set(), "uploaded_doc_fields": set()}
    profile = {"branch": "DOMESTIC", "residency_category": "US_VISA"}
    assert sim._document_upload_target("please upload your passport and visa", profile, state) == (
        "passport_photo",
        "passport",
    )
    sim._remember_uploaded_document(state, "passport_photo", "passport")
    assert sim._document_upload_target("please upload your passport and visa", profile, state) == ("visa", "visa")

    state = {"uploaded_doc_types": set(), "uploaded_doc_fields": set()}
    profile = {"branch": "DOMESTIC", "residency_category": "US_PERMANENT_RESIDENT"}
    assert sim._document_upload_target("please upload your passport and permanent resident card", profile, state) == (
        "passport_photo",
        "passport",
    )
    sim._remember_uploaded_document(state, "passport_photo", "passport")
    assert sim._document_upload_target("please upload your passport and permanent resident card", profile, state) == (
        "card_photo",
        "permanent_resident_card",
    )


def test_rule_user_answers_field_confirmation_before_generic_submit() -> None:
    sim = RuleBasedOnboardingUserInteraction({"name": "onboarding_user"})
    profile = {"account_type": "CASH", "permanent_resident": False}
    state = {"scenario": {"user_behavior": "cooperative"}}

    account_type_answer = sim._answer("please confirm your account type: cash or margin", profile, state)
    permanent_resident_answer = sim._answer(
        "please confirm whether you are a permanent resident", profile, state
    )
    document_review_answer = sim._answer(
        "please review the extracted document fields and confirm whether the document details are correct",
        profile,
        state,
    )

    assert account_type_answer == "I want a CASH account."
    assert permanent_resident_answer == "For permanent resident status, no."
    assert document_review_answer == "Yes, the extracted document details are correct."
    assert "submit the application" not in document_review_answer.lower()


def test_extract_document_info_requires_plain_text_review_before_submit(monkeypatch) -> None:
    monkeypatch.setenv("DIGITAL_ONBOARDING_REQUIRE_UPLOADED_IMAGE", "0")
    scenario = make_scenarios(1, split="test", seed=11, behavior_mode="cooperative", branch_mode="us_citizen")[0]
    agent_data = DummyAgentData(scenario)

    async def run() -> None:
        capture = await _call(
            agent_data,
            "capture_document",
            {"doc_type": "drivers_license_front", "purpose": "upload"},
        )
        assert capture["status"] == "success"

        extract = await _call(
            agent_data,
            "extract_document_info",
            {"document_type": "DRIVERS_LICENSE"},
        )
        assert extract["status"] == "success"
        assert extract["review_required"] is True
        assert extract["next_action"] == "show_document_fields_to_user_for_review"
        assert extract["tool_call_allowed_next"] is False

        agent_data.extra_fields["onboarding_state"]["authenticated"] = True
        agent_data.messages = [
            {"role": "user", "content": "CAPTURE_RESULT: document uploaded. [[UPLOADED_IMAGE]]"}
        ]
        submit = await _call(
            agent_data,
            "submit_documents",
            {"documents": [{"file_type": "drivers_licence_front"}]},
        )
        assert submit["status"] == "error"
        assert submit["error_code"] == "document_review_pending"
        assert "Document review is pending" in submit["message"]

        state = agent_data.extra_fields["onboarding_state"]
        state["document_review_pending"] = False
        state["document_review_confirmed"] = True
        agent_data.messages = [{"role": "user", "content": "Yes, the document details are correct."}]
        home_address = await _call(
            agent_data,
            "submit_home_address",
            {"address_branch": "US", "home_address": scenario["profile"]["home_address"]},
        )
        assert home_address["status"] == "success"
        assert home_address.get("error_code") != "document_collect_scope"
        assert "home_address" in home_address["accepted_fields"]

    asyncio.run(run())


def test_upload_request_detector_covers_us_non_citizen_documents() -> None:
    assert assistant_requests_document_upload("Please upload your visa page now.")
    assert assistant_requests_document_upload("Please upload your green card now.")
    assert assistant_requests_document_upload("Please provide your permanent resident card.")


def test_foreigner_branch_remains_available_explicitly() -> None:
    scenarios = make_scenarios(4, split="test", seed=3, behavior_mode="cooperative", branch_mode="foreigner")
    branches = {scenario["branch"] for scenario in scenarios}

    assert branches == {"FOREIGNER"}
    foreigner = scenarios[0]
    assert "tax_id" in foreigner["required_fields"]
    assert "tax_id_country" in foreigner["required_fields"]
    assert "passport_photo" in foreigner["required_fields"]
    assert "address_proof" in foreigner["required_fields"]
    assert foreigner["profile"]["account_type"] == "CASH"


def test_normalize_collect_payload_maps_foreign_tax_and_documents() -> None:
    payload = normalize_collect_payload(
        {
            "tax_id": "CN-TAX-000001",
            "tax_id_country": "CHN",
            "documents": [
                {
                    "file_type": "passport",
                    "file_id": "file_passport",
                    "min_file_id": "min_passport",
                    "passport_number": "P12345678",
                    "expiration_date": "2032-12-31",
                },
                {
                    "file_type": "bank_statement",
                    "file_id": "file_address",
                    "min_file_id": "min_address",
                },
            ],
        }
    )

    assert payload["weight_form"] == {"tax_id": "CN-TAX-000001", "tax_id_country": "CHN"}
    assert payload["passport_photo"] == {"file_id": "file_passport", "min_file_id": "min_passport"}
    assert payload["address_proof"] == {"file_id": "file_address", "min_file_id": "min_address"}
    assert payload["passport_no"] == "P12345678"
    assert payload["passport_expire_date"] == "2032-12-31"


def test_normalize_collect_payload_keeps_driver_license_sides_separate() -> None:
    payload = normalize_collect_payload(
        {
            "documents": [
                {
                    "file_type": "drivers_licence_front",
                    "file_id": "file_front",
                    "min_file_id": "min_front",
                    "expiration_date": "2030-12-31",
                },
                {
                    "file_type": "drivers_licence_back",
                    "file_id": "file_back",
                    "min_file_id": "min_back",
                },
            ]
        }
    )

    assert payload["drivers_license"]["front"]["file_id"] == "file_front"
    assert payload["drivers_license"]["front"]["expire_date"] == "2030-12-31"
    assert payload["drivers_license"]["back"]["file_id"] == "file_back"


def test_present_and_submit_production_tools_are_supported() -> None:
    scenario = make_scenarios(2, split="test", seed=5, behavior_mode="cooperative", branch_mode="foreigner")[1]
    agent_data = DummyAgentData(scenario)
    profile = scenario["profile"]

    async def run() -> None:
        widget = await _call(agent_data, "present_tax_id_input", {"question": "Tax ID", "default_country": profile["tax_id_country"]})
        assert widget["status"] == "success"

        login_send = await _call(
            agent_data,
            "send_verification_code",
            {
                "contact": profile["contact"],
                "contact_type": profile["contact_type"],
                "area_code": profile["area_code"],
            },
        )
        assert login_send["status"] == "success"
        login = await _call(
            agent_data,
            "login_and_get_token",
            {
                "contact": profile["contact"],
                "contact_type": profile["contact_type"],
                "area_code": profile["area_code"],
                "verification_code": profile["verification_code"],
            },
        )
        assert login["status"] == "success"

        submit = await _call(
            agent_data,
            "submit_personal_identity",
            {
                "given_name": profile["given_name"],
                "family_name": profile["family_name"],
                "date_of_birth": profile["date_of_birth"],
                "gender": profile["gender"],
                "marital_status": profile["marital_status"],
                "num_dependents": profile["num_dependents"],
                "tax_id": profile["tax_id"],
                "tax_id_country": profile["tax_id_country"],
            },
        )
        assert submit["status"] == "success"
        assert "tax_id" in submit["accepted_fields"]
        assert "tax_id_country" in submit["accepted_fields"]

    asyncio.run(run())
