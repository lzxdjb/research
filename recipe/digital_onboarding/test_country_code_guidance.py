from recipe.digital_onboarding.country_code_guidance import append_citizenship_country_code_guidance


def test_append_citizenship_country_code_guidance() -> None:
    text = "Could you please provide your citizenship country?"

    guided = append_citizenship_country_code_guidance(text)

    assert "3-letter country code" in guided
    assert "China -> CHN" in guided
    assert "https://www.iban.com/country-codes" in guided


def test_append_citizenship_country_code_guidance_is_idempotent() -> None:
    text = append_citizenship_country_code_guidance("What is your citizenship?")

    assert append_citizenship_country_code_guidance(text) == text


def test_append_citizenship_country_code_guidance_ignores_other_fields() -> None:
    text = "Could you please provide your birth country?"

    assert append_citizenship_country_code_guidance(text) == text
