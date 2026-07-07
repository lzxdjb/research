# Copyright 2024 Bytedance Ltd. and/or its affiliates
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
"""
Megatron Actor.
In megatron actor, the differences are:
1. We only make minibatch

Note that our model doesn't have to be `MegatronModule` because we don't share embedding in the last layer
"""

import itertools
import logging
import os
from functools import partial
from typing import Iterable

import torch
import torch.distributed
from megatron.core import parallel_state as mpu
from megatron.core.distributed import finalize_model_grads

# from megatron.core.optimizer import DistributedOptimizer
from megatron.core.optimizer import DistributedOptimizer
from megatron.core.pipeline_parallel import get_forward_backward_func
from omegaconf import OmegaConf
from torch import nn

from verl import DataProto
from verl.trainer.ppo.core_algos import agg_loss, get_policy_loss_fn, kl_penalty
from verl.utils.device import get_device_id, get_torch_device
from verl.utils.megatron.pipeline_parallel import make_batch_generator
from verl.utils.megatron.router_replay_patch import RouterReplay, RouterReplayAction
from verl.utils.megatron.router_replay_utils import (
    RouterReplayHelper,
    merge_router_topk_indices,
    pp_gather,
    reorder_and_merge_vpp_layers,
    set_router_replay_data,
)
from verl.utils.megatron.tensor_parallel import (
    vocab_parallel_entropy,
    vocab_parallel_log_probs_from_logits,
    vocab_parallel_sum_pi_squared_from_logits,
)
from verl.utils.megatron_utils import get_megatron_mtp_loss, get_model_config, unwrap_model
from verl.utils.profiler import GPUMemoryLogger
from verl.utils.py_functional import append_to_dict
from verl.utils.seqlen_balancing import get_reverse_idx, rearrange_micro_batches
from verl.utils.torch_functional import broadcast_dict_tensor
from verl.workers.actor import BasePPOActor
from verl.workers.config import MtpConfig

__all__ = ["MegatronPPOActor"]


logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


class MegatronPPOActor(BasePPOActor):
    def __init__(
        self,
        config,
        model_config,
        hf_config,
        tf_config,
        actor_module: nn.ModuleList,
        actor_optimizer: DistributedOptimizer,
        mtp_config: MtpConfig = None,
    ):
        """MeagtronPPOActor class. This class implements the simple PPO logics when the model is built with Megatron.

        Args:
            config (OmegaConf): the basic config that contains the hyper-parameters of PPO Actor. It must contain

                ``ppo_micro_batch_size_per_gpu``: micro batch size when updating ppo.

                ``ppo_mini_batch_size``: minibatch size when updating ppo using the batch data.

                ``ppo_epochs``: number of epochs to update the actor using the batch data.

                ``shuffle``: whether to shuffle the data after each ppo epoch.

                ``clip_ratio``: clip ratio of the ppo algorithm. See https://arxiv.org/abs/1707.06347.

                ``entropy_coeff``: entropy coefficient of the PPO loss. See https://arxiv.org/abs/1707.06347.
            model_config (OmegaConf): model configuration. It must contains ``model_config.vocab_size`` and
                ``model_config.hidden_size``
            hf_config (PretrainedConfig): huggingface config
            tf_config (TransformerConfig): mcore transformer config
            mtp_config (MtpConfig): mtp config, default None
            actor_module (nn.ModuleList): actor module is a ModuleList that contains a list of nn.Module in this
                pp stage.
                each nn.Module in this rank holds a vpp module chunk. See https://arxiv.org/pdf/2104.04473.pdf for
                more details.
                The actor module has some constraints to follow in order to use the updating logics implemented here

                1. It must implement unpad_input before any computation and pad_input after all the computation.
                Remove padding is an
                optimization that removes the padding tokens. See unpad_input and pad_input function in flash-attn
                (https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/bert_padding.py).

                2. Each pp stage must return the hidden state with the same shape [total_nnz, 1, hidden_size],
                where total_nnz is the number of valid tokens in this batch. If sequence parallel is enabled, the size
                of the hidden state is [total_nnz // tp, 1, hidden_size].
            actor_optimizer (DistributedOptimizer): currently, we only support DistributedOptimizer in Megatron.
                It implements
                zero1 optimizer that shards the optimizer state across dp ranks.

        >>> from megatron.training import get_model
        >>> from megatron.optimizer import get_megatron_optimizer
        >>> actor_module = get_model(megatron_actor_model_provider, wrap_with_ddp=True)
        >>> actor_module = nn.ModuleList(actor_module)
        >>> actor_optimizer = get_megatron_optimizer(actor_module)
        >>> actor = MegatronPPOActor(config=config,
        >>>                          model_config=actor_model_config,
        >>>                          hf_config=hf_config,
        >>>                          tf_config=tf_config,
        >>>                          actor_module=actor_module,
        >>>                          actor_optimizer=actor_optimizer)
        """
        super().__init__(config)
        self._validate_config(config)
        self.model_config = model_config
        self.hf_config = hf_config
        self.tf_config = tf_config
        self.mtp_config = mtp_config
        self.actor_module = actor_module
        self.actor_optimizer: DistributedOptimizer = actor_optimizer

        if self.mtp_config:
            assert self.mtp_config.enable, "MTP requires mtp_config.enable to be True"

        self.use_fused_kernels = self.config.get("use_fused_kernels", False)
        if getattr(self.mtp_config, "enable", False) and self.use_fused_kernels:
            self.use_fused_kernels = False
            logger.warning_once(
                "MTP is not compatible with fused kernels for now. Automatically disable use_fused_kernels."
            )
        if self.config.get("calculate_sum_pi_squared", False) and self.use_fused_kernels:
            self.use_fused_kernels = False
            logger.warning_once(
                "calculate_sum_pi_squared requires logits, so Megatron actor disables fused kernels for log-prob "
                "computation."
            )
        if self.use_fused_kernels and not getattr(self.config, "overlap_moe_expert_parallel_comm", False):
            # do not patch if overlap_moe_expert_parallel_comm is enabled
            logger.warning_once(
                "Recommend to disable use_fused_kernels since the fused kernel's performance is broken for triton>=3.3"
                "Unless you are using a very old version of triton < 3.3"
            )
            from verl.models.mcore.model_forward_fused import patch_fused_forward

            for model in self.actor_module:
                patch_fused_forward(model)
        else:
            from verl.models.mcore.mtp_patch import patch_postprocess

            for model in self.actor_module:
                if self.mtp_config:
                    from verl.models.mcore.mtp_patch import patch_mtp_layer_get_embeddings

                    patch_postprocess(model)

                    if self.mtp_config.detach_encoder:
                        patch_mtp_layer_get_embeddings(model)

        self.optimizer_step_args = OmegaConf.create(
            {
                "skip_grad": None,
                "overlap_dp_param_comm": False,
                "overlap_dp_grad_comm": False,
                "gradient_accumulation_steps": 1,
                "sequence_parallel": self.tf_config.sequence_parallel,
                "DDP_impl": "local",
                "layernorm_allreduce_bucket_threshold": 0,
                "reduce_grads_use_alltoall": False,
            }
        )

        self.router_replay = self.config.router_replay
        self.enable_routing_replay = self.router_replay.mode != "disabled"
        if self.enable_routing_replay:
            self.mini_layer_topk_idx_list = []

        config = get_model_config(self.actor_module[0])
        print(config)
        config.finalize_model_grads_func = finalize_model_grads

    def _validate_config(self, config) -> None:
        """Validate config options not implemented for Megatron backend"""
        assert config.get("ulysses_sequence_parallel_size", 1) == 1
        if config.get("shuffle", False):
            assert config.data_loader_seed is not None, "If shuffle dataloader, seed must be manually set"
        if config.megatron.tensor_model_parallel_size == 1:
            print("[Warining] Because actor tp size == 1, set sp to False")
            config.megatron.sequence_parallel = False
        self.config = config

    @GPUMemoryLogger(role="megatron actor", logger=logger)
    def compute_log_prob(self, data: DataProto, calculate_entropy=False) -> torch.Tensor:
        """Compute the log probability of the responses given input_ids, attention_mask and position_ids

        Args:
            data (DataProto): a DataProto containing keys

                ``input_ids``: tensor of shape [batch_size, sequence_length]. torch.int64. Note that input_ids is the
                concatenation of prompt and response. Note that ``sequence_length = prompt_length + response_length``.

                ``attention_mask``: tensor of shape [batch_size, sequence_length]. torch.int64.

                ``position_ids``: tensor of shape [batch_size, sequence_length]. torch.int64.

                ``responses``:  tensor of shape [batch_size, response_length]. torch.int64.

        Returns:
            DataProto: torch.Tensor: the log_prob tensor
        """
        prev_modes = [m.training for m in self.actor_module]
        for module in self.actor_module:
            module.eval()
        use_dynamic_bsz = data.meta_info.get("use_dynamic_bsz", False)
        micro_batch_size = data.meta_info.get("micro_batch_size", None)
        max_token_len = data.meta_info.get("max_token_len", None)
        calculate_sum_pi_squared = self.config.get("calculate_sum_pi_squared", False)
        if use_dynamic_bsz:
            assert max_token_len is not None, "max_token_len must be set when use_dynamic_bsz is True"
            max_token_len = max_token_len * self.config.megatron.context_parallel_size
        else:
            assert micro_batch_size is not None, (
                "micro batch size is needed for forward compute when use_dynamic_bsz is False"
            )

        def compute_logprobs_fn(output, data, use_dynamic_bsz=False, indices=None):
            response = data["responses"]
            response_length = response.size(1)
            log_probs = output["log_probs"][:, -response_length - 1 : -1].contiguous()
            stats = {"log_probs": log_probs}
            if calculate_sum_pi_squared:
                stats["sum_pi_squared"] = output["sum_pi_squared"][:, -response_length - 1 : -1].contiguous()
            return stats

        # We make recompute_old_log_prob by default here.
        # TODO (zhangchi.usc1992): actually, this function should only return log_prob and this logic should be
        # handled by user outside
        recompute_old_log_prob = self.config.get("recompute_old_log_prob", True)

        entropys = torch.Tensor()
        if recompute_old_log_prob:
            select_keys = ["responses", "input_ids", "attention_mask", "position_ids"]

            if self.enable_routing_replay and self.config.router_replay.mode == "R3":
                assert "routed_experts" in data.batch.keys(), "routed_experts must be in data.batch.keys()"
                select_keys.append("routed_experts")

            batch = data.select(batch_keys=select_keys).batch
            input_ids = batch["input_ids"]
            batch_size = input_ids.size(0)
            response = batch["responses"]
            response_length = response.size(1)
            with torch.no_grad():
                output = self.forward_backward_batch(
                    data,
                    forward_only=True,
                    post_process_fn=compute_logprobs_fn,
                    calculate_entropy=calculate_entropy,
                    use_dynamic_bsz=use_dynamic_bsz,
                    micro_batch_size=micro_batch_size,
                    max_token_len=max_token_len,
                )
                if mpu.is_pipeline_last_stage(ignore_virtual=True):
                    # only on last rank. It should be on every tp rank
                    if calculate_entropy:
                        output_stats = [o[0] for o in output["output"]]
                    else:
                        output_stats = output["output"]
                    log_probs = [o["log_probs"] for o in output_stats]  # (bs, seq_size)
                    log_probs = torch.cat(log_probs, dim=0).to(torch.float32)
                    if calculate_sum_pi_squared:
                        sum_pi_squared = [o["sum_pi_squared"] for o in output_stats]  # (bs, seq_size)
                        sum_pi_squared = torch.cat(sum_pi_squared, dim=0).to(torch.float32)
                    if use_dynamic_bsz:
                        indices = output["indices"]
                        indices = list(itertools.chain.from_iterable(indices))
                        assert len(indices) == log_probs.size(0), f"{len(indices)} vs. {log_probs.size()}"
                        revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
                        log_probs = log_probs[revert_indices]
                        if calculate_sum_pi_squared:
                            sum_pi_squared = sum_pi_squared[revert_indices]
                else:
                    log_probs = torch.empty(
                        size=(batch_size, response_length), dtype=torch.float32, device=input_ids.device
                    )
                    if calculate_sum_pi_squared:
                        sum_pi_squared = torch.empty(
                            size=(batch_size, response_length), dtype=torch.float32, device=input_ids.device
                        )
                log_probs = log_probs.to(get_device_id())
                # broadcast across pp ranks
                torch.distributed.broadcast(
                    tensor=log_probs,
                    src=mpu.get_pipeline_model_parallel_last_rank(),
                    group=mpu.get_pipeline_model_parallel_group(),
                    async_op=False,
                )
                log_probs = log_probs.to("cpu")
                if calculate_sum_pi_squared:
                    sum_pi_squared = sum_pi_squared.to(get_device_id())
                    torch.distributed.broadcast(
                        tensor=sum_pi_squared,
                        src=mpu.get_pipeline_model_parallel_last_rank(),
                        group=mpu.get_pipeline_model_parallel_group(),
                        async_op=False,
                    )
                    sum_pi_squared = sum_pi_squared.to("cpu")
                if calculate_entropy:
                    # Note that o[0] is metrics, o[1] is entropy
                    if mpu.is_pipeline_last_stage(ignore_virtual=True):
                        entropys = torch.cat([o[1] for o in output["output"]], dim=0)
                        entropys = entropys.to(torch.float32)
                        if use_dynamic_bsz:
                            indices = output["indices"]
                            indices = list(itertools.chain.from_iterable(indices))
                            assert len(indices) == entropys.size(0), f"{len(indices)} vs. {entropys.size()}"
                            revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
                            entropys = entropys[revert_indices]
                    else:
                        entropys = torch.empty(
                            size=(batch_size, response_length), dtype=torch.float32, device=input_ids.device
                        )
                    # broadcast across pp ranks
                    entropys = entropys.to(get_device_id())
                    torch.distributed.broadcast(
                        tensor=entropys,
                        src=mpu.get_pipeline_model_parallel_last_rank(),
                        group=mpu.get_pipeline_model_parallel_group(),
                        async_op=False,
                    )
                    entropys = entropys.to("cpu")
                layers_topk_idx = None

                if RouterReplayHelper.is_r2_record_action(self.tf_config):
                    # (bs, max_seq_len/response_len,local_layer_num,topk)
                    layers_topk_idx = output["mini_layer_topk_idx_tensor"].to(torch.uint8)
                    if use_dynamic_bsz:
                        indices = output["indices"]
                        indices = list(itertools.chain.from_iterable(indices))
                        assert len(indices) == layers_topk_idx.size(0), f"{len(indices)} vs. {layers_topk_idx.size()}"
                        revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
                        layers_topk_idx = layers_topk_idx[revert_indices]
                    layers_topk_idx = pp_gather(layers_topk_idx, self.tf_config)
        # add empty cache after each compute
        get_torch_device().empty_cache()

        for module, mode in zip(self.actor_module, prev_modes, strict=False):
            module.train(mode)
        return log_probs, entropys, layers_topk_idx, sum_pi_squared if calculate_sum_pi_squared else None

    def make_minibatch_iterator(self, data: DataProto) -> Iterable[DataProto]:
        """Make minibatch iterator for updating the actor

        Args:
            data (DataProto): a DataProto containing keys

                ``input_ids``: tensor of shape [batch_size, sequence_length]. torch.int64, where
                ``sequence_length = prompt_length + response_length``

                ``attention_mask``: tensor of shape [batch_size, sequence_length]. torch.int64

                ``position_ids``: tensor of shape [batch_size, sequence_length]. torch.int64

                ``responses``: tensor of shape [batch_size, response_length]. torch.int64. Note that
                responses = input_ids[:, -response_length:]

                ``old_log_probs``: tensor of shape [batch_size, response_length]. torch.float32. The log probability
                of responses.

                ``advantages``: tensor of shape [batch_size, response_length]. torch.float32. The advantages of
                responses.
                See PPO paper for details. https://arxiv.org/abs/1707.06347

        Returns:

        """
        select_keys = [
            "responses",
            "input_ids",
            "attention_mask",
            "response_mask",
            "position_ids",
            "old_log_probs",
            "advantages",
        ]
        if "turn_index" in data.batch.keys():
            select_keys.append("turn_index")
        loss_mode = self.config.policy_loss.get("loss_mode", "vanilla")
        if loss_mode in {"state_predictive_grpo", "state_predictive_grpo_normalized"} and "update_sketch" in data.batch.keys():
            select_keys.append("update_sketch")
        if loss_mode in {"state_predictive_grpo", "state_predictive_grpo_normalized"} and "state_index" in data.batch.keys():
            select_keys.append("state_index")
        if self.config.use_kl_loss:
            select_keys.append("ref_log_prob")
        # Include pre-computed IS weights if present in batch
        # Weights are computed centrally in trainer and added to batch when algorithm.rollout_is=True
        if "rollout_is_weights" in data.batch.keys():
            select_keys.append("rollout_is_weights")
        # Include rollout_log_probs for computing rollout_corr metrics in bypass mode
        if "rollout_log_probs" in data.batch.keys():
            select_keys.append("rollout_log_probs")
        self.has_multi_modal_inputs = "multi_modal_inputs" in data.non_tensor_batch.keys()
        # router replay
        if self.enable_routing_replay:
            select_keys.append("routed_experts")
        if self.has_multi_modal_inputs:
            data = data.select(select_keys, ["multi_modal_inputs"])
        else:
            data = data.select(batch_keys=select_keys)

        return data.make_iterator(
            mini_batch_size=self.config.ppo_mini_batch_size,
            epochs=self.config.ppo_epochs,
            seed=self.config.data_loader_seed,
            dataloader_kwargs={"shuffle": self.config.shuffle},
        )
        
    def make_distill_minibatch_iterator(self, data: DataProto) -> Iterable[DataProto]:
        """Simplified iterator for distillation — SFT-style, one epoch."""
        select_keys = [
            "responses", "input_ids", "attention_mask",
            "response_mask", "position_ids",
            "old_log_probs", "advantages",
        ]
        data = data.select(batch_keys=select_keys)
        return data.make_iterator(
            mini_batch_size=self.config.ppo_mini_batch_size,
            epochs=1,  # single pass for distillation
            seed=self.config.data_loader_seed,
            dataloader_kwargs={"shuffle": False},
    )

    def forward_backward_batch(
        self,
        data: DataProto,
        forward_only=False,
        post_process_fn=None,
        calculate_entropy=False,
        use_dynamic_bsz=False,
        micro_batch_size=None,
        max_token_len=None,
        mini_batch_size=None,
    ):
        """
        We assume:
        - The model takes input: (input_ids, attention_mask, position_ids). No rmpad for the input
        - The communication shape is (total_nnz_pad_to_sp // tp_size, 1, hidden_size) if sequence parallel is enabled
        """
        # broadcast from last pp rank to all other pp ranks
        # TODO: actually, we just need to control the sampling order.
        data.to(get_device_id())
        data.batch = data.batch.contiguous()
        mini_batch = data
        broadcast_dict_tensor(
            mini_batch.batch,
            src=mpu.get_pipeline_model_parallel_last_rank(),
            group=mpu.get_pipeline_model_parallel_group(),
        )
        mini_batch.to("cpu")
        # split into micro-batches
        mini_batch.batch["attention_mask"] = mini_batch.batch["attention_mask"].to(bool)
        self.has_multi_modal_inputs = "multi_modal_inputs" in mini_batch.non_tensor_batch.keys()
        if self.has_multi_modal_inputs:
            mini_batch.batch["multi_modal_inputs"] = mini_batch.non_tensor_batch["multi_modal_inputs"]
            mini_batch.batch["multi_modal_inputs_idx"] = torch.Tensor(
                list(range(len(mini_batch.non_tensor_batch["multi_modal_inputs"])))
            ).to(torch.int64)

        if mini_batch.batch["position_ids"].dim() == 3:  # qwen2vl mrope [bs, 3, seq_len]
            mini_batch.batch["position_ids"] = mini_batch.batch["position_ids"][
                :, 0
            ]  # mcore patch recompute qwen2vl's pos ids during forward

        indices = None
        temperature = data.meta_info["temperature"]
        if use_dynamic_bsz:
            assert max_token_len is not None, "max_token_len must be set when use_dynamic_bsz is True"
            dp_group = mpu.get_data_parallel_group()
            vpp_size = mpu.get_virtual_pipeline_model_parallel_world_size()
            if vpp_size is not None and vpp_size > 1:
                microbatch_group_size_per_vp_stage = self.tf_config.microbatch_group_size_per_vp_stage
                micro_batches, indices = rearrange_micro_batches(
                    batch=mini_batch.batch,
                    num_batches_divided_by=microbatch_group_size_per_vp_stage,
                    max_token_len=max_token_len,
                    dp_group=dp_group,
                )
                assert len(micro_batches) % self.tf_config.microbatch_group_size_per_vp_stage == 0, (
                    f"micro_batches {micro_batches} must be divisible by microbatch_group_size_per_vp_stage "
                    f"{microbatch_group_size_per_vp_stage} for megatron backend"
                )
            else:
                micro_batches, indices = rearrange_micro_batches(
                    batch=mini_batch.batch, max_token_len=max_token_len, dp_group=dp_group
                )
            total_seqlen = max_token_len
        else:
            assert micro_batch_size is not None, (
                "micro_batch_size is needed to be passed in when not using dynamic batch size"
            )
            micro_batches = mini_batch.batch.split(micro_batch_size)
            seq_len = micro_batches[0]["input_ids"].shape[1]
            total_seqlen = micro_batch_size * seq_len
        # compute input shapes for pp stages
        n_micro_batch = len(micro_batches)

        forward_backward_func = get_forward_backward_func()

        def loss_func(output, data, meta_info):
            # For memory efficiency
            # We move calculation of entropy to compute_log_probs, forward_only == True
            log_probs = None
            entropy = None
            if isinstance(output, dict):
                log_probs = output["log_probs"]
                if "entropy" in output:
                    entropy = output["entropy"]
            else:
                assert isinstance(output, torch.Tensor)
                log_probs = output

            device = log_probs.device
            metrics = {}
            if forward_only:
                if post_process_fn is None:
                    pass
                    # metrics["logits"] = output
                else:
                    stats = post_process_fn(output, data)
                    metrics.update(stats)
                if not calculate_entropy:
                    return torch.tensor(1.0, device=device), metrics

            responses = data["responses"]
            response_length = responses.size(1)
            response_mask = data["response_mask"].to(bool)
            loss_agg_mode = self.config.loss_agg_mode
            # compute policy loss
            log_prob = log_probs[:, -response_length - 1 : -1].contiguous()
            ret_entropy = None
            stats = {}
            if not forward_only:
                old_log_prob = data["old_log_probs"]
                advantages = data["advantages"]

                entropy_coeff = self.config.entropy_coeff
                loss_agg_mode = self.config.loss_agg_mode

                loss_mode = self.config.policy_loss.get("loss_mode", "vanilla")
                needs_token_entropy = loss_mode in {"entropy_safe_token", "tespo", "entropy_reward_first", "erf"}
                needs_sum_pi_squared = loss_mode in {
                    "simple_intentional_grpo",
                    "intentional_grpo",
                    "vanilla_adaptive_alpha_grpo",
                    "adaptive_alpha_grpo",
                }
                needs_update_sketch = loss_mode in {"state_predictive_grpo", "state_predictive_grpo_normalized"}

                policy_loss_fn = get_policy_loss_fn(loss_mode)

                # Extract pre-computed rollout correction weights if present
                # Weights are computed centrally in trainer and added when algorithm.rollout_is=True
                rollout_is_weights = data.get("rollout_is_weights", None)
                extra_loss_kwargs = {}
                
                # In loss_func, before computing pg_loss:
                if meta_info and meta_info.get("erl_distill"):
                    # Pure SFT cross-entropy: loss = -mean(log_prob * response_mask)
                    distill_coeff = meta_info.get("erl_distill_coeff", 1.0)
                    sft_loss = agg_loss(
                        loss_mat=-log_prob,
                        loss_mask=response_mask,
                        loss_agg_mode=loss_agg_mode,
                    )
                    stats["erl/distill_loss"] = sft_loss.detach().item()
                    return distill_coeff * sft_loss, [stats, None]

                if loss_mode == "seeupo_turn":
                    # These were injected into data.batch by update_policy_seeupo before
                    # calling forward_backward_batch. They are micro-batch-sliced automatically
                    # by mini_batch.batch.split(micro_batch_size) above, so shapes match (mbs, L).
                    extra_loss_kwargs["seeupo_seg_mask"] = data["seeupo_seg_mask"].to(bool)
                    extra_loss_kwargs["seeupo_M"]        = data["seeupo_M"]          # (mbs,)
                
                elif loss_mode in {"empo", "turn_level_ppo"}:
                    # turn_index was included in select_keys and sliced per micro-batch automatically.
                    if "turn_index" in data.keys():
                        extra_loss_kwargs["turn_index"] = data["turn_index"]
                if needs_token_entropy:
                    extra_loss_kwargs["entropy"] = (
                        entropy[:, -response_length - 1 : -1].contiguous() if entropy is not None else None
                    )
                if needs_sum_pi_squared:
                    extra_loss_kwargs["sum_pi_squared"] = data.get("sum_pi_squared", None)
                if needs_update_sketch:
                    extra_loss_kwargs["update_sketch"] = data.get("update_sketch", None)
                    extra_loss_kwargs["state_index"] = data.get("state_index", None)


                pg_loss, pg_metrics = policy_loss_fn(
                    old_log_prob=old_log_prob,
                    log_prob=log_prob,
                    advantages=advantages,
                    response_mask=response_mask,
                    loss_agg_mode=loss_agg_mode,
                    config=self.config,
                    rollout_is_weights=rollout_is_weights,
                    **extra_loss_kwargs,
                )
                stats.update(pg_metrics)

                # Skip if using bypass_mode loss (metrics already computed in pg_metrics)
                rollout_log_prob = data.get("rollout_log_probs", None)
                if loss_mode != "bypass_mode" and rollout_log_prob is not None:
                    # Compute metrics using CURRENT policy π_θ vs π_rollout
                    # Tracks evolving off-policy gap as π_θ updates during mini-batch training
                    from verl.trainer.ppo.rollout_corr_helper import compute_rollout_corr_metrics_from_logprobs

                    rollout_corr_metrics = compute_rollout_corr_metrics_from_logprobs(
                        log_prob=log_prob,
                        rollout_log_prob=rollout_log_prob,
                        response_mask=response_mask,
                    )
                    stats.update(rollout_corr_metrics)

                stats["actor/pg_loss"] = pg_loss.detach().item()
                policy_loss = pg_loss

            if calculate_entropy:
                assert entropy is not None, "calculate_entropy=True but actor output did not contain token entropy"
                entropy = entropy[:, -response_length - 1 : -1].contiguous()
                if not forward_only:
                    entropy_loss = agg_loss(loss_mat=entropy, loss_mask=response_mask, loss_agg_mode=loss_agg_mode)
                    entropy_coeff = meta_info["entropy_coeff"]
                    policy_loss = pg_loss - entropy_coeff * entropy_loss
                else:
                    ret_entropy = entropy

            if forward_only:
                policy_loss = torch.tensor(1.0, device=device)
            else:
                if self.config.use_kl_loss:
                    ref_log_prob = data["ref_log_prob"]
                    # compute kl loss
                    kld = kl_penalty(logprob=log_prob, ref_logprob=ref_log_prob, kl_penalty=self.config.kl_loss_type)
                    kl_loss = agg_loss(loss_mat=kld, loss_mask=response_mask, loss_agg_mode=self.config.loss_agg_mode)

                    policy_loss = policy_loss + kl_loss * self.config.kl_loss_coef
                    metrics["actor/kl_loss"] = kl_loss.detach().item()
                    metrics["actor/kl_coef"] = self.config.kl_loss_coef

                # return loss and stats

            append_to_dict(metrics, stats)
            return policy_loss, [metrics, ret_entropy]

        def forward_step(batch_iter, model, return_schedule_plan: bool = False):
            """
            Args:
                batch_iter: the batch iterator
                model: the model
                return_schedule_plan: whether to return the schedule plan, for 1f1b overlap
            """
            if return_schedule_plan:
                assert self.tf_config.overlap_moe_expert_parallel_comm, (
                    "overlap_moe_expert_parallel_comm must be enabled to return the schedule plan"
                )
                # TODO: Fix this
                assert not calculate_entropy, "calculate_entropy must be disabled to return the schedule plan"
                from megatron.core.models.gpt.gpt_model import GPTModel

                assert isinstance(model, GPTModel), "model must be a GPTModel"
                assert self.use_fused_kernels, "use_fused_kernels must be enabled to return the schedule plan"
                # TODO: support VLM with MoE
                from verl.models.mcore.model_forward_1f1b_overlap import gptmodel_forward_1f1b_overlap

            batch = next(batch_iter)
            batch = batch.to(get_device_id())
            batch = batch.contiguous()

            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"].to(bool)
            position_ids = batch["position_ids"]

            unwrapped_model = unwrap_model(model)
            if hasattr(unwrapped_model, "vp_stage"):
                vp_rank = unwrapped_model.vp_stage
            else:
                vp_rank = 0

            multi_modal_inputs = {}
            if "multi_modal_inputs" in batch:
                from verl.utils.model import extract_multi_modal_inputs

                indices = batch.get("multi_modal_inputs_idx", None)
                multi_modal_inputs = extract_multi_modal_inputs(batch["multi_modal_inputs"], indices)
            responses = batch["responses"]
            response_length = responses.size(1)
            label = position_ids.clone()
            label[:, -response_length - 1 : -1] = responses
            label_mask = attention_mask.clone()
            label_mask[:, : -response_length - 1] = False
            label_mask[:, -1] = False

            if RouterReplayHelper.is_replay_backward_action(self.tf_config, vp_rank):
                router_instance_list = RouterReplayHelper.get_micro_batch_router_list(self.tf_config, vp_rank)
                for router in router_instance_list:
                    router.set_router_replay_action(RouterReplayAction.REPLAY_FORWARD)

            if RouterReplayHelper.is_replay_forward_action(self.tf_config, vp_rank):
                layers_topk_idx = batch["routed_experts"]
                set_router_replay_data(layers_topk_idx, attention_mask, self.tf_config, vp_rank)

            from verl.models.mcore import get_mcore_forward_fn, get_mcore_forward_fused_fn

            if self.use_fused_kernels:
                forward_fn = get_mcore_forward_fused_fn(self.hf_config)
                if return_schedule_plan:
                    forward_fn = gptmodel_forward_1f1b_overlap
                # return dict of [logits, entropy]
                output = forward_fn(
                    model=model,
                    input_ids=input_ids,
                    position_ids=position_ids,
                    attention_mask=attention_mask,
                    labels=label,
                    labels_mask=label_mask,
                    temperature=temperature,
                    multi_modal_inputs=multi_modal_inputs,
                )
            else:
                forward_fn = get_mcore_forward_fn(self.hf_config)

                def logits_processor(logits, label, label_mask):
                    assert logits.shape[:2] == label.shape[:2]
                    assert label.shape == label_mask.shape
                    logits.div_(temperature)
                    ret = {}
                    if calculate_entropy:
                        logits_bak = logits.clone()
                        # # disable the hint until the fused_kernel is optimized for triton>=3.3
                        # logger.warning_once(
                        #     "For memory-efficient computation, enable fused kernels via "
                        #     "`actor_rollout_ref.model.use_fused_kernels=True`. "
                        #     "The current `clone()` operation ensures correctness but increases memory usage."
                        # )
                        entropy = vocab_parallel_entropy(logits)
                        ret["entropy"] = entropy
                    else:
                        logits_bak = logits
                    if self.config.get("calculate_sum_pi_squared", False):
                        sum_pi_squared = vocab_parallel_sum_pi_squared_from_logits(logits)
                        ret["sum_pi_squared"] = sum_pi_squared.masked_fill(~label_mask, 0.0)
                    log_probs = vocab_parallel_log_probs_from_logits(logits_bak, label)
                    log_probs = log_probs.masked_fill(~label_mask, 0.0)
                    ret["log_probs"] = log_probs
                    return ret

                logits_processor_args = {"label": label, "label_mask": label_mask}
                output = forward_fn(
                    model=model,
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    multi_modal_inputs=multi_modal_inputs,
                    logits_processor=logits_processor,
                    logits_processor_args=logits_processor_args,
                    data_format="thd" if self.config.megatron.use_remove_padding else "bshd",
                    mtp_config=None if forward_only else self.mtp_config,
                )

            if forward_only:
                meta_info = None
            else:
                clip_ratio_c = self.config.get("clip_ratio_c", 3.0)
                meta_info = {
                    "clip_ratio": self.config.clip_ratio,
                    "entropy_coeff": self.config.entropy_coeff,
                    "clip_ratio_c": clip_ratio_c,
                }

            if RouterReplayHelper.is_r2_record_action(self.tf_config, vp_rank):
                merge_router_topk_indices(
                    attention_mask, input_ids, self.mini_layer_topk_idx_list, self.tf_config, vp_rank
                )

            if RouterReplayHelper.is_replay_forward_action(self.tf_config, vp_rank):
                router_instance_list = RouterReplayHelper.get_micro_batch_router_list(self.tf_config, vp_rank)
                for router in router_instance_list:
                    router.set_router_replay_action(RouterReplayAction.REPLAY_BACKWARD)

            return output, partial(loss_func, data=batch, meta_info=meta_info)

        # batch should be a list of batches inside micro-batches
        batch_generator = make_batch_generator(micro_batches, vpp_size=len(self.actor_module))

        # TODO: we may use the new schedule instead
        # for flash-attn: (seq_len, batch_size, hidden_size) = (mbs*seq_len, 1, hidden_size)
        if mpu.get_pipeline_model_parallel_world_size() > 1:
            losses_reduced = forward_backward_func(
                forward_step_func=forward_step,
                data_iterator=batch_generator,
                model=self.actor_module,
                num_microbatches=n_micro_batch,
                seq_length=total_seqlen,  # no use when input_shapes was set
                micro_batch_size=1,  # no use when input_shapes was set
                forward_only=forward_only,
            )
        else:
            losses_reduced = forward_backward_func(
                forward_step_func=forward_step,
                data_iterator=batch_generator,
                model=self.actor_module,
                num_microbatches=n_micro_batch,
                seq_length=total_seqlen,  # in use for pp = 1
                micro_batch_size=1,  # in use for pp = 1
                forward_only=forward_only,
            )
        # loss_reduces contains the stats returned from loss_func

        if self.has_multi_modal_inputs:
            data.batch.pop("multi_modal_inputs")
            data.batch.pop("multi_modal_inputs_idx")
            data.non_tensor_batch.pop("multi_modal_inputs")

        losses_reduced = {"output": losses_reduced}
        if use_dynamic_bsz:
            losses_reduced["indices"] = indices
        if RouterReplayHelper.is_r2_record_action(self.tf_config):
            if self.tf_config.virtual_pipeline_model_parallel_size is not None:
                # config = self.actor_module[0].module.module.config
                vp_size = len(self.actor_module)
                microbatch_group_size_per_vp_stage = self.tf_config.microbatch_group_size_per_vp_stage
                bs = n_micro_batch
                losses_reduced["mini_layer_topk_idx_tensor"] = reorder_and_merge_vpp_layers(
                    self.mini_layer_topk_idx_list, bs, vp_size, microbatch_group_size_per_vp_stage
                )
            else:
                losses_reduced["mini_layer_topk_idx_tensor"] = torch.cat(self.mini_layer_topk_idx_list, dim=0)
            self.mini_layer_topk_idx_list = []

        # Collect and pass MTP metrics to losses_reduced
        if not forward_only and self.mtp_config and self.mtp_config.enable_train:
            metrics = get_megatron_mtp_loss(n_micro_batch)
            losses_reduced["mtp_losses"] = [metrics]

        return losses_reduced

    @GPUMemoryLogger(role="megatron actor", logger=logger)
    def update_policy(self, dataloader: Iterable[DataProto], enable_mtp: bool = False) -> dict:
        """Update the policy with an iterator of DataProto

        Args:
            dataloader (Iterable[DataProto]): an iterator over the DataProto that returns by ``make_minibatch_iterator``
                The keys of each data batch is described in the make_minibatch_iterator.

            enable_mtp (bool, optional): whether to enable MTP communication

        Returns:
            Dict: a dictionary containing the statistics. Note that the statistics are only valid in the last pp stage
            and users have to combine the output in each dp rank manually.

        """
        metrics = {}
        for data in dataloader:
            if self.config.router_replay.mode in ["R2", "R3"]:
                RouterReplay.set_global_router_replay_action(RouterReplayAction.REPLAY_FORWARD)
            self.actor_optimizer.zero_grad()
            # use use_contiguous_buffers_in_local_ddp and no overlap_dp_param_comm
            for chunk in self.actor_module:
                # if use distributed optimizer, zero grad buffer will be handled by optimizer
                chunk.zero_grad_buffer()

            loss_mode = self.config.policy_loss.get("loss_mode", "vanilla")
            needs_token_entropy = loss_mode in {"entropy_safe_token", "tespo", "entropy_reward_first", "erf"}
            calculate_entropy = self.config.calculate_entropy or (self.config.entropy_coeff != 0) or needs_token_entropy
            if data.meta_info.get("micro_batch_size", None) is not None:
                micro_batch_size = data.meta_info["micro_batch_size"]
            else:
                micro_batch_size = self.config.ppo_micro_batch_size_per_gpu
            max_token_len = None
            if self.config.use_dynamic_bsz:
                max_token_len = self.config.ppo_max_token_len_per_gpu * self.config.megatron.context_parallel_size
            metric_micro_batch = self.forward_backward_batch(
                data,
                calculate_entropy=calculate_entropy,
                use_dynamic_bsz=self.config.use_dynamic_bsz,
                micro_batch_size=micro_batch_size,
                max_token_len=max_token_len,
                mini_batch_size=self.config.ppo_mini_batch_size,
            )

            mtp_losses = metric_micro_batch.get("mtp_losses", None)
            if mtp_losses is not None:
                # mtp_losses is now in format: [{"mtp_losses/mtp_1_loss": [value1], "mtp_losses/mtp_2_loss": [value2]}]
                for mtp_metrics_dict in mtp_losses:
                    append_to_dict(metrics, mtp_metrics_dict)

            metric_micro_batch = metric_micro_batch["output"]
            for metric in metric_micro_batch:
                # Note that o[0] is metrics, o[1] is entropy, o[2] is response_mask
                append_to_dict(metrics, metric[0])  # append the metric from this micro-batch to global metrics.

            update_successful, grad_norm, num_zeros_in_grad = self.actor_optimizer.step()
            data = {"actor/grad_norm": grad_norm}
            append_to_dict(metrics, data)

            if update_successful:
                # allgather already execute in optimizer.step in new megatron
                pass
            else:
                raise NotImplementedError

            if self.config.router_replay.mode in ["R2", "R3"]:
                RouterReplay.clear_global_router_replay_action()
                RouterReplay.clear_global_indices()

        self.actor_optimizer.zero_grad()
        get_torch_device().empty_cache()
        return metrics

    @GPUMemoryLogger(role="megatron actor", logger=logger)
    def update_policy_seeupo(self, dataloader: Iterable[DataProto]) -> dict:
        """
        SeeUPO sequential policy update — Algorithm 1 from the paper.

        For each mini-batch, performs T optimizer steps in **reverse** turn order.
        After each step, re-forwards the updated model on that turn's tokens
        (no_grad) to compute the sequence-level IS ratio, then updates M_t.

        Requires data.batch to contain:
            input_ids, attention_mask, position_ids, responses,
            old_log_probs   : (B, response_length)
            advantages      : (B, response_length)   — GRPO scalar broadcast
            response_mask   : (B, response_length)
            turn_index      : (B, response_length)   — 0-based turn id, -1 = non-response
        """
        metrics = {}

        for data in dataloader:
            # ── shared forward-pass config ────────────────────────────────────
            loss_mode = self.config.policy_loss.get("loss_mode", "vanilla")
            needs_token_entropy = loss_mode in {"entropy_safe_token", "tespo", "entropy_reward_first", "erf"}
            calculate_entropy = self.config.calculate_entropy or (self.config.entropy_coeff != 0) or needs_token_entropy
            micro_batch_size  = (
                data.meta_info.get("micro_batch_size", None)
                or self.config.ppo_micro_batch_size_per_gpu
            )
            max_token_len = None
            if self.config.use_dynamic_bsz:
                max_token_len = (
                    self.config.ppo_max_token_len_per_gpu
                    * self.config.megatron.context_parallel_size
                )

            cached_multi_modal_inputs = data.non_tensor_batch.get("multi_modal_inputs", None)

            # ── determine number of turns in this batch ───────────────────────
            turn_index    = data.batch["turn_index"]                    # (B, L)
            valid_turns   = turn_index[turn_index >= 0]
            if valid_turns.numel() == 0:
                # No multi-turn data — fall back to vanilla update
                return self.update_policy(dataloader)
            num_turns = int(valid_turns.max().item()) + 1

            # ── global advantage: recover per-sample scalar from GRPO broadcast ──
            # GRPO wrote the same value at every response token; take the mean
            # over response tokens to get back the scalar.
            response_mask = data.batch["response_mask"]                 # (B, L)
            resp_counts   = response_mask.float().sum(-1).clamp(min=1)  # (B,)
            traj_adv      = (
                data.batch["advantages"] * response_mask.float()
            ).sum(-1) / resp_counts                                     # (B,)

            # ── M_{T+1} = global advantage ────────────────────────────────────
            M = traj_adv.clone().cpu()                                  # (B,) on CPU

            # ── temporarily override loss_mode to seeupo_turn ─────────────────
            original_loss_mode = self.config.policy_loss.get("loss_mode", "vanilla")
            # self.config.policy_loss["loss_mode"] = "seeupo_turn"

            # ── reverse turn loop ─────────────────────────────────────────────
            for t in range(num_turns - 1, -1, -1):

                seg_mask = (turn_index == t) & response_mask.bool()     # (B, L)
                if seg_mask.sum() == 0:
                    continue

                # ── inject per-turn tensors into data.batch ───────────────────
                # forward_backward_batch splits data.batch by micro_batch_size,
                # so seg_mask and M will be sliced correctly per micro-batch.
                data.batch["seeupo_seg_mask"] = seg_mask.to(torch.bool)
                data.batch["seeupo_M"]        = M.clone()               # (B,)

                if cached_multi_modal_inputs is not None:
                    data.non_tensor_batch["multi_modal_inputs"] = cached_multi_modal_inputs

                # ── gradient step for turn t ──────────────────────────────────
                if self.config.router_replay.mode in ["R2", "R3"]:
                    RouterReplay.set_global_router_replay_action(RouterReplayAction.REPLAY_FORWARD)

                self.actor_optimizer.zero_grad()
                for chunk in self.actor_module:
                    chunk.zero_grad_buffer()

                metric_micro_batch = self.forward_backward_batch(
                    data,
                    calculate_entropy=calculate_entropy,
                    use_dynamic_bsz=self.config.use_dynamic_bsz,
                    micro_batch_size=micro_batch_size,
                    max_token_len=max_token_len,
                    mini_batch_size=self.config.ppo_mini_batch_size,
                )

                # Collect metrics from this turn's step
                mtp_losses = metric_micro_batch.get("mtp_losses", None)
                if mtp_losses is not None:
                    for mtp_metrics_dict in mtp_losses:
                        append_to_dict(metrics, mtp_metrics_dict)
                for metric in metric_micro_batch.get("output", []):
                    append_to_dict(metrics, metric[0])

                update_successful, grad_norm, _ = self.actor_optimizer.step()
                append_to_dict(metrics, {f"actor/grad_norm_turn{t}": grad_norm})
                if not update_successful:
                    raise RuntimeError(f"SeeUPO optimizer step failed at turn {t}")

                if self.config.router_replay.mode in ["R2", "R3"]:
                    RouterReplay.clear_global_router_replay_action()
                    RouterReplay.clear_global_indices()
               

                if cached_multi_modal_inputs is not None:
                    data.non_tensor_batch["multi_modal_inputs"] = cached_multi_modal_inputs
                # Extract new log_probs from the re-forward output.
                # losses_reduced["output"] is a list of per-micro-batch loss_func returns.
                # In forward_only mode with no post_process_fn, loss_func returns
                # (tensor(1.0), [{}, None]) — the log_probs are NOT in the return value.
                # We need to run a dedicated post_process_fn to capture them.
                # Solution: use a closure to capture log_probs during the re-forward.
                new_log_probs = self._reforward_log_probs(
                    data=data,
                    micro_batch_size=micro_batch_size,
                    max_token_len=max_token_len,
                )                                                        # (B, response_length)

                # ── IS_t: sequence-level ratio for turn t ─────────────────────
                # IS_t[b] = prod_{i in seg_t} π_new[b,i] / π_old[b,i]
                #         = exp( sum_{i in seg_t} (log π_new - log π_old) )
                old_log_probs_tensor = data.batch["old_log_probs"]       # (B, L)
                log_ratio_t = (
                    (new_log_probs - old_log_probs_tensor) * seg_mask.float()
                ).clamp(-20.0, 20.0)                                     # (B, L)
                seq_log_ratio_t = log_ratio_t.sum(dim=-1).cpu()          # (B,)
                IS_t = torch.exp(seq_log_ratio_t)                        # (B,)

                # ── M_t = IS_t × M_{t+1}  (Equation 8) ───────────────────────
                M = IS_t * M                                             # (B,), no grad

            # ── restore loss_mode and clean up injected tensors ───────────────
            # self.config.policy_loss["loss_mode"] = original_loss_mode
            data.batch.pop("seeupo_seg_mask", None)
            data.batch.pop("seeupo_M", None)

        self.actor_optimizer.zero_grad()
        get_torch_device().empty_cache()
        return metrics


    def _reforward_log_probs(
        self,
        data: DataProto,
        micro_batch_size: int,
        max_token_len: int | None,
    ) -> torch.Tensor:
        """
        Run a forward-only pass and return log_probs shape (B, response_length).
        Uses a post_process_fn closure to capture log_probs out of loss_func,
        since forward_only=True does not compute a loss.
        """
        captured = []

        def capture_log_probs(output, data_mb, use_dynamic_bsz=False, indices=None):
            """post_process_fn: runs inside loss_func, captures log_probs."""
            response_length = data_mb["responses"].size(1)
            if isinstance(output, dict):
                lp = output["log_probs"]
            else:
                lp = output
            log_prob = lp[:, -response_length - 1: -1].contiguous()
            captured.append(log_prob.detach().cpu())
            return {}   # return empty metrics dict

        self.forward_backward_batch(
            data,
            forward_only=True,
            post_process_fn=capture_log_probs,
            calculate_entropy=False,
            use_dynamic_bsz=self.config.use_dynamic_bsz,
            micro_batch_size=micro_batch_size,
            max_token_len=max_token_len,
            mini_batch_size=self.config.ppo_mini_batch_size,
        )

        # Broadcast from last PP stage to all ranks (mirrors compute_log_prob)
        if mpu.is_pipeline_last_stage(ignore_virtual=True):
            new_log_probs = torch.cat(captured, dim=0).to(torch.float32)
        else:
            B = data.batch["responses"].shape[0]
            L = data.batch["responses"].shape[1]
            new_log_probs = torch.zeros(B, L, dtype=torch.float32)

        new_log_probs = new_log_probs.to(get_device_id())
        torch.distributed.broadcast(
            tensor=new_log_probs,
            src=mpu.get_pipeline_model_parallel_last_rank(),
            group=mpu.get_pipeline_model_parallel_group(),
            async_op=False,
        )
        return new_log_probs.cpu()                                       # (B, response_length)
