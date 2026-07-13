# Copyright 2025 Bytedance Ltd. and/or its affiliates
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


import torch
import torch.nn.functional as F
from tensordict import TensorDict

from verl.trainer.ppo.core_algos import (
    agg_loss,
    build_value_loss_weights,
    compute_value_loss,
    get_policy_loss_fn,
    kl_penalty,
)
from verl.utils import tensordict_utils as tu
from verl.utils.dataset.dataset_utils import DatasetPadMode
from verl.utils.metric import AggregationType, Metric
from verl.utils.torch_functional import masked_mean, masked_sum
from verl.workers.config import ActorConfig, CriticConfig
from verl.workers.utils.padding import no_padding_2_padding


def sft_loss(config: ActorConfig, model_output, data: TensorDict, dp_group=None):
    pad_mode = tu.get_non_tensor_data(data=data, key="pad_mode", default=DatasetPadMode.NO_PADDING)
    dp_size = data["dp_size"]
    batch_num_tokens = data["batch_num_tokens"]

    log_prob = model_output["log_probs"]

    if pad_mode == DatasetPadMode.NO_PADDING:
        # log_prob and loss mask are nested tensors of shape [bsz, j1]
        # for each sample, loss mask shape is [1, prompt_length + response_length]
        loss_mask = data["loss_mask"]

        log_prob_flatten = log_prob.values()
        loss_mask_flatten = loss_mask.values()

        # left-shift the loss mask by one token to align with log_prob
        loss_mask_flatten = torch.roll(loss_mask_flatten, shifts=-1, dims=0)

        # NOTE: loss is averaged over all tokens in the batch across all data parallel groups,
        # For FSDP backend, the loss is directly used for backward; while for Megatron backend,
        # the loss should be scaled by `num_microbatches` for pp schedule.
        loss = -masked_sum(log_prob_flatten, loss_mask_flatten) / batch_num_tokens * dp_size
    else:
        response_mask = data["response_mask"].to(bool)
        loss = -masked_sum(log_prob, response_mask) / batch_num_tokens * dp_size

    return loss, {}


def _slice_response_from_unpad_output(tensor: torch.Tensor, data: TensorDict) -> torch.Tensor:
    """Slice response from unpad model output.

    Args:
        tensor: model output tensor of shape [bsz, 1]
        data: TensorDict with "prompt_ids", "response_ids", "attention_mask"

    Returns:
        tensor: sliced response tensor of shape [bsz, max_response_len]
    """
    values = tensor.values() if tensor.is_nested else tensor
    prompt_ids = data["prompts"]
    response_ids = data["responses"]
    attention_mask = data["attention_mask"]

    if prompt_ids.is_nested:
        prompt_lens = prompt_ids.offsets().diff()
        response_lens = response_ids.offsets().diff()
        max_response_len = response_ids.offsets().max().item()
    else:
        assert not attention_mask.is_nested
        prompt_lens = attention_mask[:, : prompt_ids.shape[1]].sum(dim=1)
        response_lens = attention_mask[:, prompt_ids.shape[1] :].sum(dim=1)
        max_response_len = response_ids.shape[1]

    sequence_lens = prompt_lens + response_lens
    sequence_offsets = sequence_lens.cumsum(dim=0)
    assert sequence_offsets[-1].item() == values.shape[0]

    response_list = []
    for resp_len, seq_offset in zip(response_lens, sequence_offsets, strict=True):
        pad_size = max_response_len - resp_len
        # left-shift model output by one token for log_probs/values
        response_list.append(F.pad(values[seq_offset - resp_len - 1 : seq_offset - 1], (0, pad_size)))

    output = torch.stack(response_list, dim=0)
    return output

# Add this small helper near agg_loss or in the same utils file
_AGG_LOSS_KEYS = frozenset({
    "dp_size",
    "batch_num_tokens",
    "global_batch_size",
    "loss_scale_factor",
})

def get_agg_loss_kwargs(global_batch_info: dict) -> dict:
    """Filter global_batch_info to only keys accepted by agg_loss."""
    return {k: v for k, v in global_batch_info.items() if k in _AGG_LOSS_KEYS}


def ppo_loss(config: ActorConfig, model_output, data: TensorDict, dp_group=None):
    """Computes ppo loss from model output (log_prob, entropy, values, etc. ) and old_log_probs from data."""
    # ── ERL distillation branch ───────────────────────────────────────
    if data.get("is_erl_distill", None) is not None:
        # log_prob is dense (N, response_length) — output of no_padding_2_padding
        log_prob = no_padding_2_padding(model_output["log_probs"], data)

        # loss_mask in the TensorDict is nested (full sequence).
        # no_padding_2_padding already sliced to the response portion,
        # so we need the dense response-portion of loss_mask to match.
        # Re-use no_padding_2_padding on loss_mask to get the same slice.
        loss_mask_nested = data["loss_mask"]  # nested, full sequence
        # Convert nested loss_mask to dense response-aligned mask the same way
        # log_prob was extracted — i.e. slice out the response tokens.
        loss_mask = no_padding_2_padding(loss_mask_nested, data).bool()

        # Now both are dense (N, max_response_len) — safe to multiply
        # batch_num_tokens = data.get("batch_num_tokens", None)
        batch_num_tokens = tu.get_non_tensor_data(data, key="batch_num_tokens", default=None)

        nll = -(log_prob * loss_mask).sum()
        if batch_num_tokens is not None and batch_num_tokens > 0:
            nll = nll / batch_num_tokens
        else:
            nll = nll / loss_mask.sum().clamp(min=1).float()

        erl_coeff = data.get("erl_distill_coeff", 1.0)
        if isinstance(erl_coeff, torch.Tensor):
            erl_coeff = erl_coeff.item()
        nll = nll * erl_coeff

        metrics = {
            "erl/distill_loss": Metric(value=nll, aggregation=AggregationType.MEAN),
        }
        return nll, metrics
    # ── end ERL distillation branch ───────────────────────────────────


    log_prob = no_padding_2_padding(model_output["log_probs"], data)
    entropy = model_output.get("entropy", None)
    if entropy is not None:
        entropy = no_padding_2_padding(entropy, data)

    # global batch info for loss aggregation
    config.global_batch_info["dp_size"] = data["dp_size"]
    config.global_batch_info["batch_num_tokens"] = data["batch_num_tokens"]
    config.global_batch_info["global_batch_size"] = data["global_batch_size"]
    config.global_batch_info["loss_scale_factor"] = config.loss_scale_factor
    config.global_batch_info["turn_index"]      = data.get("turn_index", None)
    config.global_batch_info["seeupo_seg_mask"] = data.get("seeupo_seg_mask", None)
    config.global_batch_info["seeupo_M"]        = data.get("seeupo_M", None)
    config.global_batch_info["seeupo_IS"]       = data.get("seeupo_IS", None)   # ← add
    config.global_batch_info["seeupo_A"]        = data.get("seeupo_A", None)    # ← add


    # assumes that if any of the global batch info is set, the policy_loss_fn will
    # normalize using dp_size/global_bsz/global_token; in this case, metric aggregation should be SUM
    # to reflect the mean loss over the global batch
    if (
        data["dp_size"] > 1
        or data["batch_num_tokens"] is not None
        or data["global_batch_size"] is not None
        or config.loss_scale_factor is not None
    ):
        metric_aggregation = AggregationType.SUM
    else:
        metric_aggregation = AggregationType.MEAN

    metrics = {}

    response_mask = data["response_mask"].to(bool)
    # compute policy loss
    old_log_prob = data["old_log_probs"]
    advantages = data["advantages"]
    rollout_is_weights = data.get("rollout_is_weights", None)

    loss_agg_mode = config.loss_agg_mode

    loss_mode = config.policy_loss.get("loss_mode", "vanilla")

    policy_loss_fn = get_policy_loss_fn(loss_mode)
    extra_loss_kwargs = {}
    if loss_mode in {"entropy_safe_token", "tespo", "entropy_reward_first", "erf"}:
        extra_loss_kwargs["entropy"] = entropy
    if loss_mode in {
        "simple_intentional_grpo",
        "intentional_grpo",
        "vanilla_adaptive_alpha_grpo",
        "adaptive_alpha_grpo",
        "vanilla_norm_matched_alpha_grpo",
        "norm_matched_alpha_grpo",
    }:
        extra_loss_kwargs["sum_pi_squared"] = data.get("sum_pi_squared", None)
    if loss_mode in {
        "state_predictive_grpo",
        "state_predictive_grpo_normalized",
        "state_agreement_grpo",
        "state_agreement_grpo_normalized",
        "state_xdomain_grpo",
        "state_xdomain_grpo_normalized",
        "state_evidence_joint_grpo",
        "state_agreement_joint_grpo",
        "state_xdomain_joint_grpo",
    }:
        extra_loss_kwargs["update_sketch"] = data.get("update_sketch", None)
        extra_loss_kwargs["state_index"] = data.get("state_index", None)

    pg_loss, pg_metrics = policy_loss_fn(
        old_log_prob=old_log_prob,
        log_prob=log_prob,
        advantages=advantages,
        response_mask=response_mask,
        loss_agg_mode=loss_agg_mode,
        config=config,
        rollout_is_weights=rollout_is_weights,
        **extra_loss_kwargs,
    )

    # AggregationType.MEAN for pg metrics: assumes policy_loss_fn normalizes by local_bsz/local_tokens
    # Ex: in compute_policy_loss_vanilla, pg_metrics are pg_clipfrac, ppo_kl, pg_clipfrac_lower
    pg_metrics = Metric.from_dict(pg_metrics, aggregation=AggregationType.MEAN)

    metrics.update(pg_metrics)
    metrics["actor/pg_loss"] = Metric(value=pg_loss, aggregation=metric_aggregation)
    policy_loss = pg_loss

    # add entropy loss
    if entropy is not None:
        entropy_loss = agg_loss(
            loss_mat=entropy, loss_mask=response_mask, loss_agg_mode=loss_agg_mode,     **get_agg_loss_kwargs(config.global_batch_info),   # ← was **config.global_batch_info

        )
        entropy_coeff = config.entropy_coeff
        policy_loss -= entropy_coeff * entropy_loss
        metrics["actor/entropy_loss"] = Metric(value=entropy_loss, aggregation=metric_aggregation)

    # add kl loss
    if config.use_kl_loss:
        ref_log_prob = data["ref_log_prob"]
        # compute kl loss
        kld = kl_penalty(logprob=log_prob, ref_logprob=ref_log_prob, kl_penalty=config.kl_loss_type)
        kl_loss = agg_loss(
            loss_mat=kld, loss_mask=response_mask, loss_agg_mode=config.loss_agg_mode,     **get_agg_loss_kwargs(config.global_batch_info),   # ← was **config.global_batch_info

        )

        policy_loss += kl_loss * config.kl_loss_coef
        metrics["kl_loss"] = Metric(value=kl_loss, aggregation=metric_aggregation)
        metrics["kl_coef"] = config.kl_loss_coef

    return policy_loss, metrics


def value_loss(config: CriticConfig, model_output, data: TensorDict, dp_group=None):
    """value loss

    Args:
        config: CriticConfig
        model_output: model output from the model
        data: the input to the model
        dp_group: data paralle group

    Returns:
        value loss
    """
    vpreds = _slice_response_from_unpad_output(model_output["values"], data)  # (bsz, response_length)

    values = data["values"]
    returns = data["returns"]
    response_mask = data["response_mask"].to(bool)
    value_loss_weights, weight_metrics = build_value_loss_weights(
        response_mask=response_mask,
        mode=getattr(config, "value_loss_weight_mode", "none"),
        turn_index=data.get("turn_index", None),
        old_log_probs=data.get("old_log_probs", None),
        normalize=getattr(config, "value_loss_weight_normalize", True),
        clip_min=getattr(config, "value_loss_weight_clip_min", None),
        clip_max=getattr(config, "value_loss_weight_clip_max", None),
        clip_renormalize=getattr(config, "value_loss_weight_clip_renormalize", True),
        rho=getattr(config, "value_loss_weight_rho", 1.0),
        alpha=getattr(config, "value_loss_weight_alpha", 1.0),
    )

    vf_loss, vf_clipfrac = compute_value_loss(
        vpreds=vpreds,
        values=values,
        returns=returns,
        response_mask=response_mask,
        cliprange_value=config.cliprange_value,
        loss_agg_mode=config.loss_agg_mode,
        value_loss_weights=value_loss_weights,
    )

    metrics = {}

    metrics.update(
        {
            "critic/vf_loss": vf_loss.detach().item(),
            "critic/vf_clipfrac": vf_clipfrac.detach().item(),
            "critic/vpred_mean": masked_mean(vpreds, response_mask).detach().item(),
        }
    )
    if value_loss_weights is not None:
        metrics.update(
            {
                f"critic/value_loss_weight/{name}": value.detach().item()
                for name, value in weight_metrics.items()
            }
        )

    return vf_loss, metrics
