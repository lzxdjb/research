#!/usr/bin/env python3
"""Smoke test the digital-onboarding real-bank tool backend."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from recipe.digital_onboarding import real_bank
from recipe.digital_onboarding.real_bank import prepare_real_bank_scenario
from recipe.digital_onboarding.scenario import make_scenario
from recipe.digital_onboarding.tools import (
    OnboardingTool,
    register_verified_upload,
)
from verl.tools.schemas import OpenAIFunctionToolSchema


class DummyAgentData:
    def __init__(self, scenario: dict[str, Any], request_id: str):
        scenario_json = json.dumps(scenario, ensure_ascii=False, sort_keys=True)
        self.request_id = request_id
        self.tools_kwargs = {
            "__onboarding_scenario_json__": scenario_json,
            "__onboarding_tool_backend__": "real_bank",
        }
        self.extra_fields = {}
        self.messages = [
            {"role": "user", "content": scenario["initial_user_utterance"]},
            {"role": "user", "content": "Yes, I confirm the information is correct. Please submit."},
        ]


class FakeTradeAPI:
    instances: list["FakeTradeAPI"] = []
    sessions: dict[tuple[str, str], dict[str, Any]] = {}

    def __init__(self, environment: str = "test", session_id: str | None = None, session_dir: str | None = None, **kwargs):
        self.environment = environment
        self.session_id = session_id or "unknown"
        self.session_dir = session_dir or "/tmp"
        self.userId = None
        self.token = None
        self.access_token = None
        self.collected: dict[str, Any] = {}
        self.uploads: list[dict[str, Any]] = []
        self.token_calls = 0
        self.token_call_loaded_session = False
        self.required = {
            "account_type",
            "given_name",
            "family_name",
            "date_of_birth",
            "agreements_accepted",
        }
        saved = self.__class__.sessions.get((self.session_dir, self.session_id), {})
        self.userId = saved.get("userId")
        self.token = saved.get("token")
        self.access_token = saved.get("access_token")
        self.__class__.instances.append(self)

    def _session_path(self) -> str:
        return os.path.join(self.session_dir, f"{self.session_id}.json")

    def send_verification_code(self, contact: str, contact_type: str = "MOBILE", area_code: str = "1") -> dict:
        return {"i18nMsg": "success", "data": {"hideMobile": contact[-4:]}}

    def login(self, contact: str, verification_code: str, contact_type: str = "MOBILE", area_code: str = "1") -> dict:
        self.userId = f"user_{self.session_id}"
        self.token = f"token_{self.session_id}"
        self.__class__.sessions[(self.session_dir, self.session_id)] = {
            "userId": self.userId,
            "token": self.token,
            "access_token": self.access_token,
        }
        return {"i18nMsg": "success", "data": {"userId": self.userId, "token": self.token}}

    def get_trading_token(self) -> dict:
        self.token_calls += 1
        self.token_call_loaded_session = bool(self.userId and self.token)
        self.access_token = f"access_{self.session_id}"
        self.__class__.sessions[(self.session_dir, self.session_id)] = {
            "userId": self.userId,
            "token": self.token,
            "access_token": self.access_token,
        }
        return {"s": "ok", "d": {"access_token": self.access_token}}

    def query_progress(self) -> dict:
        collected = sorted(self.collected)
        missing = sorted(self.required - set(self.collected))
        status = "OPENED" if not missing and self.collected.get("__submitted__") else "COLLECTING"
        return {
            "s": "ok",
            "d": {
                "app_no": f"app_{self.session_id}",
                "status": status,
                "missing_fields": missing,
                "collected_fields": collected,
                "completion_percentage": int(100 * len(collected) / max(1, len(self.required))),
            },
        }

    def collect_information(self, data: dict[str, Any]) -> dict:
        self.collected.update(data)
        return {"s": "ok", "d": True}

    def submit_application(self) -> dict:
        missing = sorted(self.required - set(self.collected))
        if missing:
            return {"s": "missing_param", "errmsg": ",".join(missing)}
        self.collected["__submitted__"] = True
        return {"s": "ok", "d": True}

    def upload_file(self, file_path: str, is_need_min: bool = False, file_type: str | None = None) -> dict:
        self.uploads.append({"file_path": file_path, "is_need_min": is_need_min, "file_type": file_type})
        if file_type:
            self.collected[file_type] = True
            return {"s": "ok", "d": True}
        return {"s": "ok", "d": {"fileId": f"file_{self.session_id}", "minFileId": f"min_{self.session_id}"}}


class RateLimitedFakeTradeAPI(FakeTradeAPI):
    def send_verification_code(self, contact: str, contact_type: str = "MOBILE", area_code: str = "1") -> dict:
        return {"errorCode": "MAC_SEND_OVER_MAX", "i18nMsg": "too many verification codes"}

    def login(self, contact: str, verification_code: str, contact_type: str = "MOBILE", area_code: str = "1") -> dict:
        raise AssertionError("login should not hit the bank after MAC_SEND_OVER_MAX bypass")

    def get_trading_token(self) -> dict:
        raise AssertionError("token should not hit the bank after MAC_SEND_OVER_MAX bypass")

    def query_progress(self) -> dict:
        raise AssertionError("progress should use trajectory state after auth bypass")

    def collect_information(self, data: dict[str, Any]) -> dict:
        raise AssertionError("collect should use trajectory state after auth bypass")

    def submit_application(self) -> dict:
        raise AssertionError("submit should use trajectory state after auth bypass")


def _schema(name: str, properties: dict[str, Any] | None = None, required: list[str] | None = None) -> OpenAIFunctionToolSchema:
    return OpenAIFunctionToolSchema.model_validate(
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {
                    "type": "object",
                    "properties": properties or {},
                    "required": required or [],
                },
            },
        }
    )


def _set_env(name: str, value: str | None) -> str | None:
    previous = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return previous


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def _write_api_module(root: Path, *, constructor: str) -> None:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    if constructor == "session_id":
        init_signature = "self, environment='test', session_id=None, session_dir=None"
        init_body = "self.environment = environment; self.session_id = session_id; self.session_dir = session_dir"
    else:
        init_signature = "self, environment='test'"
        init_body = "self.environment = environment"
    (scripts / "api.py").write_text(
        f"""
import os


class TradeAPI:
    def __init__({init_signature}):
        {init_body}
        self.userId = None
        self.token = None
        self.access_token = None

    def login(self, contact, verification_code, contact_type='MOBILE', area_code='1'):
        self.userId = contact
        self.token = verification_code
        return {{'data': {{'userId': self.userId, 'token': self.token}}}}

    def get_trading_token(self):
        self.access_token = 'access'
        return {{'s': 'ok', 'd': {{'access_token': self.access_token}}}}
""",
        encoding="utf-8",
    )


def test_real_bank_api_source_switches_only_for_real_execution(tmp_path: Path) -> None:
    old_root = tmp_path / "open-account"
    new_root = tmp_path / "new-open-account"
    _write_api_module(old_root, constructor="session_id")
    _write_api_module(new_root, constructor="simple")

    previous_open = real_bank.OPEN_ACCOUNT_SCRIPTS
    previous_new = real_bank.NEW_OPEN_ACCOUNT_SCRIPTS
    previous_bypass = _set_env("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT", "1")
    previous_upload = _set_env("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER", "1")
    previous_auth = _set_env("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS", "1")
    previous_session = _set_env("DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT", str(tmp_path / "sessions"))
    previous_override = _set_env("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR", None)
    real_bank.get_trade_api_class.cache_clear()
    try:
        real_bank.OPEN_ACCOUNT_SCRIPTS = old_root / "scripts"
        real_bank.NEW_OPEN_ACCOUNT_SCRIPTS = new_root / "scripts"

        training_api = real_bank.make_trade_api("training-path")
        if getattr(training_api, "session_id", None) != "training-path":
            raise AssertionError("fake-wrapper training path should keep using open-account TradeAPI")

        os.environ["DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT"] = "0"
        os.environ["DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER"] = "0"
        os.environ["DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS"] = "1"
        production_api = real_bank.make_trade_api("production-path")
        if not hasattr(production_api, "_api"):
            raise AssertionError("real execution path should wrap the simple new-open-account TradeAPI")
        if hasattr(production_api._api, "session_id"):
            raise AssertionError("real execution path should not use open-account TradeAPI")
    finally:
        real_bank.OPEN_ACCOUNT_SCRIPTS = previous_open
        real_bank.NEW_OPEN_ACCOUNT_SCRIPTS = previous_new
        real_bank.get_trade_api_class.cache_clear()
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT", previous_bypass)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER", previous_upload)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS", previous_auth)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT", previous_session)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR", previous_override)


def test_real_execution_simple_api_keeps_per_trajectory_sessions(tmp_path: Path) -> None:
    new_root = tmp_path / "new-open-account"
    _write_api_module(new_root, constructor="simple")

    previous_new = real_bank.NEW_OPEN_ACCOUNT_SCRIPTS
    previous_bypass = _set_env("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT", "0")
    previous_upload = _set_env("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER", "0")
    previous_auth = _set_env("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS", "0")
    previous_session = _set_env("DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT", str(tmp_path / "sessions"))
    previous_override = _set_env("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR", None)
    real_bank.get_trade_api_class.cache_clear()
    try:
        real_bank.NEW_OPEN_ACCOUNT_SCRIPTS = new_root / "scripts"
        first = real_bank.make_trade_api("trajectory-a")
        second = real_bank.make_trade_api("trajectory-b")

        first.login("alice", "code-a")
        second.login("bob", "code-b")
        first_reload = real_bank.make_trade_api("trajectory-a")

        if first_reload.userId != "alice" or first_reload.token != "code-a":
            raise AssertionError("real execution wrapper should reload the matching trajectory session")
        if first._session_path() == second._session_path():
            raise AssertionError("real execution wrapper should not share session files across trajectories")
    finally:
        real_bank.NEW_OPEN_ACCOUNT_SCRIPTS = previous_new
        real_bank.get_trade_api_class.cache_clear()
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT", previous_bypass)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER", previous_upload)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS", previous_auth)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_SESSION_ROOT", previous_session)
        _restore_env("DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR", previous_override)


def test_query_progress_terminal_status_does_not_mark_submitted() -> None:
    tool = OnboardingTool(config={"type": "native", "backend": "real_bank"}, tool_schema=_schema("query_progress"))
    state = {
        "profile": {},
        "collected_fields": {},
        "submitted": False,
        "bank_submit_success": False,
    }

    tool._sync_bank_progress(
        state,
        {
            "s": "ok",
            "d": {
                "app_no": "stale_app",
                "status": "AUDITING",
                "missing_fields": [],
                "collected_fields": [],
                "completion_percentage": 100,
            },
        },
    )

    assert state["bank_status"] == "AUDITING"
    assert state["bank_query_ok"] is True
    assert state["submitted"] is False
    assert state["bank_submit_success"] is False


async def _call(agent_data: DummyAgentData, name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    tool = OnboardingTool(config={"type": "native", "backend": "real_bank"}, tool_schema=_schema(name))
    instance_id, _ = await tool.create()
    response, reward, result = await tool.execute(instance_id, parameters, agent_data=agent_data)
    await tool.release(instance_id)
    print(json.dumps({"tool": name, "reward": reward, "response": response.text, "result": result}, ensure_ascii=False, indent=2))
    return result


async def _assert_upload_modes(scenario: dict[str, Any], profile: dict[str, Any]) -> None:
    agent_data = DummyAgentData(scenario, request_id="smoke_upload_modes_0001")
    real_upload_request = {
        "verified": True,
        "verification_id": "upload_test_1",
        "filename": "drivers_license_front.png",
        "stored_path": __file__,
        "file_url": "verified-upload://upload_test_1/drivers_license_front.png",
        "file_id": "verified_file",
        "min_file_id": "verified_min",
        "doc_type": "drivers_license_front",
    }

    previous = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER")
    try:
        os.environ["DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER"] = "1"
        fake_capture = await _call(
            agent_data,
            "capture_document",
            {"doc_type": "drivers_license_front"},
        )
        if fake_capture.get("message") != "Please upload the requested document image before I continue.":
            raise AssertionError("fake upload wrapper should keep the local upload prompt")
        register_verified_upload(agent_data, real_upload_request)
        fake_upload = await _call(
            agent_data,
            "upload_file",
            {"filename": "drivers_license_front.png", "file_data": "", "is_need_min": True, "doc_type": "drivers_license_front"},
        )
        if fake_upload.get("bank_response", {}).get("d", {}).get("fileId") != "verified_file":
            raise AssertionError("fake upload wrapper should use local verified upload ids")

        os.environ["DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER"] = "0"
        real_capture = await _call(
            agent_data,
            "capture_document",
            {"doc_type": "drivers_license_front"},
        )
        if real_capture.get("message") != "CAPTURE_REQUESTED":
            raise AssertionError("real upload mode should request the frontend/bank upload flow")
        agent_data.extra_fields["onboarding_state"]["bank_auth_bypass"] = True
        auth_bypass_capture = await _call(
            agent_data,
            "capture_document",
            {"doc_type": "drivers_license_front"},
        )
        if auth_bypass_capture.get("message") != "CAPTURE_REQUESTED":
            raise AssertionError("bank auth bypass should not force the fake upload wrapper")
        register_verified_upload(agent_data, {**real_upload_request, "verification_id": "upload_test_2"})
        real_upload = await _call(
            agent_data,
            "upload_file",
            {"filename": "drivers_license_front.png", "file_data": "", "is_need_min": True, "doc_type": "drivers_license_front"},
        )
        if real_upload.get("status") != "success":
            raise AssertionError("real upload mode should upload to the bank successfully")
        if real_upload.get("bank_response", {}).get("d") is not True:
            raise AssertionError("real upload mode should expose the bank file/collect response")
        if not real_upload.get("state", {}).get("document_upload_verified"):
            raise AssertionError("real upload mode should mark the document upload verified")
    finally:
        if previous is None:
            os.environ.pop("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER", None)
        else:
            os.environ["DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER"] = previous


async def _run(live: bool, rate_limit: bool = False) -> int:
    request_id = "smoke_request_0001"
    scenario = prepare_real_bank_scenario(
        make_scenario(0, split="smoke", seed=7, behavior_mode="cooperative"),
        request_id=request_id,
    )
    if rate_limit:
        scenario["required_fields"] = [
            "account_type",
            "given_name",
            "family_name",
            "date_of_birth",
            "agreements_accepted",
        ]
    agent_data = DummyAgentData(scenario, request_id=request_id)

    patcher = None
    if not live:
        fake_api_cls = RateLimitedFakeTradeAPI if rate_limit else FakeTradeAPI
        fake_api_cls.instances = []
        fake_api_cls.sessions = {}
        patcher = patch("recipe.digital_onboarding.real_bank.get_trade_api_class", return_value=fake_api_cls)
        patcher.start()

    try:
        previous_upload = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER")
        previous_auth = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS")
        previous_rate = os.environ.get("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT")
        if not rate_limit:
            os.environ["DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER"] = "0"
            os.environ["DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS"] = "0"
            os.environ["DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT"] = "0"
        profile = scenario["profile"]
        await _call(
            agent_data,
            "send_verification_code",
            {"contact": profile["mobile"], "contact_type": "MOBILE", "area_code": profile["area_code"]},
        )
        if rate_limit:
            repeat_send = await _call(
                agent_data,
                "send_verification_code",
                {"contact": profile["mobile"], "contact_type": "MOBILE", "area_code": profile["area_code"]},
            )
            if repeat_send.get("status") != "success" or not repeat_send.get("state", {}).get("verification_sent"):
                raise AssertionError("repeated send should be idempotent after rate-limit bypass")
        pickle.dumps(agent_data.extra_fields)
        await _call(
            agent_data,
            "login_and_get_token",
            {
                "contact": profile["mobile"],
                "contact_type": "MOBILE",
                "area_code": profile["area_code"],
                "verification_code": profile["verification_code"],
            },
        )
        login_state = agent_data.extra_fields.get("onboarding_state", {})
        if not rate_limit and not login_state.get("bank_real_authenticated"):
            raise AssertionError("strict real login should initialize trading token/session state")
        if not live and not rate_limit:
            if len(FakeTradeAPI.instances) < 2:
                raise AssertionError("strict real token initialization should reload the saved login session")
            token_api = FakeTradeAPI.instances[-1]
            if token_api.token_calls != 1 or not token_api.token_call_loaded_session:
                raise AssertionError("strict real token initialization should mirror `test_api.py token` after login")
        await _call(agent_data, "query_progress", {})
        await _assert_upload_modes(scenario, profile)
        pickle.dumps(agent_data.extra_fields)
        await _call(
            agent_data,
            "collect_information",
            {
                "data": {
                    "account_type": profile["account_type"],
                    "given_name": profile["gvie_name"],
                    "family_name": profile["family_name"],
                    "date_of_birth": profile["date_of_birth"],
                    "agreements_accepted": True,
                }
            },
        )
        submit = await _call(agent_data, "submit_application", {})
    finally:
        if "previous_upload" in locals():
            _restore_env("DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER", previous_upload)
            _restore_env("DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS", previous_auth)
            _restore_env("DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT", previous_rate)
        if patcher:
            patcher.stop()

    state = submit.get("state", {})
    ok = state.get("backend") == "real_bank" and state.get("bank_submit_success")
    print(json.dumps({"ok": ok, "final_state": state}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Call the real open-account backend instead of mocked API.")
    parser.add_argument("--rate-limit", action="store_true", help="Mock MAC_SEND_OVER_MAX and verify test-mode auth bypass.")
    args = parser.parse_args()
    return asyncio.run(_run(args.live, rate_limit=args.rate_limit))


if __name__ == "__main__":
    raise SystemExit(main())
