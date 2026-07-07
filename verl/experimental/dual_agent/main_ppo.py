"""Hydra entrypoint for experimental dual-agent PPO/GRPO training."""

from __future__ import annotations

import os
import socket
from pprint import pprint

import hydra
import ray
from omegaconf import OmegaConf, open_dict

from verl.experimental.reward_loop import migrate_legacy_reward_impl
from verl.trainer.constants_ppo import get_ppo_ray_runtime_env
from verl.utils import hf_processor, hf_tokenizer
from verl.utils.config import validate_config
from verl.utils.device import auto_set_device
from verl.utils.fs import copy_to_local

from .utils import (
    ACTOR_A_KEY,
    ACTOR_A_ROLE,
    ACTOR_B_KEY,
    ACTOR_B_ROLE,
    actor_config_view,
    materialized_actor_rollout_ref,
    pool_spec_from_config,
    resolve_actor_rollout_worker,
)


@hydra.main(config_path="config", config_name="dual_agent_ppo", version_base=None)
def main(config):
    auto_set_device(config)
    config = migrate_legacy_reward_impl(config)
    run_dual_agent_ppo(config)


def run_dual_agent_ppo(config) -> None:
    if not ray.is_initialized():
        default_runtime_env = get_ppo_ray_runtime_env()
        ray_init_kwargs = config.ray_kwargs.get("ray_init", {})
        runtime_env_kwargs = ray_init_kwargs.get("runtime_env", {})
        runtime_env = OmegaConf.merge(default_runtime_env, runtime_env_kwargs)
        ray_init_kwargs = OmegaConf.create({**ray_init_kwargs, "runtime_env": runtime_env})
        ray.init(**OmegaConf.to_container(ray_init_kwargs))

    runner = ray.remote(num_cpus=1)(DualAgentTaskRunner).remote()
    ray.get(runner.run.remote(config))


class DualAgentTaskRunner:
    def run(self, config):
        print(f"DualAgentTaskRunner hostname: {socket.gethostname()}, PID: {os.getpid()}")
        pprint(OmegaConf.to_container(config, resolve=True))
        OmegaConf.resolve(config)

        with open_dict(config):
            config.actor_a_rollout_ref = materialized_actor_rollout_ref(config, ACTOR_A_KEY)
            config.actor_b_rollout_ref = materialized_actor_rollout_ref(config, ACTOR_B_KEY)
            config.actor_a_rollout_ref.rollout.n_gpus_per_node = config.dual_agent.actor_a.n_gpus_per_node
            config.actor_b_rollout_ref.rollout.n_gpus_per_node = config.dual_agent.actor_b.n_gpus_per_node
            config.actor_a_rollout_ref.rollout.n = config.dual_agent.rollout.n
            config.actor_b_rollout_ref.rollout.n = config.dual_agent.rollout.n
            # Keep the canonical field pointing at A so inherited dataset/trainer
            # utilities that still read actor_rollout_ref remain usable.
            config.actor_rollout_ref = config.actor_a_rollout_ref

        validate_config(
            config=actor_config_view(config, ACTOR_A_KEY),
            use_reference_policy=False,
            use_critic=False,
        )
        validate_config(
            config=actor_config_view(config, ACTOR_B_KEY),
            use_reference_policy=False,
            use_critic=False,
        )

        actor_a_remote_cls = ray.remote(resolve_actor_rollout_worker(config, ACTOR_A_KEY))
        actor_b_remote_cls = ray.remote(resolve_actor_rollout_worker(config, ACTOR_B_KEY))

        from verl.trainer.ppo.utils import Role

        role_worker_mapping = {
            # String roles are consumed by DualAgentPPOTrainer.init_workers.
            ACTOR_A_ROLE: actor_a_remote_cls,
            ACTOR_B_ROLE: actor_b_remote_cls,
            # Compatibility alias for RayPPOTrainer.__init__.
            Role.ActorRollout: actor_a_remote_cls,
        }
        mapping = {ACTOR_A_ROLE: "actor_a_pool", ACTOR_B_ROLE: "actor_b_pool"}

        from verl.trainer.ppo.ray_trainer import ResourcePoolManager

        resource_pool_manager = ResourcePoolManager(resource_pool_spec=pool_spec_from_config(config), mapping=mapping)

        local_path = copy_to_local(
            config.actor_a_rollout_ref.model.path,
            use_shm=config.actor_a_rollout_ref.model.get("use_shm", False),
        )
        trust_remote_code = config.data.get("trust_remote_code", False)
        tokenizer = hf_tokenizer(local_path, trust_remote_code=trust_remote_code)
        processor = hf_processor(local_path, trust_remote_code=trust_remote_code, use_fast=True)

        from verl.trainer.main_ppo import create_rl_dataset, create_rl_sampler
        from verl.utils.dataset.rl_dataset import collate_fn

        train_dataset = create_rl_dataset(
            config.data.train_files,
            config.data,
            tokenizer,
            processor,
            is_train=True,
            max_samples=config.data.get("train_max_samples", -1),
        )
        val_dataset = create_rl_dataset(
            config.data.val_files,
            config.data,
            tokenizer,
            processor,
            is_train=False,
            max_samples=config.data.get("val_max_samples", -1),
        )
        train_sampler = create_rl_sampler(config.data, train_dataset)

        from verl.single_controller.ray import RayWorkerGroup
        from .trainer import DualAgentPPOTrainer

        trainer = DualAgentPPOTrainer(
            config=config,
            tokenizer=tokenizer,
            processor=processor,
            role_worker_mapping=role_worker_mapping,
            resource_pool_manager=resource_pool_manager,
            ray_worker_group_cls=RayWorkerGroup,
            train_dataset=train_dataset,
            val_dataset=val_dataset,
            collate_fn=collate_fn,
            train_sampler=train_sampler,
        )
        trainer.init_workers()
        trainer.fit()


if __name__ == "__main__":
    main()
