"""Shared helpers for the experimental dual-agent trainer."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from omegaconf import DictConfig, OmegaConf, open_dict

from verl.trainer.ppo.utils import need_reference_policy

ACTOR_A_KEY = "actor_a_rollout_ref"
ACTOR_B_KEY = "actor_b_rollout_ref"
ACTOR_A_ROLE = "actor_a_rollout_ref"
ACTOR_B_ROLE = "actor_b_rollout_ref"


def actor_config_view(config: DictConfig, actor_key: str) -> DictConfig:
    """Return a config view whose canonical actor_rollout_ref points to one agent."""
    view = OmegaConf.create(OmegaConf.to_container(config, resolve=False))
    with open_dict(view):
        view.actor_rollout_ref = deepcopy(config[actor_key])
    return view


def materialized_actor_rollout_ref(config: DictConfig, actor_key: str) -> DictConfig:
    """Resolve one actor config with actor_rollout_ref rebound to that actor."""
    view = actor_config_view(config, actor_key)
    resolved = OmegaConf.to_container(view.actor_rollout_ref, resolve=True, throw_on_missing=False)
    return OmegaConf.create(resolved)


def runtime_worker_role(config: DictConfig, actor_key: str) -> str:
    """Worker internals only accept the standard verl role strings."""
    view = actor_config_view(config, actor_key)
    return "actor_rollout_ref" if need_reference_policy(view) else "actor_rollout"


def resolve_actor_rollout_worker(config: DictConfig, actor_key: str) -> Any:
    """Resolve the worker class for one actor from its strategy config."""
    actor_cfg = config[actor_key]
    use_legacy_worker_impl = config.trainer.get("use_legacy_worker_impl", "auto")

    if use_legacy_worker_impl == "disable":
        from verl.workers.engine_workers import ActorRolloutRefWorker

        return ActorRolloutRefWorker

    strategy = actor_cfg.actor.strategy
    if strategy in {"fsdp", "fsdp2"}:
        from verl.workers.fsdp_workers import AsyncActorRolloutRefWorker

        return AsyncActorRolloutRefWorker
    if strategy == "megatron":
        from verl.workers.megatron_workers import AsyncActorRolloutRefWorker

        return AsyncActorRolloutRefWorker
    raise NotImplementedError(f"Unsupported dual-agent actor strategy: {strategy}")


def get_dual_cfg(config: DictConfig) -> DictConfig:
    if "dual_agent" not in config:
        raise ValueError("dual_agent config block is required")
    return config.dual_agent


def pool_spec_from_config(config: DictConfig) -> dict[str, list[int]]:
    """Create two independent GPU pools, one per actor."""
    dual_cfg = get_dual_cfg(config)
    actor_a = dual_cfg.actor_a
    actor_b = dual_cfg.actor_b
    return {
        "actor_a_pool": [int(actor_a.n_gpus_per_node)] * int(actor_a.nnodes),
        "actor_b_pool": [int(actor_b.n_gpus_per_node)] * int(actor_b.nnodes),
    }


def messages_to_text(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        role = message.get("role", "unknown")
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
