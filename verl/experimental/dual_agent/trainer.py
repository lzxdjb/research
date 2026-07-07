"""Experimental single-process dual-agent PPO/GRPO trainer."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any

import numpy as np
import ray
import torch
from omegaconf import OmegaConf
from omegaconf import open_dict
from tensordict import TensorDict
from tqdm import tqdm

from verl import DataProto
from verl.checkpoint_engine import CheckpointEngineManager
from verl.protocol import pad_dataproto_to_divisor, unpad_dataproto
from verl.single_controller.ray import RayClassWithInitArgs
from verl.single_controller.ray.base import create_colocated_worker_cls
from verl.trainer.config import AlgoConfig
from verl.trainer.ppo.core_algos import AdvantageEstimator, agg_loss
from verl.trainer.ppo.metric_utils import (
    compute_data_metrics,
    compute_throughout_metrics,
    compute_timing_metrics,
)
from verl.trainer.ppo.ray_trainer import RayPPOTrainer, apply_kl_penalty, compute_advantage, compute_response_mask
from verl.trainer.ppo.reward import extract_reward
from verl.trainer.ppo.utils import need_critic, need_reference_policy
from verl.utils import tensordict_utils as tu
from verl.utils.config import omega_conf_to_dataclass
from verl.utils.py_functional import rename_dict
from verl.utils.seqlen_balancing import get_seqlen_balanced_partitions
from verl.workers.utils.padding import left_right_2_no_padding, no_padding_2_padding

from .dual_rollout import DualAgentRolloutManager
from .utils import (
    ACTOR_A_KEY,
    ACTOR_A_ROLE,
    ACTOR_B_KEY,
    ACTOR_B_ROLE,
    actor_config_view,
    runtime_worker_role,
)


class _PrefixedRewardLoopManager:
    """RewardLoopManager variant with unique Ray actor names per agent."""

    def __init__(self, config: DictConfig, prefix: str, rm_resource_pool=None):
        from verl.experimental.reward_loop.reward_loop import RewardLoopWorker
        from verl.experimental.reward_loop.reward_model import RewardModelManager

        self.config = config
        self.prefix = prefix
        if self.config.reward.reward_model.enable:
            self.reward_model_manager = RewardModelManager(config.reward.reward_model, rm_resource_pool)
            self.reward_router_address = self.reward_model_manager.get_router_address()
        else:
            self.reward_model_manager = None
            self.reward_router_address = None
        self.reward_loop_workers_class = ray.remote(RewardLoopWorker)
        self._init_reward_loop_workers()

    def _init_reward_loop_workers(self):
        self.reward_loop_workers = []
        num_workers = self.config.reward.num_workers
        node_ids = [node["NodeID"] for node in ray.nodes() if node["Alive"] and node["Resources"].get("CPU", 0) > 0]
        for i in range(num_workers):
            node_id = node_ids[i % len(node_ids)]
            self.reward_loop_workers.append(
                self.reward_loop_workers_class.options(
                    name=f"{self.prefix}_reward_loop_worker_{i}_{uuid.uuid4().hex[:8]}",
                    scheduling_strategy=ray.util.scheduling_strategies.NodeAffinitySchedulingStrategy(
                        node_id=node_id, soft=True
                    ),
                ).remote(self.config, self.reward_router_address)
            )

    def compute_rm_score(self, data: DataProto) -> DataProto:
        if self.reward_model_manager is not None:
            self.reward_model_manager.wake_up()

        chunks = data.chunk(len(self.reward_loop_workers))
        outputs = ray.get(
            [
                worker.compute_score_batch.remote(chunk)
                for worker, chunk in zip(self.reward_loop_workers, chunks, strict=True)
            ]
        )
        outputs_flat = [item for sublist in outputs for item in sublist]
        scores = [item["reward_score"] for item in outputs_flat]
        prompt_length = data.batch["prompts"].size(1)
        valid_response_length = data.batch["attention_mask"][:, prompt_length:].sum(dim=1)
        rm_scores = torch.zeros_like(data.batch["responses"], dtype=torch.float32)
        rm_scores[torch.arange(rm_scores.size(0)), valid_response_length - 1] = torch.tensor(
            scores, dtype=torch.float32
        )
        batch = TensorDict({"rm_scores": rm_scores}, batch_size=len(data))

        reward_extra_infos = [output.get("reward_extra_info", {}) for output in outputs_flat]
        non_tensor_batch = {}
        if reward_extra_infos:
            for key in reward_extra_infos[0].keys():
                non_tensor_batch[key] = np.array([info[key] for info in reward_extra_infos])

        if self.reward_model_manager is not None:
            self.reward_model_manager.sleep()
        return DataProto(batch=batch, non_tensor_batch=non_tensor_batch)


class DualAgentPPOTrainer(RayPPOTrainer):
    """A narrow dual-agent trainer built on top of verl worker primitives.

    Current intended algorithm is critic-free GRPO/REINFORCE-style training.
    Each step:
    1. generate an interactive A/B trajectory,
    2. build one training view per actor,
    3. compute rewards/advantages separately,
    4. update both actors concurrently,
    5. sync both trainers to rollout replicas before the next generation phase.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with open_dict(self.config):
            self.config.actor_a_rollout_ref.actor.optim.total_training_steps = self.total_training_steps
            self.config.actor_b_rollout_ref.actor.optim.total_training_steps = self.total_training_steps
        self.actor_a_rollout_wg = None
        self.actor_b_rollout_wg = None
        self.actor_a_checkpoint_manager = None
        self.actor_b_checkpoint_manager = None
        self.actor_a_reward_loop_manager = None
        self.actor_b_reward_loop_manager = None

    def init_workers(self):
        if need_critic(self.config):
            raise NotImplementedError("dual_agent trainer currently supports critic-free algorithms only.")
        if need_reference_policy(actor_config_view(self.config, ACTOR_A_KEY)) or need_reference_policy(
            actor_config_view(self.config, ACTOR_B_KEY)
        ):
            raise NotImplementedError("dual_agent trainer currently requires KL/ref policy disabled for both actors.")

        self.resource_pool_manager.create_resource_pool()
        self.resource_pool_to_cls = {pool: {} for pool in self.resource_pool_manager.resource_pool_dict.values()}

        actor_a_pool = self.resource_pool_manager.get_resource_pool(ACTOR_A_ROLE)
        actor_b_pool = self.resource_pool_manager.get_resource_pool(ACTOR_B_ROLE)

        actor_a_cls = RayClassWithInitArgs(
            cls=self.role_worker_mapping[ACTOR_A_ROLE],
            config=self.config.actor_a_rollout_ref,
            role=runtime_worker_role(self.config, ACTOR_A_KEY),
        )
        actor_b_cls = RayClassWithInitArgs(
            cls=self.role_worker_mapping[ACTOR_B_ROLE],
            config=self.config.actor_b_rollout_ref,
            role=runtime_worker_role(self.config, ACTOR_B_KEY),
        )
        self.resource_pool_to_cls[actor_a_pool][ACTOR_A_ROLE] = actor_a_cls
        self.resource_pool_to_cls[actor_b_pool][ACTOR_B_ROLE] = actor_b_cls

        all_wg = {}
        wg_kwargs = {"device_name": self.device_name}
        for resource_pool, class_dict in self.resource_pool_to_cls.items():
            worker_dict_cls = create_colocated_worker_cls(class_dict=class_dict)
            wg_dict = self.ray_worker_group_cls(
                resource_pool=resource_pool,
                ray_cls_with_init=worker_dict_cls,
                **wg_kwargs,
            )
            all_wg.update(wg_dict.spawn(prefix_set=class_dict.keys()))

        self.actor_a_rollout_wg = all_wg[ACTOR_A_ROLE]
        self.actor_b_rollout_wg = all_wg[ACTOR_B_ROLE]

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_a = executor.submit(self.actor_a_rollout_wg.init_model)
            fut_b = executor.submit(self.actor_b_rollout_wg.init_model)
            fut_a.result()
            fut_b.result()

        self.actor_a_reward_loop_manager = _PrefixedRewardLoopManager(
            config=actor_config_view(self.config, ACTOR_A_KEY), prefix="actor_a", rm_resource_pool=None
        )
        self.actor_b_reward_loop_manager = _PrefixedRewardLoopManager(
            config=actor_config_view(self.config, ACTOR_B_KEY), prefix="actor_b", rm_resource_pool=None
        )
        # Keep inherited helpers that read self.reward_loop_manager pointed at A.
        self.reward_loop_manager = self.actor_a_reward_loop_manager
        self.dual_rollout_manager = DualAgentRolloutManager.create(
            config=self.config,
            actor_a_wg=self.actor_a_rollout_wg,
            actor_b_wg=self.actor_b_rollout_wg,
            actor_a_resource_pool=actor_a_pool,
            actor_b_resource_pool=actor_b_pool,
        )

        self.actor_a_checkpoint_manager = CheckpointEngineManager(
            config=omega_conf_to_dataclass(self.config.actor_a_rollout_ref.rollout.checkpoint_engine),
            trainer=self.actor_a_rollout_wg,
            replicas=self.dual_rollout_manager.actor_a_manager.rollout_replicas,
        )
        self.actor_b_checkpoint_manager = CheckpointEngineManager(
            config=omega_conf_to_dataclass(self.config.actor_b_rollout_ref.rollout.checkpoint_engine),
            trainer=self.actor_b_rollout_wg,
            replicas=self.dual_rollout_manager.actor_b_manager.rollout_replicas,
        )
        self.actor_a_checkpoint_manager.sleep_replicas()
        self.actor_b_checkpoint_manager.sleep_replicas()

    def fit(self):
        from verl.utils.tracking import Tracking

        logger = Tracking(
            project_name=self.config.trainer.project_name,
            experiment_name=self.config.trainer.experiment_name,
            default_backend=self.config.trainer.logger,
            config=OmegaConf.to_container(self.config, resolve=True),
        )

        self.global_steps = 0
        self.actor_a_checkpoint_manager.update_weights(self.global_steps)
        self.actor_b_checkpoint_manager.update_weights(self.global_steps)

        progress_bar = tqdm(total=self.total_training_steps, initial=self.global_steps, desc="Dual-Agent Training")
        self.global_steps += 1

        rollout_n = int(self.config.dual_agent.rollout.get("n", self.config.actor_a_rollout_ref.rollout.n))
        for _epoch in range(self.config.trainer.total_epochs):
            for batch_dict in self.train_dataloader:
                metrics: dict[str, Any] = {}
                timing_raw: dict[str, Any] = {}
                batch = DataProto.from_single_dict(batch_dict)
                batch.non_tensor_batch["uid"] = np.array([str(uuid.uuid4()) for _ in range(len(batch))], dtype=object)

                gen_batch = self._get_gen_batch(batch)
                gen_batch.meta_info["global_steps"] = self.global_steps
                gen_batch = gen_batch.repeat(repeat_times=rollout_n, interleave=True)
                rollout_output = self.dual_rollout_manager.generate_sequences(gen_batch)
                self.actor_a_checkpoint_manager.sleep_replicas()
                self.actor_b_checkpoint_manager.sleep_replicas()
                timing_raw.update(rollout_output.timing)

                base_batch = batch.repeat(repeat_times=rollout_n, interleave=True)
                actor_a_batch = deepcopy(base_batch).union(rollout_output.actor_a)
                actor_b_batch = deepcopy(base_batch).union(rollout_output.actor_b)

                actor_a_batch, actor_a_metrics = self._prepare_actor_batch(
                    "actor_a",
                    self.actor_a_rollout_wg,
                    self.actor_a_reward_loop_manager,
                    self.config.actor_a_rollout_ref,
                    actor_a_batch,
                    rollout_n,
                )
                actor_b_batch, actor_b_metrics = self._prepare_actor_batch(
                    "actor_b",
                    self.actor_b_rollout_wg,
                    self.actor_b_reward_loop_manager,
                    self.config.actor_b_rollout_ref,
                    actor_b_batch,
                    rollout_n,
                )
                metrics.update(actor_a_metrics)
                metrics.update(actor_b_metrics)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    fut_a = executor.submit(
                        self._update_actor_for,
                        "actor_a",
                        self.actor_a_rollout_wg,
                        self.config.actor_a_rollout_ref,
                        actor_a_batch,
                    )
                    fut_b = executor.submit(
                        self._update_actor_for,
                        "actor_b",
                        self.actor_b_rollout_wg,
                        self.config.actor_b_rollout_ref,
                        actor_b_batch,
                    )
                    actor_a_output = fut_a.result()
                    actor_b_output = fut_b.result()

                metrics.update(self._prefixed_metrics(actor_a_output, "actor_a"))
                metrics.update(self._prefixed_metrics(actor_b_output, "actor_b"))

                self.actor_a_checkpoint_manager.update_weights(self.global_steps)
                self.actor_b_checkpoint_manager.update_weights(self.global_steps)

                data_metrics = compute_data_metrics(batch=actor_a_batch, use_critic=False)
                metrics.update({f"actor_a/{k}": v for k, v in data_metrics.items()})
                metrics.update(compute_timing_metrics(batch=actor_a_batch, timing_raw=timing_raw))
                metrics.update(
                    compute_throughout_metrics(
                        batch=actor_a_batch,
                        timing_raw=timing_raw,
                        n_gpus=self.resource_pool_manager.get_n_gpus(),
                    )
                )

                logger.log(data=metrics, step=self.global_steps)
                progress_bar.update(1)
                self.global_steps += 1
                if self.global_steps > self.total_training_steps:
                    progress_bar.close()
                    return
        progress_bar.close()

    def _prepare_actor_batch(
        self,
        name: str,
        worker_group,
        reward_loop_manager,
        actor_cfg: DictConfig,
        batch: DataProto,
        rollout_n: int,
    ) -> tuple[DataProto, dict[str, Any]]:
        metrics: dict[str, Any] = {}
        if "response_mask" not in batch.batch:
            batch.batch["response_mask"] = compute_response_mask(batch)

        batch.meta_info["global_token_num"] = torch.sum(batch.batch["attention_mask"], dim=-1).tolist()
        reward_proto = reward_loop_manager.compute_rm_score(batch)
        batch = batch.union(reward_proto)
        self._move_reward_to_last_trainable_token(batch)
        reward_tensor, reward_extra_infos_dict = extract_reward(batch)

        old_log_prob, old_log_prob_mfu = self._compute_old_log_prob_for(worker_group, actor_cfg, batch)
        entropys = old_log_prob.batch["entropys"]
        entropy_agg = agg_loss(
            loss_mat=entropys,
            loss_mask=batch.batch["response_mask"],
            loss_agg_mode=actor_cfg.actor.loss_agg_mode,
            loss_scale_factor=actor_cfg.actor.loss_scale_factor,
        )
        old_log_prob.batch.pop("entropys")
        batch = batch.union(old_log_prob)
        metrics[f"{name}/entropy"] = entropy_agg.detach().item()
        metrics[f"{name}/perf/mfu/actor_infer"] = old_log_prob_mfu

        batch.batch["token_level_scores"] = reward_tensor
        if reward_extra_infos_dict:
            batch.non_tensor_batch.update({k: np.array(v) for k, v in reward_extra_infos_dict.items()})

        if self.config.algorithm.use_kl_in_reward:
            batch, kl_metrics = apply_kl_penalty(
                batch, kl_ctrl=self.kl_ctrl_in_reward, kl_penalty=self.config.algorithm.kl_penalty
            )
            metrics.update({f"{name}/{k}": v for k, v in kl_metrics.items()})
        else:
            batch.batch["token_level_rewards"] = batch.batch["token_level_scores"]

        batch = compute_advantage(
            batch,
            adv_estimator=self.config.algorithm.adv_estimator,
            gamma=self.config.algorithm.gamma,
            lam=self.config.algorithm.lam,
            num_repeat=rollout_n,
            norm_adv_by_std_in_grpo=self.config.algorithm.get("norm_adv_by_std_in_grpo", True),
            config=self.config.algorithm,
        )
        return batch, metrics

    def _compute_old_log_prob_for(self, worker_group, actor_cfg: DictConfig, batch: DataProto):
        if self.use_legacy_worker_impl == "disable":
            batch_td = batch.to_tensordict()
            batch_td = left_right_2_no_padding(batch_td)
            tu.assign_non_tensor(batch_td, calculate_entropy=True, compute_loss=False)
            output = worker_group.compute_log_prob(batch_td)
            entropy = tu.get(output, "entropy")
            log_probs = tu.get(output, "log_probs")
            entropy = no_padding_2_padding(entropy, batch_td)
            log_probs = no_padding_2_padding(log_probs, batch_td)
            old_log_prob = tu.get_tensordict({"old_log_probs": log_probs.float(), "entropys": entropy.float()})
            return DataProto.from_tensordict(old_log_prob), tu.get(output, "metrics")["mfu"]
        old_log_prob = worker_group.compute_log_prob(batch)
        return old_log_prob, 0

    def _update_actor_for(self, name: str, worker_group, actor_cfg: DictConfig, batch: DataProto) -> DataProto:
        batch.meta_info["multi_turn"] = True
        batch.meta_info["temperature"] = actor_cfg.rollout.temperature
        loss_mode = actor_cfg.actor.policy_loss.get("loss_mode", "vanilla")
        use_seeupo = loss_mode in ("seeupo_turn", "seeupo_turn_new") and "turn_index" in batch.batch

        if self.use_legacy_worker_impl == "disable":
            batch_td = batch.to_tensordict()
            batch_td = left_right_2_no_padding(batch_td)
            ppo_mini_batch_size = actor_cfg.actor.ppo_mini_batch_size * actor_cfg.rollout.n
            tu.assign_non_tensor(
                batch_td,
                calculate_entropy=actor_cfg.actor.entropy_coeff != 0.0,
                global_batch_size=ppo_mini_batch_size,
                mini_batch_size=ppo_mini_batch_size,
                epochs=actor_cfg.actor.ppo_epochs,
                seed=actor_cfg.actor.data_loader_seed,
                dataloader_kwargs={"shuffle": actor_cfg.actor.shuffle},
            )
            output = worker_group.update_actor_seeupo(batch_td) if use_seeupo else worker_group.update_actor(batch_td)
            metrics = tu.get(output, "metrics")
            metrics = rename_dict(metrics, f"{name}/actor/")
            if f"{name}/actor/mfu" in metrics:
                metrics[f"{name}/perf/mfu/actor"] = metrics.pop(f"{name}/actor/mfu")
            return DataProto.from_single_dict(data={}, meta_info={"metrics": metrics})

        output = worker_group.update_actor_seeupo(batch) if use_seeupo else worker_group.update_actor(batch)
        output.meta_info["metrics"] = {f"{name}/{k}": v for k, v in output.meta_info.get("metrics", {}).items()}
        return output

    def _move_reward_to_last_trainable_token(self, batch: DataProto) -> None:
        if "rm_scores" not in batch.batch:
            return
        scores = batch.batch["rm_scores"].sum(dim=-1)
        response_mask = batch.batch["response_mask"].bool()
        new_scores = torch.zeros_like(batch.batch["rm_scores"], dtype=torch.float32)
        for row in range(response_mask.size(0)):
            idxs = torch.nonzero(response_mask[row], as_tuple=False).flatten()
            if idxs.numel() > 0:
                new_scores[row, idxs[-1]] = scores[row]
        batch.batch["rm_scores"] = new_scores

    def _prefixed_metrics(self, output: DataProto, default_prefix: str) -> dict[str, Any]:
        metrics = output.meta_info.get("metrics", {})
        return {
            key if key.startswith(default_prefix) else f"{default_prefix}/{key}": value
            for key, value in metrics.items()
        }
