# Copyright 2026 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import os
from typing import Any, Optional

import pytest
from omegaconf import OmegaConf

from verl.experimental.agent_loop.agent_loop import DictConfigWrap
from verl.experimental.agent_loop.tool_agent_loop import AgentData, AgentState, ToolAgentLoop
from verl.utils.dataset.rl_dataset import RLHFDataset
from verl.workers.rollout.replica import TokenOutput


class _CapturingServerManager:
    def __init__(self):
        self.last_sampling_params: dict[str, Any] | None = None

    async def generate(
        self,
        request_id: str,
        *,
        prompt_ids: list[int],
        sampling_params: dict[str, Any],
        image_data: Optional[list[Any]] = None,
        video_data: Optional[list[Any]] = None,
    ) -> TokenOutput:
        del request_id, prompt_ids, image_data, video_data
        self.last_sampling_params = dict(sampling_params)
        token_count = int(sampling_params["max_tokens"])
        return TokenOutput(token_ids=list(range(100, 100 + token_count)), log_probs=[0.0] * token_count)


class _EmptyLengthServerManager:
    async def generate(
        self,
        request_id: str,
        *,
        prompt_ids: list[int],
        sampling_params: dict[str, Any],
        image_data: Optional[list[Any]] = None,
        video_data: Optional[list[Any]] = None,
    ) -> TokenOutput:
        del request_id, prompt_ids, sampling_params, image_data, video_data
        return TokenOutput(token_ids=[], stop_reason="length")


class _TokenCountingTokenizer:
    padding_side = "right"

    def apply_chat_template(
        self,
        messages: list[dict[str, Any]],
        *,
        add_generation_prompt: bool = True,
        tokenize: bool = True,
        **kwargs,
    ) -> list[int]:
        del tokenize, kwargs
        token_count = 0
        for message in messages:
            content = str(message.get("content") or "")
            token_count += len(content.split())
        if add_generation_prompt:
            token_count += 1
        return list(range(token_count))

    def decode(self, ids: list[int], skip_special_tokens: bool = True) -> str:
        del skip_special_tokens
        return " ".join(str(token_id) for token_id in ids)


class _OneTokenUserInteraction:
    async def generate_response(
        self, instance_id: str, messages: list[dict[str, Any]], **kwargs
    ) -> tuple[bool, str, float, dict]:
        del instance_id, messages, kwargs
        return False, "user", 0.0, {"reason": "unit_test"}


def _make_tool_loop(prompt_length: int = 5, response_length: int = 5, model_path: str | None = None) -> ToolAgentLoop:
    config = OmegaConf.create(
        {
            "actor_rollout_ref": {
                "rollout": {
                    "prompt_length": prompt_length,
                    "response_length": response_length,
                    "multi_turn": {
                        "max_user_turns": 0,
                        "max_assistant_turns": 0,
                        "max_parallel_calls": 1,
                        "max_tool_response_length": 256,
                        "tool_response_truncate_side": "middle",
                        "tool_config_path": None,
                        "interaction_config_path": None,
                        "format": "hermes",
                    },
                },
                "model": {"path": model_path} if model_path is not None else {},
            },
            "data": {
                "apply_chat_template_kwargs": {},
                "tool_config_path": None,
            },
        }
    )
    return ToolAgentLoop(
        trainer_config=DictConfigWrap(config),
        server_manager=_CapturingServerManager(),
        tokenizer=_TokenCountingTokenizer(),
        processor=None,
        dataset_cls=RLHFDataset,
        data_config=DictConfigWrap(config.data),
    )


def test_tool_agent_uses_model_context_limit_from_config_on_cpu(tmp_path):
    (tmp_path / "config.json").write_text('{"max_position_embeddings": 8}', encoding="utf-8")

    loop = _make_tool_loop(prompt_length=5, response_length=10, model_path=os.fspath(tmp_path))

    assert loop.sequence_length_limit == 8


def test_tool_agent_uses_omni_text_context_not_code2wav_context_on_cpu(tmp_path):
    (tmp_path / "config.json").write_text(
        """
        {
          "code2wav_config": {"max_position_embeddings": 8000},
          "thinker_config": {
            "text_config": {"max_position_embeddings": 65536}
          }
        }
        """,
        encoding="utf-8",
    )

    loop = _make_tool_loop(prompt_length=4352, response_length=40000, model_path=os.fspath(tmp_path))

    assert loop.sequence_length_limit == 44352


@pytest.mark.asyncio
async def test_tool_agent_caps_assistant_generation_to_remaining_sequence_budget_on_cpu():
    loop = _make_tool_loop(prompt_length=5, response_length=5)
    agent_data = AgentData(
        messages=[],
        image_data=None,
        video_data=None,
        metrics={},
        request_id="request-1",
        tools_kwargs={},
    )
    agent_data.prompt_ids = list(range(8))
    agent_data.response_mask = [1, 1]

    state = await loop._handle_generating_state(agent_data, {"temperature": 1.0, "max_tokens": 99})

    assert state == AgentState.TERMINATED
    assert loop.server_manager.last_sampling_params["max_tokens"] == 2
    assert agent_data.extra_fields["termination_reason"] == "generation_budget_exhausted_after_assistant_generation"


@pytest.mark.asyncio
async def test_tool_agent_terminates_on_empty_length_stop_from_rollout_server_on_cpu():
    loop = _make_tool_loop(prompt_length=5, response_length=5)
    loop.server_manager = _EmptyLengthServerManager()
    agent_data = AgentData(
        messages=[],
        image_data=None,
        video_data=None,
        metrics={},
        request_id="request-1",
        tools_kwargs={},
    )
    agent_data.prompt_ids = list(range(9))
    agent_data.response_mask = [1]

    state = await loop._handle_generating_state(agent_data, {"temperature": 1.0})

    assert state == AgentState.TERMINATED
    assert agent_data.extra_fields["termination_reason"] == "generation_budget_exhausted_after_assistant_generation"


@pytest.mark.asyncio
async def test_tool_agent_terminates_before_user_append_would_leave_zero_generation_budget_on_cpu():
    loop = _make_tool_loop(prompt_length=5, response_length=5)

    async def two_token_chat_template(*args, **kwargs) -> list[int]:
        del args, kwargs
        return [1, 2]

    loop.apply_chat_template = two_token_chat_template
    agent_data = AgentData(
        messages=[],
        image_data=None,
        video_data=None,
        metrics={},
        request_id="request-1",
        tools_kwargs={},
        interaction=_OneTokenUserInteraction(),
    )
    agent_data.prompt_ids = list(range(8))
    agent_data.response_mask = [1, 1]

    state = await loop._handle_interacting_state(agent_data)

    assert state == AgentState.TERMINATED
    assert len(agent_data.prompt_ids) == 8
    assert len(agent_data.response_mask) == 2
    assert agent_data.extra_fields["termination_reason"] == "generation_budget_exhausted_before_interaction_append"
