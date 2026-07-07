"""Turn-by-turn dual-agent rollout manager.

The manager owns two standard verl rollout-server managers, but it does not use
their single-agent AgentLoop workers.  Instead, it creates lightweight dual
workers that call Agent A and Agent B servers alternately and returns two
DataProto batches: one training view for each actor.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import numpy as np
import ray
import torch
from omegaconf import DictConfig
from tensordict import TensorDict

from verl.experimental.agent_loop.agent_loop import (
    AgentLoopManager,
    AsyncLLMServerManager,
    GlobalRequestLoadBalancer,
)
from verl.protocol import DataProto
from verl.single_controller.ray import RayResourcePool, RayWorkerGroup
from verl.utils.chat_template import apply_chat_template
from verl.utils.config import omega_conf_to_dataclass
from verl.utils.model import compute_position_id_with_mask
from verl.utils.ray_utils import auto_await, get_event_loop
from verl.utils.tokenizer import normalize_token_ids
from verl.workers.config import HFModelConfig, RolloutConfig
from .utils import ACTOR_A_KEY, ACTOR_B_KEY, actor_config_view, messages_to_text


@dataclass
class DualAgentRolloutBatch:
    actor_a: DataProto
    actor_b: DataProto
    timing: dict[str, float]


@dataclass
class _View:
    prompt_ids: list[int]
    response_ids: list[int]
    response_mask: list[int]
    transcript: str


class _Runtime:
    def __init__(
        self,
        config: DictConfig,
        actor_key: str,
        servers: list[tuple[str, ray.actor.ActorHandle]],
        load_balancer_handle: ray.actor.ActorHandle,
    ):
        self.config = actor_config_view(config, actor_key)
        self.rollout_config: RolloutConfig = omega_conf_to_dataclass(self.config.actor_rollout_ref.rollout)
        self.model_config: HFModelConfig = omega_conf_to_dataclass(self.config.actor_rollout_ref.model)
        self.server_manager = AsyncLLMServerManager(self.config, servers, load_balancer_handle)
        self.tokenizer = self.model_config.tokenizer
        self.loop = get_event_loop()
        self.apply_chat_template_kwargs = self.config.data.get("apply_chat_template_kwargs", {})

    async def encode(self, messages: list[dict[str, Any]], *, add_generation_prompt: bool = True) -> list[int]:
        tokenized = await self.loop.run_in_executor(
            None,
            lambda: apply_chat_template(
                self.tokenizer,
                messages,
                add_generation_prompt=add_generation_prompt,
                tokenize=True,
                **self.apply_chat_template_kwargs,
            ),
        )
        return normalize_token_ids(tokenized)

    async def generate(self, request_id: str, messages: list[dict[str, Any]], sampling_params: dict[str, Any]):
        prompt_ids = await self.encode(messages, add_generation_prompt=True)
        output = await self.server_manager.generate(
            request_id=request_id,
            prompt_ids=prompt_ids,
            sampling_params=sampling_params,
        )
        text = self.tokenizer.decode(output.token_ids, skip_special_tokens=True).strip()
        return prompt_ids, output.token_ids, text


@ray.remote
class DualAgentRolloutWorker:
    def __init__(
        self,
        config: DictConfig,
        actor_a_servers: list[tuple[str, ray.actor.ActorHandle]],
        actor_a_load_balancer: ray.actor.ActorHandle,
        actor_b_servers: list[tuple[str, ray.actor.ActorHandle]],
        actor_b_load_balancer: ray.actor.ActorHandle,
    ):
        self.config = config
        self.dual_config = config.dual_agent
        self.actor_a = _Runtime(config, ACTOR_A_KEY, actor_a_servers, actor_a_load_balancer)
        self.actor_b = _Runtime(config, ACTOR_B_KEY, actor_b_servers, actor_b_load_balancer)
        self.max_turns = int(self.dual_config.rollout.max_turns)
        self.stop_strings = list(self.dual_config.rollout.get("stop_strings", []))
        self.actor_b_system_prompt = str(self.dual_config.actor_b.get("system_prompt", "You are Agent B."))

    async def generate_sequences(self, batch: DataProto) -> dict[str, DataProto]:
        sampling_a = self._sampling_params(self.actor_a.rollout_config)
        sampling_b = self._sampling_params(self.actor_b.rollout_config)

        tasks = []
        for i in range(len(batch)):
            kwargs = {key: value[i] for key, value in batch.non_tensor_batch.items()}
            tasks.append(self._run_one(kwargs, sampling_a, sampling_b))
        results = await asyncio.gather(*tasks)

        actor_a_views = [item[0] for item in results]
        actor_b_views = [item[1] for item in results]
        return {
            "actor_a": self._views_to_dataproto("actor_a", actor_a_views, self.actor_a),
            "actor_b": self._views_to_dataproto("actor_b", actor_b_views, self.actor_b),
        }

    def _sampling_params(self, rollout_config: RolloutConfig) -> dict[str, Any]:
        return {
            "temperature": rollout_config.temperature,
            "top_p": rollout_config.top_p,
            "top_k": rollout_config.top_k,
            "repetition_penalty": rollout_config.repetition_penalty,
            "logprobs": False,
            "max_tokens": int(self.dual_config.rollout.max_tokens_per_turn),
        }

    def _initial_actor_b_messages(self, raw_prompt: list[dict[str, Any]], kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        if "actor_b_raw_prompt" in kwargs and kwargs["actor_b_raw_prompt"] is not None:
            return list(kwargs["actor_b_raw_prompt"])
        extra_info = kwargs.get("extra_info") or {}
        if isinstance(extra_info, dict) and extra_info.get("actor_b_raw_prompt"):
            return list(extra_info["actor_b_raw_prompt"])
        return [
            {"role": "system", "content": self.actor_b_system_prompt},
            {"role": "user", "content": "Initial task/context:\n" + messages_to_text(raw_prompt)},
        ]

    async def _run_one(
        self,
        kwargs: dict[str, Any],
        sampling_a: dict[str, Any],
        sampling_b: dict[str, Any],
    ) -> tuple[_View, _View]:
        raw_prompt = list(kwargs["raw_prompt"])
        a_messages = list(raw_prompt)
        b_messages = self._initial_actor_b_messages(raw_prompt, kwargs)

        a_prompt_ids = await self.actor_a.encode(a_messages, add_generation_prompt=True)
        b_prompt_ids = await self.actor_b.encode(b_messages, add_generation_prompt=True)
        a_response_ids: list[int] = []
        a_response_mask: list[int] = []
        b_response_ids: list[int] = []
        b_response_mask: list[int] = []
        transcript: list[dict[str, str]] = []

        request_id = uuid4().hex
        for turn_idx in range(self.max_turns):
            _, a_ids, a_text = await self.actor_a.generate(f"{request_id}:a", a_messages, sampling_a)
            a_messages.append({"role": "assistant", "content": a_text})
            b_messages.append({"role": "user", "content": a_text})
            transcript.append({"speaker": "agent_a", "content": a_text})
            a_response_ids.extend(a_ids)
            a_response_mask.extend([1] * len(a_ids))
            b_obs_ids = await self.actor_b.encode([{"role": "user", "content": a_text}], add_generation_prompt=True)
            b_response_ids.extend(b_obs_ids)
            b_response_mask.extend([0] * len(b_obs_ids))
            if self._should_stop(a_text):
                break

            _, b_ids, b_text = await self.actor_b.generate(f"{request_id}:b", b_messages, sampling_b)
            b_messages.append({"role": "assistant", "content": b_text})
            a_messages.append({"role": "user", "content": b_text})
            transcript.append({"speaker": "agent_b", "content": b_text})
            b_response_ids.extend(b_ids)
            b_response_mask.extend([1] * len(b_ids))
            a_obs_ids = await self.actor_a.encode([{"role": "user", "content": b_text}], add_generation_prompt=True)
            a_response_ids.extend(a_obs_ids)
            a_response_mask.extend([0] * len(a_obs_ids))
            if self._should_stop(b_text):
                break

        transcript_text = "\n".join(f"{m['speaker']}: {m['content']}" for m in transcript)
        return (
            _View(a_prompt_ids, a_response_ids, a_response_mask, transcript_text),
            _View(b_prompt_ids, b_response_ids, b_response_mask, transcript_text),
        )

    def _should_stop(self, text: str) -> bool:
        return any(stop and stop in text for stop in self.stop_strings)

    def _views_to_dataproto(self, role: str, views: list[_View], runtime: _Runtime) -> DataProto:
        prompt_length = runtime.rollout_config.prompt_length
        response_length = runtime.rollout_config.response_length
        pad_id = runtime.tokenizer.pad_token_id or 0

        prompts = torch.full((len(views), prompt_length), pad_id, dtype=torch.long)
        prompt_attention = torch.zeros((len(views), prompt_length), dtype=torch.long)
        responses = torch.full((len(views), response_length), pad_id, dtype=torch.long)
        response_attention = torch.zeros((len(views), response_length), dtype=torch.long)
        response_mask = torch.zeros((len(views), response_length), dtype=torch.long)

        for row, view in enumerate(views):
            prompt_ids = view.prompt_ids[-prompt_length:]
            response_ids = view.response_ids[:response_length]
            mask = view.response_mask[:response_length]

            prompts[row, -len(prompt_ids) :] = torch.tensor(prompt_ids, dtype=torch.long)
            prompt_attention[row, -len(prompt_ids) :] = 1
            if response_ids:
                responses[row, : len(response_ids)] = torch.tensor(response_ids, dtype=torch.long)
                response_attention[row, : len(response_ids)] = 1
                response_mask[row, : len(mask)] = torch.tensor(mask, dtype=torch.long)

        attention_mask = torch.cat([prompt_attention, response_attention], dim=1)
        input_ids = torch.cat([prompts, responses], dim=1)
        position_ids = compute_position_id_with_mask(attention_mask)
        batch = TensorDict(
            {
                "prompts": prompts,
                "responses": responses,
                "response_mask": response_mask,
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "position_ids": position_ids,
            },
            batch_size=len(views),
        )
        non_tensor_batch = {
            "dual_agent_role": np.array([role] * len(views), dtype=object),
            "dual_transcript": np.array([view.transcript for view in views], dtype=object),
        }
        return DataProto(batch=batch, non_tensor_batch=non_tensor_batch, meta_info={"metrics": []})


class DualAgentRolloutManager:
    def __init__(
        self,
        config: DictConfig,
        actor_a_wg: RayWorkerGroup,
        actor_b_wg: RayWorkerGroup,
        actor_a_resource_pool: RayResourcePool,
        actor_b_resource_pool: RayResourcePool,
    ):
        self.config = config
        self.actor_a_config = actor_config_view(config, ACTOR_A_KEY)
        self.actor_b_config = actor_config_view(config, ACTOR_B_KEY)
        self.actor_a_wg = actor_a_wg
        self.actor_b_wg = actor_b_wg
        self.actor_a_resource_pool = actor_a_resource_pool
        self.actor_b_resource_pool = actor_b_resource_pool

    @classmethod
    @auto_await
    async def create(
        cls,
        config: DictConfig,
        actor_a_wg: RayWorkerGroup,
        actor_b_wg: RayWorkerGroup,
        actor_a_resource_pool: RayResourcePool,
        actor_b_resource_pool: RayResourcePool,
    ):
        instance = cls(config, actor_a_wg, actor_b_wg, actor_a_resource_pool, actor_b_resource_pool)
        instance.actor_a_manager = await AgentLoopManager.create(
            config=instance.actor_a_config,
            worker_group=actor_a_wg,
            rollout_resource_pool=actor_a_resource_pool,
        )
        instance.actor_b_manager = await AgentLoopManager.create(
            config=instance.actor_b_config,
            worker_group=actor_b_wg,
            rollout_resource_pool=actor_b_resource_pool,
        )
        await instance._init_dual_workers()
        return instance

    async def _init_dual_workers(self):
        self.dual_workers = []
        num_workers = int(self.config.dual_agent.rollout.num_workers)
        actor_a_servers = list(
            zip(self.actor_a_manager.server_addresses, self.actor_a_manager.server_handles, strict=True)
        )
        actor_b_servers = list(
            zip(self.actor_b_manager.server_addresses, self.actor_b_manager.server_handles, strict=True)
        )
        node_ids = [node["NodeID"] for node in ray.nodes() if node["Alive"] and node["Resources"].get("CPU", 0) > 0]
        for i in range(num_workers):
            node_id = node_ids[i % len(node_ids)]
            self.dual_workers.append(
                DualAgentRolloutWorker.options(
                    name=f"dual_agent_rollout_worker_{i}_{uuid4().hex[:8]}",
                    scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                        node_id=node_id, soft=True
                    ),
                ).remote(
                    self.config,
                    actor_a_servers,
                    self.actor_a_manager.global_load_balancer,
                    actor_b_servers,
                    self.actor_b_manager.global_load_balancer,
                )
            )

    @auto_await
    async def generate_sequences(self, prompts: DataProto) -> DualAgentRolloutBatch:
        started = time.time()
        chunks = prompts.chunk(len(self.dual_workers))
        outputs = await asyncio.gather(
            *[
                worker.generate_sequences.remote(chunk)
                for worker, chunk in zip(self.dual_workers, chunks, strict=True)
            ]
        )
        actor_a = DataProto.concat([output["actor_a"] for output in outputs])
        actor_b = DataProto.concat([output["actor_b"] for output in outputs])
        timing = {"dual_agent/generate_sequences": time.time() - started}
        actor_a.meta_info["timing"] = timing
        actor_b.meta_info["timing"] = timing
        return DualAgentRolloutBatch(actor_a=actor_a, actor_b=actor_b, timing=timing)
