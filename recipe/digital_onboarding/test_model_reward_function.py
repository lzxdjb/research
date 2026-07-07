from recipe.digital_onboarding.model_reward_function import _bank_only_reward_result
from recipe.digital_onboarding.real_bank import bank_rule_score_from_tool_results


def test_bank_only_reward_returns_zero_for_stale_terminal_bank_state():
    result = _bank_only_reward_result(
        {
            "available": True,
            "bank_status": "AUDITING",
            "bank_completion_percentage": 100,
            "bank_missing_count": 0,
            "bank_submit_success": False,
        }
    )

    assert result["score"] == 0.0
    assert result["bank_submission_success"] == 0.0


def test_bank_only_reward_returns_one_for_current_submit_success():
    result = _bank_only_reward_result(
        {
            "available": True,
            "bank_status": "AUDITING",
            "bank_completion_percentage": 100,
            "bank_missing_count": 0,
            "bank_submit_success": True,
        }
    )

    assert result["score"] == 1.0
    assert result["bank_submission_success"] == 1.0


def test_bank_only_reward_returns_zero_for_unfinished_bank_state():
    result = _bank_only_reward_result(
        {
            "available": True,
            "bank_status": "COLLECTING",
            "bank_completion_percentage": 50,
            "bank_missing_count": 10,
            "bank_submit_success": False,
        }
    )

    assert result["score"] == 0.0
    assert result["bank_completion_ratio"] == 0.5


def test_bank_only_procedure_reward_uses_completion_percentage(monkeypatch):
    monkeypatch.setenv("DIGITAL_ONBOARDING_PROCEDURE_REWARD_ENABLED", "1")
    monkeypatch.setenv("DIGITAL_ONBOARDING_PROCEDURE_REWARD_WEIGHT", "0.15")
    monkeypatch.setenv("DIGITAL_ONBOARDING_FINAL_SUBMIT_REWARD", "1.0")

    result = _bank_only_reward_result(
        {
            "available": True,
            "bank_status": "COLLECTING",
            "bank_completion_percentage": 30,
            "bank_missing_count": 10,
            "bank_submit_success": False,
        }
    )

    assert result["reward_backend"] == "bank_rule_procedure"
    assert result["bank_completion_ratio"] == 0.3
    assert result["bank_procedure_reward"] == 0.045
    assert result["bank_final_reward"] == 0.0
    assert result["bank_submission_success"] == 0.0
    assert result["score"] == 0.045


def test_bank_only_procedure_reward_adds_final_submit_reward(monkeypatch):
    monkeypatch.setenv("DIGITAL_ONBOARDING_PROCEDURE_REWARD_ENABLED", "1")
    monkeypatch.setenv("DIGITAL_ONBOARDING_PROCEDURE_REWARD_WEIGHT", "0.15")
    monkeypatch.setenv("DIGITAL_ONBOARDING_FINAL_SUBMIT_REWARD", "1.0")

    result = _bank_only_reward_result(
        {
            "available": True,
            "bank_status": "AUDITING",
            "bank_completion_percentage": 100,
            "bank_missing_count": 0,
            "bank_submit_success": True,
        }
    )

    assert result["bank_completion_ratio"] == 1.0
    assert result["bank_procedure_reward"] == 0.15
    assert result["bank_final_reward"] == 1.0
    assert result["bank_submission_success"] == 1.0
    assert result["score"] == 1.15


def test_bank_rule_score_requires_successful_submit_application():
    result = bank_rule_score_from_tool_results(
        [
            {
                "tool": "query_progress",
                "status": "success",
                "state": {
                    "backend": "real_bank",
                    "authenticated": True,
                    "bank_query_ok": True,
                    "bank_status": "AUDITING",
                    "bank_missing_fields": [],
                    "bank_completion_percentage": 100,
                    "bank_submit_success": False,
                    "submitted": True,
                },
            }
        ]
    )

    assert result["score"] == 0.0
    assert result["bank_submit_success"] is False
    assert result["bank_stale_terminal_status"] is True


def test_bank_rule_score_accepts_only_current_submit_success():
    result = bank_rule_score_from_tool_results(
        [
            {
                "tool": "submit_application",
                "status": "success",
                "bank_submit_success": True,
                "state": {
                    "backend": "real_bank",
                    "authenticated": True,
                    "bank_query_ok": True,
                    "bank_status": "AUDITING",
                    "bank_missing_fields": [],
                    "bank_completion_percentage": 100,
                },
            }
        ]
    )

    assert result["score"] == 1.0
    assert result["bank_submit_success"] is True
    assert result["bank_submit_attempted"] is True
