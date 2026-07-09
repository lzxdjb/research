# Copyright 2024 Bytedance Ltd. and/or its affiliates
# Copyright 2023-2024 SGLang Team
# Copyright 2025 ModelBest Inc. and/or its affiliates
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
PPO Trainer with Ray-based single controller.
This trainer supports model-agonistic model initialization with huggingface
"""

import json
import os
import uuid
from collections import defaultdict
from copy import deepcopy
from pprint import pprint
from typing import Any, Optional

import numpy as np
import torch
from omegaconf import OmegaConf, open_dict
from torch.utils.data import Dataset, Sampler
from torchdata.stateful_dataloader import StatefulDataLoader
from tqdm import tqdm

from verl import DataProto
from verl.checkpoint_engine import CheckpointEngineManager
from verl.experimental.dataset.sampler import AbstractCurriculumSampler
from verl.protocol import pad_dataproto_to_divisor, unpad_dataproto
from verl.single_controller.ray import RayClassWithInitArgs, RayWorkerGroup, ResourcePoolManager
from verl.single_controller.ray.base import create_colocated_worker_cls
from verl.trainer.config import AlgoConfig
from verl.trainer.ppo import core_algos
from verl.trainer.ppo.core_algos import AdvantageEstimator, agg_loss
from verl.trainer.ppo.metric_utils import (
    compute_data_metrics,
    compute_reward_extra_metrics,
    compute_throughout_metrics,
    compute_timing_metrics,
    compute_variance_proxy_metrics,
    process_validation_metrics,
)
from verl.trainer.ppo.reward import extract_reward
from verl.trainer.ppo.utils import Role, WorkerType, need_critic, need_reference_policy, need_reward_model
from verl.utils import tensordict_utils as tu
from verl.utils.checkpoint.checkpoint_manager import find_latest_ckpt_path, should_save_ckpt_esi
from verl.utils.config import omega_conf_to_dataclass
from verl.utils.debug import marked_timer
from verl.utils.import_utils import load_class_from_fqn
from verl.utils.metric import reduce_metrics
from verl.utils.py_functional import rename_dict
from verl.utils.rollout_skip import RolloutSkip
from verl.utils.seqlen_balancing import calculate_workload, get_seqlen_balanced_partitions, log_seqlen_unbalance
from verl.utils.torch_functional import masked_mean
from verl.utils.tracking import ValidationGenerationsLogger
from verl.workers.config import FSDPEngineConfig
from verl.workers.utils.padding import left_right_2_no_padding, no_padding_2_padding
from tensordict import TensorDict


# verl/trainer/ppo/erl_distill.py  — full replacement

import numpy as np
import torch
from tensordict import TensorDict

from verl import DataProto
from verl.utils.tensordict_utils import NonTensorData  # or wherever tu lives


from collections import defaultdict

def _geomean_prob(log_probs_1d: torch.Tensor) -> float:
    n = log_probs_1d.numel()
    if n == 0:
        return 0.0
    return float(log_probs_1d.sum()) / n   # log-domain mean → convert outside
def _get_item(container, i: int):
    """Safely index a LinkedList or plain list/tuple."""
    try:
        return container[i]
    except (IndexError, KeyError, TypeError):
        return None
def _compute_ig_rewards_cpu(
    log_probs:       torch.Tensor,   # (B, response_length)
    extra_log_probs: torch.Tensor,   # (E, response_length)
    meta:            list,           # [{"sample_idx": i, "turn_idx": t, "n_ans": n}, ...]
    turn_segs_ll,
    reward_extra_ll,
    response_length: int,
) -> torch.Tensor:
    import math
    n_original = log_probs.shape[0]
    device     = log_probs.device

    probs: dict[int, dict[int, float]] = defaultdict(dict)

    for row_idx, m in enumerate(meta):
        i     = m["sample_idx"]
        t_idx = m["turn_idx"]
        n_ans = m["n_ans"]
        n_ans_clamped = min(n_ans, response_length)

        ans_lp = extra_log_probs[row_idx, -n_ans_clamped:]
        # geometric mean in probability space = arithmetic mean in log space → exp
        lp_mean = float(ans_lp.sum()) / max(n_ans_clamped, 1)
        probs[i][t_idx] = math.exp(lp_mean)

    ig_token_rewards = torch.zeros(
        n_original, response_length, dtype=torch.float32, device=device
    )

    for i in range(n_original):
        segs_entry  = _get_item(turn_segs_ll, i)
        extra_entry = _get_item(reward_extra_ll, i)

        segs      = segs_entry if segs_entry is not None else []
        outcome_r = float(extra_entry.get("score", 0.0)) if extra_entry is not None else 0.0

        prob_dict    = probs.get(i, {})
        sorted_turns = sorted(prob_dict.keys())
        T            = len(sorted_turns)
        if T == 0:
            continue

        prev_prob = 0.0
        for rank, t_idx in enumerate(sorted_turns):
            prob = prob_dict[t_idx]
            seg  = next((s for s in segs if int(s["turn"]) - 1 == t_idx), None)
            if seg is None:
                prev_prob = prob
                continue

            last_tok = int(seg["resp_end"]) - 1
            if not (0 <= last_tok < response_length):
                prev_prob = prob
                continue

            if rank < T - 1:
                ig_token_rewards[i, last_tok] = prob - prev_prob
            else:
                ig_token_rewards[i, last_tok] = outcome_r

            prev_prob = prob

    return ig_token_rewards

def build_distill_batch(
    rollout_batch: DataProto,
    prompt_length: int,
    response_length: int,
    dp_size: int = 1
) -> TensorDict | None:
    """
    Build a TensorDict for the ERL distillation pass from a rollout batch.

    Filters to samples where reflection was triggered AND second attempt succeeded.
    For each qualifying sample, constructs:
        input_ids      : [x; y2]  — original prompt + second-attempt tokens
        loss_mask      : 0 for x tokens, 1 for y2 tokens  (SFT on y2 only)
        attention_mask : 1 for all real tokens, 0 for padding
        position_ids   : incremental
        advantages     : 1.0  (unused by distill loss, but keeps shapes consistent)
        old_log_probs  : 0.0  (same)
        temperature    : 1.0

    Returns None if no qualifying samples.
    """

    non_tb = rollout_batch.non_tensor_batch
    batch  = rollout_batch.batch

    reflected       = non_tb.get("erl_reflected",      np.zeros(len(rollout_batch), dtype=bool))
    second_rewards  = non_tb.get("erl_second_reward",  np.zeros(len(rollout_batch), dtype=float))
    
    # DEBUG: make first reflected sample succeed
    for i in range(len(second_rewards)):
        if reflected[i]:
            second_rewards[i] = 1.0

    qualifying = [
        i for i in range(len(rollout_batch))
        if reflected[i] and float(second_rewards[i]) > 0.0
    ]
    if not qualifying:
        return None

    prompt_end_idxs     = non_tb.get("erl_distill_prompt_end_idx",  np.zeros(len(rollout_batch), dtype=int))
    response_start_idxs = non_tb.get("erl_distill_response_start",  np.zeros(len(rollout_batch), dtype=int))

    samples = []
    for i in qualifying:
        # Full response tensor (right-padded)
        resp_ids  = batch["responses"][i]        # (response_length,)
        resp_mask = batch["response_mask"][i]    # (response_length,)  1=LLM, 0=obs/pad

        # y2 = second-attempt tokens: response positions >= r_start with mask=1
        r_start = int(response_start_idxs[i])
        y2_select = resp_mask.bool().clone()
        y2_select[:r_start] = False
        y2_ids = resp_ids[y2_select]

        if len(y2_ids) == 0:
            continue

        # x = original prompt token ids (before reflection was injected)
        # input_ids is [prompt | response] left-padded prompt
        input_ids_full = batch["input_ids"][i]   # (prompt_length + response_length,)
        # The original prompt occupies the last `p_end` tokens of the prompt block
        p_end = int(prompt_end_idxs[i])
        # prompt block is left-padded to prompt_length
        x_ids = input_ids_full[:prompt_length]   # take the prompt block
        # trim left padding, keep only the real p_end prompt tokens
        x_ids = x_ids[-p_end:] if p_end > 0 else x_ids[:0]

        samples.append({"x_ids": x_ids, "y2_ids": y2_ids})

    if not samples:
        return None

    # Replace the stacking block with this:

    rows_input_ids      = []
    rows_prompts        = []   # x only (nested)
    rows_responses      = []   # y2 only (nested, also kept dense as responses_t)
    rows_loss_mask      = []
    rows_attention_mask = []
    rows_position_ids   = []
    x_lens  = []
    y2_lens = []

    for s in samples:
        x  = s["x_ids"]   # 1-D, already trimmed (no padding)
        y2 = s["y2_ids"]  # 1-D, no padding

        x_len_i  = len(x)
        y2_len_i = len(y2)
        x_lens.append(x_len_i)
        y2_lens.append(y2_len_i)

        # Left-pad x to prompt_length, right-pad y2 to response_length
        pad_x  = prompt_length  - x_len_i
        pad_y  = response_length - y2_len_i
        x_padded  = torch.cat([x.new_zeros(pad_x),  x])
        y2_padded = torch.cat([y2, y2.new_zeros(pad_y)])

        full_ids  = torch.cat([x_padded, y2_padded])

        lm_x  = torch.zeros(prompt_length,  dtype=torch.long)
        lm_y2 = torch.cat([torch.ones(y2_len_i,  dtype=torch.long),
                            torch.zeros(pad_y,    dtype=torch.long)])
        full_lm = torch.cat([lm_x, lm_y2])

        attn_x  = (x_padded  != 0).long()
        attn_y2 = (y2_padded != 0).long()
        full_attn = torch.cat([attn_x, attn_y2])

        full_pos = torch.arange(prompt_length + response_length, dtype=torch.long)

        rows_input_ids.append(full_ids)
        rows_prompts.append(x_padded)          # dense, will also make nested version
        rows_responses.append(y2_padded)
        rows_loss_mask.append(full_lm)
        rows_attention_mask.append(full_attn)
        rows_position_ids.append(full_pos)

    N = len(samples)

    input_ids_t      = torch.stack(rows_input_ids)        # (N, prompt_len+resp_len)
    prompts_t        = torch.stack(rows_prompts)           # (N, prompt_len)
    responses_t      = torch.stack(rows_responses)         # (N, resp_len)
    loss_mask_t      = torch.stack(rows_loss_mask)         # (N, prompt_len+resp_len)
    attention_mask_t = torch.stack(rows_attention_mask)    # (N, prompt_len+resp_len)
    position_ids_t   = torch.stack(rows_position_ids)      # (N, prompt_len+resp_len)

    # ── Convert to nested tensors (remove padding) ───────────────────────
    def _to_nested(dense: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        seqs = [dense[i][mask[i].bool()] for i in range(dense.shape[0])]
        return torch.nested.as_nested_tensor(seqs, layout=torch.jagged,
                                             device=dense.device)

    # prompt attention mask = left portion of full attention mask
    prompt_attn = attention_mask_t[:, :prompt_length]
    resp_attn   = attention_mask_t[:, prompt_length:]

    input_ids_nested    = _to_nested(input_ids_t,    attention_mask_t)
    prompts_nested      = _to_nested(prompts_t,      prompt_attn)
    responses_nested    = _to_nested(responses_t,    resp_attn)
    loss_mask_nested    = _to_nested(loss_mask_t,    attention_mask_t)
    position_ids_nested = _to_nested(position_ids_t, attention_mask_t)

    advantages_t    = torch.ones( N, response_length, dtype=torch.float32)
    old_log_probs_t = torch.zeros(N, response_length, dtype=torch.float32)
    temperature_t   = torch.ones( N,                  dtype=torch.float32)

    td = TensorDict(
        {
            # Nested (remove-padding) tensors consumed by forward_step
            "input_ids":       input_ids_nested,
            "prompts":         prompts_nested,      # ← fixes KeyError
            "responses":       responses_nested,
            "loss_mask":       loss_mask_nested,
            "position_ids":    position_ids_nested,
            # Dense tensors
            "attention_mask":  attention_mask_t,
            "advantages":      advantages_t,
            "old_log_probs":   old_log_probs_t,
            "temperature":     temperature_t,
            "is_erl_distill":  torch.ones(N, dtype=torch.bool),
        },
        batch_size=[N],
    )
    
    # After building all the row lists (rows_input_ids, rows_prompts, etc.)
    # ── Pad to multiple of dp_size at the LIST level, before any stacking ──
    # breakpoint()
    if dp_size > 1:
        remainder = len(rows_input_ids) % dp_size
        if remainder != 0:
            pad_n = dp_size - (len(samples) % dp_size)
            for _ in range(pad_n):
                # Repeat last row, but zero out loss_mask so no gradient
                rows_input_ids.append(rows_input_ids[-1].clone())
                rows_prompts.append(rows_prompts[-1].clone())
                rows_responses.append(rows_responses[-1].clone())
                rows_loss_mask.append(torch.zeros_like(rows_loss_mask[-1]))  # zeroed!
                rows_attention_mask.append(rows_attention_mask[-1].clone())
                rows_position_ids.append(rows_position_ids[-1].clone())
                x_lens.append(x_lens[-1])
                y2_lens.append(y2_lens[-1])

    N = len(rows_input_ids)  # move N down here, after potential padding

    input_ids_t      = torch.stack(rows_input_ids)
    prompts_t        = torch.stack(rows_prompts)
    responses_t      = torch.stack(rows_responses)
    loss_mask_t      = torch.stack(rows_loss_mask)
    attention_mask_t = torch.stack(rows_attention_mask)
    position_ids_t   = torch.stack(rows_position_ids)

    # ── Convert to nested tensors ──────────────────────────────────────────
    def _to_nested(dense: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        seqs = [dense[i][mask[i].bool()] for i in range(dense.shape[0])]
        return torch.nested.as_nested_tensor(seqs, layout=torch.jagged,
                                             device=dense.device)

    prompt_attn = attention_mask_t[:, :prompt_length]
    resp_attn   = attention_mask_t[:, prompt_length:]

    input_ids_nested    = _to_nested(input_ids_t,    attention_mask_t)
    prompts_nested      = _to_nested(prompts_t,      prompt_attn)
    responses_nested    = _to_nested(responses_t,    resp_attn)
    loss_mask_nested    = _to_nested(loss_mask_t,    attention_mask_t)
    position_ids_nested = _to_nested(position_ids_t, attention_mask_t)

    advantages_t    = torch.ones( N, response_length, dtype=torch.float32)
    old_log_probs_t = torch.zeros(N, response_length, dtype=torch.float32)
    temperature_t   = torch.ones( N,                  dtype=torch.float32)

    td = TensorDict(
        {
            "input_ids":      input_ids_nested,
            "prompts":        prompts_nested,
            "responses":      responses_nested,
            "loss_mask":      loss_mask_nested,
            "position_ids":   position_ids_nested,
            "attention_mask": attention_mask_t,
            "advantages":     advantages_t,
            "old_log_probs":  old_log_probs_t,
            "temperature":    temperature_t,
            "is_erl_distill": torch.ones(N, dtype=torch.bool),
        },
        batch_size=[N],
    )

    return td

def apply_kl_penalty(data: DataProto, kl_ctrl: core_algos.AdaptiveKLController, kl_penalty="kl"):
    """Apply KL penalty to the token-level rewards.

    This function computes the KL divergence between the reference policy and current policy,
    then applies a penalty to the token-level rewards based on this divergence.

    Args:
        data (DataProto): The data containing batched model outputs and inputs.
        kl_ctrl (core_algos.AdaptiveKLController): Controller for adaptive KL penalty.
        kl_penalty (str, optional): Type of KL penalty to apply. Defaults to "kl".

    Returns:
        tuple: A tuple containing:
            - The updated data with token-level rewards adjusted by KL penalty
            - A dictionary of metrics related to the KL penalty
    """
    response_mask = data.batch["response_mask"]
    token_level_scores = data.batch["token_level_scores"]
    batch_size = data.batch.batch_size[0]

    # compute kl between ref_policy and current policy
    # When apply_kl_penalty, algorithm.use_kl_in_reward=True, so the reference model has been enabled.
    kld = core_algos.kl_penalty(
        data.batch["old_log_probs"], data.batch["ref_log_prob"], kl_penalty=kl_penalty
    )  # (batch_size, response_length)
    kld = kld * response_mask
    beta = kl_ctrl.value

    token_level_rewards = token_level_scores - beta * kld

    current_kl = masked_mean(kld, mask=response_mask, axis=-1)  # average over sequence
    current_kl = torch.mean(current_kl, dim=0).item()

    # according to https://github.com/huggingface/trl/blob/951ca1841f29114b969b57b26c7d3e80a39f75a0/trl/trainer/ppo_trainer.py#L837
    kl_ctrl.update(current_kl=current_kl, n_steps=batch_size)
    data.batch["token_level_rewards"] = token_level_rewards

    metrics = {"actor/reward_kl_penalty": current_kl, "actor/reward_kl_penalty_coeff": beta}

    return data, metrics


def compute_response_mask(data: DataProto):
    """Compute the attention mask for the response part of the sequence.

    This function extracts the portion of the attention mask that corresponds to the model's response,
    which is used for masking computations that should only apply to response tokens.

    Args:
        data (DataProto): The data containing batched model outputs and inputs.

    Returns:
        torch.Tensor: The attention mask for the response tokens.
    """
    responses = data.batch["responses"]
    response_length = responses.size(1)
    attention_mask = data.batch["attention_mask"]
    return attention_mask[:, -response_length:]

def _build_turn_index(
    turn_segs_batch: np.ndarray,   # object array (B,), each element = list of dicts
    response_mask: torch.Tensor,   # (B, response_length)
) -> torch.Tensor:
    """
    Returns a (B, response_length) int32 tensor.
    Value = 0-based turn index for response tokens, -1 for non-response tokens.
    """
    B, L = response_mask.shape
    turn_index = torch.full((B, L), fill_value=-1, dtype=torch.int32)

    for b in range(B):
        segs = turn_segs_batch[b]
        if not segs:
            continue
        # resp_positions[i] = the actual column index in [0, L) of the i-th response token
        resp_positions = response_mask[b].nonzero(as_tuple=True)[0]  # (num_resp_tokens,)

        for seg in segs:
            t  = seg["turn"] - 1                      # convert to 0-based
            rs = seg["resp_start"]
            re = min(seg["resp_end"], len(resp_positions))
            if rs >= re:
                continue
            token_cols = resp_positions[rs:re]        # actual column indices
            turn_index[b, token_cols] = t

    return turn_index

def compute_advantage(
    data: DataProto,
    adv_estimator: AdvantageEstimator,
    gamma: float = 1.0,
    lam: float = 1.0,
    num_repeat: int = 1,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
) -> DataProto:
    """Compute advantage estimates for policy optimization.

    This function computes advantage estimates using various estimators like GAE, GRPO, REINFORCE++, etc.
    The advantage estimates are used to guide policy optimization in RL algorithms.

    Args:
        data (DataProto): The data containing batched model outputs and inputs.
        adv_estimator (AdvantageEstimator): The advantage estimator to use (e.g., GAE, GRPO, REINFORCE++).
        gamma (float, optional): Discount factor for future rewards. Defaults to 1.0.
        lam (float, optional): Lambda parameter for GAE. Defaults to 1.0.
        num_repeat (int, optional): Number of times to repeat the computation. Defaults to 1.
        norm_adv_by_std_in_grpo (bool, optional): Whether to normalize advantages by standard deviation in
            GRPO. Defaults to True.
        config (dict, optional): Configuration dictionary for algorithm settings. Defaults to None.

    Returns:
        DataProto: The updated data with computed advantages and returns.
    """
    # Back-compatible with trainers that do not compute response mask in fit
    if "response_mask" not in data.batch.keys():
        data.batch["response_mask"] = compute_response_mask(data)
    if "turn_segs" in data.non_tensor_batch and "turn_index" not in data.batch:
        data.batch["turn_index"] = _build_turn_index(
            turn_segs_batch=data.non_tensor_batch["turn_segs"],
            response_mask=data.batch["response_mask"],
        )
    # prepare response group
    if adv_estimator == AdvantageEstimator.GAE:
        # Compute advantages and returns using Generalized Advantage Estimation (GAE)
        advantages, returns = core_algos.compute_gae_advantage_return(
            token_level_rewards=data.batch["token_level_rewards"],
            values=data.batch["values"],
            response_mask=data.batch["response_mask"],
            gamma=gamma,
            lam=lam,
        )
        if config.get("turn_level_value", False) and "turn_index" in data.batch:
            advantages, returns = core_algos.compute_turn_level_value_advantage_return(
                returns=returns,
                values=data.batch["values"],
                response_mask=data.batch["response_mask"],
                turn_index=data.batch["turn_index"],
                anchor=config.get("turn_level_value_anchor", "first"),
            )
        data.batch["advantages"] = advantages
        data.batch["returns"] = returns
        if config.get("use_pf_ppo", False):
            data = core_algos.compute_pf_ppo_reweight_data(
                data,
                config.pf_ppo.get("reweight_method"),
                config.pf_ppo.get("weight_pow"),
            )
    elif adv_estimator == AdvantageEstimator.GRPO:
        # Initialize the mask for GRPO calculation
        grpo_calculation_mask = data.batch["response_mask"]

        # Call compute_grpo_outcome_advantage with parameters matching its definition
        advantages, returns = core_algos.compute_grpo_outcome_advantage(
            token_level_rewards=data.batch["token_level_rewards"],
            response_mask=grpo_calculation_mask,
            index=data.non_tensor_batch["uid"],
            norm_adv_by_std_in_grpo=norm_adv_by_std_in_grpo,
        )
        data.batch["advantages"] = advantages
        data.batch["returns"] = returns
        
        # NEW: build turn_index tensor for SeeUPO loss

        if "turn_segs" in data.non_tensor_batch:
            data.batch["turn_index"] = _build_turn_index(
            turn_segs_batch=data.non_tensor_batch["turn_segs"],
            response_mask=data.batch["response_mask"],
        )
            
    elif adv_estimator == AdvantageEstimator.grpo_erl_split:
        # Initialize the mask for GRPO calculation
        grpo_calculation_mask = data.batch["response_mask"]

        # Call compute_grpo_outcome_advantage with parameters matching its definition
         # ── Detect whether any ERL split samples are present ──────────────
        # extra = data.non_tensor_batch.get("extra_fields", {})
        erl_split_idx    = data.non_tensor_batch.get("erl_split_idx",    None)
        erl_reflected    = data.non_tensor_batch.get("erl_reflected",    None)
        erl_first_reward = data.non_tensor_batch.get("erl_first_reward",  None)
        erl_second_reward= data.non_tensor_batch.get("erl_second_reward", None)

        split_idx_arr = np.array(
        [int(v) if v is not None else 0 for v in erl_split_idx],
        dtype=np.int64,
    )

        has_erl_split = (
            erl_split_idx is not None
            and erl_reflected is not None
            and np.any(erl_reflected)        # at least one sample actually reflected
        )
        # breakpoint()

        if has_erl_split:
            advantages, returns = core_algos.compute_grpo_erl_split_advantage(
                token_level_rewards=data.batch["token_level_rewards"],
                response_mask=grpo_calculation_mask,
                index=data.non_tensor_batch["uid"],
                erl_split_idx=split_idx_arr,
                erl_first_reward=np.array(erl_first_reward,  dtype=np.float32),
                erl_second_reward=np.array(erl_second_reward, dtype=np.float32),
                erl_reflected=np.array(erl_reflected,    dtype=bool),
                norm_adv_by_std_in_grpo=norm_adv_by_std_in_grpo,
            )
        else:
            advantages, returns = core_algos.compute_grpo_outcome_advantage(
                token_level_rewards=data.batch["token_level_rewards"],
                response_mask=grpo_calculation_mask,
                index=data.non_tensor_batch["uid"],
                norm_adv_by_std_in_grpo=norm_adv_by_std_in_grpo,
            )
            
        data.batch["advantages"] = advantages
        data.batch["returns"] = returns

            # NEW: build turn_index tensor for SeeUPO loss

        if "turn_segs" in data.non_tensor_batch:
                data.batch["turn_index"] = _build_turn_index(
                turn_segs_batch=data.non_tensor_batch["turn_segs"],
                response_mask=data.batch["response_mask"],
            )

    elif adv_estimator == AdvantageEstimator.GIGPO:
        if "turn_segs" in data.non_tensor_batch:
            data.batch["turn_index"] = _build_turn_index(
            turn_segs_batch=data.non_tensor_batch["turn_segs"],
            response_mask=data.batch["response_mask"],
            )
        assert "turn_index" in data.batch, (
            "GiGPO requires turn_index in batch. "
            "Set use_seeupo=True in multi_turn config to build it."
        )
        omega = config.get("gigpo_omega", 1.0) if config else 1.0
        gigpo_gamma = config.get("gigpo_gamma", gamma) if config else gamma

        advantages, returns = core_algos.compute_gigpo_advantage(
            token_level_rewards=data.batch["token_level_rewards"],
            response_mask=data.batch["response_mask"],
            turn_index=data.batch["turn_index"],
            index=data.non_tensor_batch["uid"],
            omega=omega,
            gamma=gigpo_gamma,
            norm_adv_by_std=norm_adv_by_std_in_grpo,
            config=config,
        )
        data.batch["advantages"] = advantages
        data.batch["returns"]    = returns

    elif adv_estimator == AdvantageEstimator.IGPO:
        if "turn_segs" in data.non_tensor_batch:
            data.batch["turn_index"] = _build_turn_index(
                turn_segs_batch=data.non_tensor_batch["turn_segs"],
                response_mask=data.batch["response_mask"],
            )
        assert "turn_index" in data.batch, (
            "IGPO requires turn_index. Set use_seeupo=True in multi_turn config."
        )
        # Use ig_token_rewards (built in compute_log_prob).
        # Falls back to standard token_level_rewards if not present.
        ig_rewards = data.batch.get("ig_token_rewards", data.batch["token_level_rewards"])
        igpo_gamma = config.get("igpo_gamma", gamma) if config else gamma
    
        advantages, returns = core_algos.compute_igpo_advantage(
            token_level_rewards=ig_rewards,
            response_mask=data.batch["response_mask"],
            turn_index=data.batch["turn_index"],
            index=data.non_tensor_batch["uid"],
            gamma=igpo_gamma,
            norm_adv_by_std=norm_adv_by_std_in_grpo,
            config=config,
        )
        data.batch["advantages"] = advantages
        data.batch["returns"]    = returns

    else:
        # handle all other adv estimator type other than GAE and GRPO
        adv_estimator_fn = core_algos.get_adv_estimator_fn(adv_estimator)
        adv_kwargs = {
            "token_level_rewards": data.batch["token_level_rewards"],
            "response_mask": data.batch["response_mask"],
            "config": config,
        }
        if "responses" in data.batch:
            adv_kwargs["responses"] = data.batch["responses"]
        if "old_log_probs" in data.batch:
            adv_kwargs["old_log_probs"] = data.batch["old_log_probs"]
        if "sum_pi_squared" in data.batch:
            adv_kwargs["sum_pi_squared"] = data.batch["sum_pi_squared"]
        if "update_sketch" in data.batch:
            adv_kwargs["update_sketch"] = data.batch["update_sketch"]
        if "uid" in data.non_tensor_batch:  # optional
            adv_kwargs["index"] = data.non_tensor_batch["uid"]
        if "reward_baselines" in data.batch:  # optional
            adv_kwargs["reward_baselines"] = data.batch["reward_baselines"]
        # GDPO: pass raw data for per-dimension reward extraction
        if adv_estimator in (AdvantageEstimator.GDPO, "gdpo"):
            adv_kwargs["non_tensor_batch"] = data.non_tensor_batch
            adv_kwargs["batch"] = data.batch
        # Add sum_pi_squared for Optimal Token Baseline
        if adv_estimator in (AdvantageEstimator.OPTIMAL_TOKEN_BASELINE, AdvantageEstimator.TIR_OPTIMAL_TOKEN_BASELINE):
            # Check if sum_pi_squared is available
            assert "sum_pi_squared" in data.batch, (
                "Step-dependent optimal baseline requires sum_pi_squared from actor. "
                "Please set actor.calculate_sum_pi_squared=True in config."
            )
            adv_kwargs["sum_pi_squared"] = data.batch["sum_pi_squared"]
            # Get pre-computed rollout IS weights if available
            rollout_is_weights = data.batch.get("rollout_is_weights", None)
            adv_kwargs["rollout_is_weights"] = rollout_is_weights
        if adv_estimator in (AdvantageEstimator.MSE_GATE, "mse_gate"):
            adv_kwargs["return_metrics"] = True
            if "old_log_probs" in data.batch:
                adv_kwargs["old_log_probs"] = data.batch["old_log_probs"]
            if "sum_pi_squared" in data.batch:
                adv_kwargs["sum_pi_squared"] = data.batch["sum_pi_squared"]
        if adv_estimator in (
            AdvantageEstimator.MULTI_DOMAIN_BOS_GRPO,
            AdvantageEstimator.SHARED_PRIVATE_MULTI_DOMAIN_BOS_GRPO,
            "multi_domain_bos_grpo",
            "md_bos_grpo",
            "shared_private_multi_domain_bos_grpo",
            "shared_private_md_bos_grpo",
            "sp_md_bos_grpo",
            AdvantageEstimator.SNR_MULTI_DOMAIN_GRPO,
            "snr_multi_domain_grpo",
            "snr_md_grpo",
        ):
            adv_kwargs["non_tensor_batch"] = data.non_tensor_batch

        # calculate advantage estimator
        adv_output = adv_estimator_fn(**adv_kwargs)
        if isinstance(adv_output, tuple) and len(adv_output) == 3:
            advantages, returns, adv_metrics = adv_output
            data.meta_info["adv_metrics"] = adv_metrics
        else:
            advantages, returns = adv_output
        data.batch["advantages"] = advantages
        data.batch["returns"] = returns
    return data


class RayPPOTrainer:
    """Distributed PPO trainer using Ray for scalable reinforcement learning.

    This trainer orchestrates distributed PPO training across multiple nodes and GPUs,
    managing actor rollouts, critic training, and reward computation with Ray backend.
    Supports various model architectures including FSDP, Megatron, vLLM, and SGLang integration.
    """

    # TODO: support each role have individual ray_worker_group_cls,
    # i.e., support different backend of different role
    def __init__(
        self,
        config,
        tokenizer,
        role_worker_mapping: dict[Role, WorkerType],
        resource_pool_manager: ResourcePoolManager,
        ray_worker_group_cls: type[RayWorkerGroup] = RayWorkerGroup,
        processor=None,
        train_dataset: Optional[Dataset] = None,
        val_dataset: Optional[Dataset] = None,
        collate_fn=None,
        train_sampler: Optional[Sampler] = None,
        device_name=None,
    ):
        """
        Initialize distributed PPO trainer with Ray backend.
        Note that this trainer runs on the driver process on a single CPU/GPU node.

        Args:
            config: Configuration object containing training parameters.
            tokenizer: Tokenizer used for encoding and decoding text.
            role_worker_mapping (dict[Role, WorkerType]): Mapping from roles to worker classes.
            resource_pool_manager (ResourcePoolManager): Manager for Ray resource pools.
            ray_worker_group_cls (RayWorkerGroup, optional): Class for Ray worker groups. Defaults to RayWorkerGroup.
            processor: Optional data processor, used for multimodal data
            train_dataset (Optional[Dataset], optional): Training dataset. Defaults to None.
            val_dataset (Optional[Dataset], optional): Validation dataset. Defaults to None.
            collate_fn: Function to collate data samples into batches.
            train_sampler (Optional[Sampler], optional): Sampler for the training dataset. Defaults to None.
            device_name (str, optional): Device name for training (e.g., "cuda", "cpu"). Defaults to None.
        """

        # Store the tokenizer for text processing
        self.tokenizer = tokenizer
        self.processor = processor
        self.config = config

        self.hybrid_engine = config.actor_rollout_ref.hybrid_engine
        assert self.hybrid_engine, "Currently, only support hybrid engine"

        if self.hybrid_engine:
            assert Role.ActorRollout in role_worker_mapping or Role.ActorRolloutRef in role_worker_mapping, (
                f"{role_worker_mapping.keys()=}"
            )

        self.role_worker_mapping = role_worker_mapping
        self.resource_pool_manager = resource_pool_manager
        self.use_reference_policy = need_reference_policy(self.config)

        self.use_rm = need_reward_model(self.config)

        self.use_critic = need_critic(self.config)
        self.ray_worker_group_cls = ray_worker_group_cls
        self.device_name = device_name if device_name else self.config.trainer.device
        self.validation_generations_logger = ValidationGenerationsLogger(
            project_name=self.config.trainer.project_name,
            experiment_name=self.config.trainer.experiment_name,
        )

        # if ref_in_actor is True, the reference policy will be actor without lora applied
        lora_rank = config.actor_rollout_ref.model.get("lora", {}).get("rank", 0)
        if lora_rank <= 0:
            lora_rank = config.actor_rollout_ref.model.get("lora_rank", 0)
        self.ref_in_actor = lora_rank > 0 or config.actor_rollout_ref.model.get("lora_adapter_path") is not None

        # define in-reward KL control
        # kl loss control currently not suppoorted
        if self.config.algorithm.use_kl_in_reward:
            self.kl_ctrl_in_reward = core_algos.get_kl_controller(self.config.algorithm.kl_ctrl)

        self.use_prefix_grouper = self.config.actor_rollout_ref.actor.get("use_prefix_grouper", False)
        self.use_legacy_worker_impl = config.trainer.get("use_legacy_worker_impl", "auto")

        self._create_dataloader(train_dataset, val_dataset, collate_fn, train_sampler)

        self.checkpoint_manager = None

    def _create_dataloader(self, train_dataset, val_dataset, collate_fn, train_sampler: Optional[Sampler]):
        """
        Creates the train and validation dataloaders.
        """
        # TODO: we have to make sure the batch size is divisible by the dp size
        from verl.trainer.main_ppo import create_rl_dataset, create_rl_sampler

        if train_dataset is None:
            train_dataset = create_rl_dataset(
                self.config.data.train_files,
                self.config.data,
                self.tokenizer,
                self.processor,
                max_samples=self.config.data.get("train_max_samples", -1),
            )
        if val_dataset is None:
            val_dataset = create_rl_dataset(
                self.config.data.val_files,
                self.config.data,
                self.tokenizer,
                self.processor,
                max_samples=self.config.data.get("val_max_samples", -1),
            )
        self.train_dataset, self.val_dataset = train_dataset, val_dataset

        if train_sampler is None:
            train_sampler = create_rl_sampler(self.config.data, self.train_dataset)
        if collate_fn is None:
            from verl.utils.dataset.rl_dataset import collate_fn as default_collate_fn

            collate_fn = default_collate_fn

        num_workers = self.config.data["dataloader_num_workers"]

        self.train_dataloader = StatefulDataLoader(
            dataset=self.train_dataset,
            batch_size=self.config.data.get("gen_batch_size", self.config.data.train_batch_size),
            num_workers=num_workers,
            drop_last=True,
            collate_fn=collate_fn,
            sampler=train_sampler,
        )

        val_batch_size = self.config.data.val_batch_size  # Prefer config value if set
        if val_batch_size is None:
            val_batch_size = len(self.val_dataset)

        self.val_dataloader = StatefulDataLoader(
            dataset=self.val_dataset,
            batch_size=val_batch_size,
            num_workers=num_workers,
            shuffle=self.config.data.get("validation_shuffle", True),
            drop_last=False,
            collate_fn=collate_fn,
        )

        assert len(self.train_dataloader) >= 1, "Train dataloader is empty!"
        assert len(self.val_dataloader) >= 1, "Validation dataloader is empty!"

        print(
            f"Size of train dataloader: {len(self.train_dataloader)}, Size of val dataloader: "
            f"{len(self.val_dataloader)}"
        )

        from omegaconf import OmegaConf
        # Build per-dataset val dataloaders when multi_val_files is configured.
        # Expected format: {name: file_path} or {name: [file1, file2, ...]}
        self.multi_val_dataloaders: dict[str, StatefulDataLoader] = {}
        multi_val_files = self.config.data.get("multi_val_files", None)
        if multi_val_files is not None:
            if hasattr(multi_val_files, "_metadata"):  # OmegaConf DictConfig
                multi_val_files = OmegaConf.to_container(multi_val_files, resolve=True)
            for ds_name, ds_files in multi_val_files.items():
                ds = create_rl_dataset(
                    ds_files,
                    self.config.data,
                    self.tokenizer,
                    self.processor,
                    max_samples=self.config.data.get("val_max_samples", -1),
                )
                bs = val_batch_size if val_batch_size <= len(ds) else len(ds)
                self.multi_val_dataloaders[ds_name] = StatefulDataLoader(
                    dataset=ds,
                    batch_size=bs,
                    num_workers=num_workers,
                    shuffle=self.config.data.get("validation_shuffle", True),
                    drop_last=False,
                    collate_fn=collate_fn,
                )
                print(f"  multi_val '{ds_name}': {len(self.multi_val_dataloaders[ds_name])} batches")

        total_training_steps = len(self.train_dataloader) * self.config.trainer.total_epochs

        if self.config.trainer.total_training_steps is not None:
            total_training_steps = self.config.trainer.total_training_steps

        self.total_training_steps = total_training_steps
        print(f"Total training steps: {self.total_training_steps}")

        try:
            OmegaConf.set_struct(self.config, True)
            with open_dict(self.config):
                if OmegaConf.select(self.config, "actor_rollout_ref.actor.optim"):
                    self.config.actor_rollout_ref.actor.optim.total_training_steps = total_training_steps
                if OmegaConf.select(self.config, "critic.optim"):
                    self.config.critic.optim.total_training_steps = total_training_steps
        except Exception as e:
            print(f"Warning: Could not set total_training_steps in config. Structure missing? Error: {e}")

    def _dump_generations(self, inputs, outputs, gts, scores, reward_extra_infos_dict, dump_path):
        """Dump rollout/validation samples as JSONL."""
        os.makedirs(dump_path, exist_ok=True)
        filename = os.path.join(dump_path, f"{self.global_steps}.jsonl")

        n = len(inputs)
        base_data = {
            "input": inputs,
            "output": outputs,
            "gts": gts,
            "score": scores,
            "step": [self.global_steps] * n,
        }

        for k, v in reward_extra_infos_dict.items():
            if len(v) == n:
                base_data[k] = v

        lines = []
        for i in range(n):
            entry = {k: v[i] for k, v in base_data.items()}
            lines.append(json.dumps(entry, ensure_ascii=False))

        with open(filename, "w") as f:
            f.write("\n".join(lines) + "\n")

        print(f"Dumped generations to {filename}")

    def _log_rollout_data(
        self, batch: DataProto, reward_extra_infos_dict: dict, timing_raw: dict, rollout_data_dir: str
    ):
        """Log rollout data to disk.
        Args:
            batch (DataProto): The batch containing rollout data
            reward_extra_infos_dict (dict): Additional reward information to log
            timing_raw (dict): Timing information for profiling
            rollout_data_dir (str): Directory path to save the rollout data
        """
        with marked_timer("dump_rollout_generations", timing_raw, color="green"):
            inputs = self.tokenizer.batch_decode(batch.batch["prompts"], skip_special_tokens=True)
            outputs = self.tokenizer.batch_decode(batch.batch["responses"], skip_special_tokens=True)
            scores = batch.batch["token_level_scores"].sum(-1).cpu().tolist()
            sample_gts = [item.non_tensor_batch.get("reward_model", {}).get("ground_truth", None) for item in batch]

            reward_extra_infos_to_dump = reward_extra_infos_dict.copy()
            if "request_id" in batch.non_tensor_batch:
                reward_extra_infos_dict.setdefault(
                    "request_id",
                    batch.non_tensor_batch["request_id"].tolist(),
                )

            self._dump_generations(
                inputs=inputs,
                outputs=outputs,
                gts=sample_gts,
                scores=scores,
                reward_extra_infos_dict=reward_extra_infos_to_dump,
                dump_path=rollout_data_dir,
            )
            self._dump_training_turn_csv(batch, rollout_data_dir)

    def _maybe_log_val_generations(self, inputs, outputs, scores):
        """Log a table of validation samples to the configured logger (wandb or swanlab)"""

        generations_to_log = self.config.trainer.log_val_generations

        if generations_to_log == 0:
            return

        import numpy as np

        # Create tuples of (input, output, score) and sort by input text
        samples = list(zip(inputs, outputs, scores, strict=True))
        samples.sort(key=lambda x: x[0])  # Sort by input text

        # Use fixed random seed for deterministic shuffling
        rng = np.random.RandomState(42)
        rng.shuffle(samples)

        # Take first N samples after shuffling
        samples = samples[:generations_to_log]

        # Log to each configured logger
        self.validation_generations_logger.log(self.config.trainer.logger, samples, self.global_steps)

    def _get_gen_batch(self, batch: DataProto) -> DataProto:
        reward_keys = (
            set(
                {
                    "data_source",
                    "metric_data_source",
                    "reward_model",
                    "extra_info",
                    "uid",
                    "domain",
                    "ability",
                    "agent_name",
                }
            )
            & batch.non_tensor_batch.keys()
        )

        # pop those keys for generation
        batch_keys_to_pop = []
        non_tensor_batch_keys_to_pop = set(batch.non_tensor_batch.keys()) - reward_keys
        gen_batch = batch.pop(
            batch_keys=batch_keys_to_pop,
            non_tensor_batch_keys=list(non_tensor_batch_keys_to_pop),
        )

        # For agent loop, we need reward model keys to compute score.
        gen_batch.non_tensor_batch.update(batch.non_tensor_batch)

        return gen_batch

    def _compute_reward_colocate(self, batch: DataProto) -> tuple[torch.Tensor, dict[str, Any]] | torch.Tensor:
        """
        compute reward use colocate reward model
        """
        assert self.reward_loop_manager is not None, "RewardLoopManager is None"
        batch_reward = self.reward_loop_manager.compute_rm_score(batch)
        return batch_reward
    
    def _dump_response_reward_csv(
        self,
        uids: list,
        inputs: list,
        outputs: list,
        gts: list,
        scores: list,
        reward_extra_infos_dict: dict,
        dataset_name: str | None = None,
    ):

        # ── resolve uid list ────────────────────────────────────────────────────
        if "uid" in reward_extra_infos_dict:
            uid_list = reward_extra_infos_dict["uid"]
        else:
            # fall back to the positional `uids` arg (which may itself be empty)
            uid_list = uids if uids else []
        """Write one row per sample: uid | input | output | ground_truth | reward | <extra reward keys...>"""
        output_dir = self.config.trainer.get("val_data_dir", None) or "."
        os.makedirs(output_dir, exist_ok=True)
        dataset_suffix = ""
        if dataset_name:
            safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(dataset_name))
            dataset_suffix = f"_{safe_name}"
        csv_path = os.path.join(output_dir, f"val_responses{dataset_suffix}_step{self.global_steps}.csv")
    
        # Collect extra reward column names (skip 'reward' – already in scores)
        extra_keys = [k for k in reward_extra_infos_dict if k != "reward"]
    
        fieldnames = ["uid", "input", "output", "ground_truth", "reward"] + extra_keys
        import csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
    
            for i, (uid, inp, out, gt, score) in enumerate(zip(uids, inputs, outputs, gts, scores)):
                row = {
                    "uid": uid_list[i] if i < len(uid_list) else "",
                    "input": inp,
                    "output": out,
                    "ground_truth": gt,
                    "reward": score,
                }
                for k in extra_keys:
                    lst = reward_extra_infos_dict[k]
                    row[k] = lst[i] if i < len(lst) else ""
                writer.writerow(row)
    
        print(f"[validate] Saved response+reward CSV → {csv_path}")
 
    def _dump_training_turn_csv(self, batch: DataProto, rollout_data_dir: str):
        """Dump training-only solver/user turn prompts and replies as CSV."""

        turn_logs = batch.non_tensor_batch.get("training_turn_log")
        if turn_logs is None:
            return

        os.makedirs(rollout_data_dir, exist_ok=True)
        csv_path = os.path.join(rollout_data_dir, f"training_turns_step{self.global_steps}.csv")

        uids = batch.non_tensor_batch.get("uid")
        request_ids = batch.non_tensor_batch.get("request_id")
        fieldnames = [
            "step",
            "sample_index",
            "uid",
            "request_id",
            "turn_index",
            "role",
            "current context (prompt)",
            "reply",
            "solver_attempts",
            "sim_user_turns",
        ]

        import csv

        rows_written = 0
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for sample_index, sample_turns in enumerate(turn_logs):
                if sample_turns is None:
                    continue
                if isinstance(sample_turns, np.ndarray):
                    sample_turns = sample_turns.tolist()
                if not isinstance(sample_turns, list):
                    continue

                uid = uids[sample_index] if uids is not None and sample_index < len(uids) else ""
                request_id = (
                    request_ids[sample_index]
                    if request_ids is not None and sample_index < len(request_ids)
                    else ""
                )
                for turn in sample_turns:
                    if not isinstance(turn, dict):
                        continue
                    writer.writerow(
                        {
                            "step": self.global_steps,
                            "sample_index": sample_index,
                            "uid": uid,
                            "request_id": request_id,
                            "turn_index": turn.get("turn_index", ""),
                            "role": turn.get("role", ""),
                            "current context (prompt)": turn.get("current_context", ""),
                            "reply": turn.get("reply", ""),
                            "solver_attempts": turn.get("solver_attempts", ""),
                            "sim_user_turns": turn.get("sim_user_turns", ""),
                        }
                    )
                    rows_written += 1

        print(f"[train] Saved solver/user turn CSV ({rows_written} rows) → {csv_path}")


    def _validate(self, merged: bool = False, val_dataloader=None, dataset_name: str | None = None):
        data_source_lst = []
        reward_extra_infos_dict: dict[str, list] = defaultdict(list)

        # Lists to collect samples for the table
        sample_inputs = []
        sample_outputs = []
        sample_gts = []
        sample_scores = []
        sample_turns = []
        sample_uids = []

        if val_dataloader is None:
            val_dataloader = self.val_dataloader

        for test_data in val_dataloader:
            test_batch = DataProto.from_single_dict(test_data)

            if "uid" not in test_batch.non_tensor_batch:
                test_batch.non_tensor_batch["uid"] = np.array(
                    [str(uuid.uuid4()) for _ in range(len(test_batch.batch))], dtype=object
                )

            # repeat test batch
            test_batch = test_batch.repeat(
                repeat_times=self.config.actor_rollout_ref.rollout.val_kwargs.n, interleave=True
            )

            ground_truths = [
                item.non_tensor_batch.get("reward_model", {}).get("ground_truth", None) for item in test_batch
            ]
            sample_gts.extend(ground_truths)

            test_gen_batch = self._get_gen_batch(test_batch)
            test_gen_batch.meta_info = {
                "eos_token_id": self.tokenizer.eos_token_id,
                "pad_token_id": self.tokenizer.pad_token_id,
                "recompute_log_prob": False,
                "do_sample": self.config.actor_rollout_ref.rollout.val_kwargs.do_sample,
                "validate": True,
                "global_steps": self.global_steps,
            }
            print(f"test_gen_batch meta info: {test_gen_batch.meta_info}")

            # pad to be divisible by dp_size
            size_divisor = self.config.actor_rollout_ref.rollout.agent.num_workers
            test_gen_batch_padded, pad_size = pad_dataproto_to_divisor(test_gen_batch, size_divisor)
            test_output_gen_batch_padded = self.async_rollout_manager.generate_sequences(test_gen_batch_padded)

            if self.use_rm and "rm_scores" not in test_output_gen_batch_padded.batch.keys():
                # for colocate reward models, we need to sleep rollout model
                # to spare GPU memory for reward model
                self.checkpoint_manager.sleep_replicas()
                batch_reward = self._compute_reward_colocate(test_output_gen_batch_padded)
                test_output_gen_batch_padded = test_output_gen_batch_padded.union(batch_reward)
                # wake up rollout model
                # replace with wake_up method once supported
                self.checkpoint_manager.update_weights(self.global_steps)

            # unpad
            test_output_gen_batch = unpad_dataproto(test_output_gen_batch_padded, pad_size=pad_size)

            print("validation generation end")

            # Store generated outputs
            output_ids = test_output_gen_batch.batch["responses"]
            output_texts = [self.tokenizer.decode(ids, skip_special_tokens=True) for ids in output_ids]
            sample_outputs.extend(output_texts)

            test_batch = test_batch.union(test_output_gen_batch)
            test_batch.meta_info["validate"] = True

            # Store original inputs
            input_ids = test_batch.batch["prompts"]
            # TODO: Can we keep special tokens except for padding tokens?
            input_texts = [self.tokenizer.decode(ids, skip_special_tokens=True) for ids in input_ids]
            sample_inputs.extend(input_texts)
            sample_uids.extend(test_batch.non_tensor_batch["uid"])

            # evaluate using reward_function
            reward_tensor, reward_extra_info = extract_reward(test_batch)

            scores = reward_tensor.sum(-1).cpu().tolist()
            sample_scores.extend(scores)

            reward_extra_infos_dict["reward"].extend(scores)
            for key, values in reward_extra_info.items():
                if key not in reward_extra_infos_dict:
                    reward_extra_infos_dict[key] = []
                if isinstance(values, np.ndarray):
                    reward_extra_infos_dict[key].extend(values.tolist())
                else:
                    reward_extra_infos_dict[key].extend(values if isinstance(values, list) else [values])

            # collect num_turns of each prompt
            if "__num_turns__" in test_batch.non_tensor_batch:
                sample_turns.append(test_batch.non_tensor_batch["__num_turns__"])

            metric_data_sources = test_batch.non_tensor_batch.get(
                "metric_data_source",
                test_batch.non_tensor_batch.get("data_source", ["unknown"] * reward_tensor.shape[0]),
            )
            data_source_lst.append(metric_data_sources)

        self._maybe_log_val_generations(inputs=sample_inputs, outputs=sample_outputs, scores=sample_scores)
        
         # ── NEW: dump response + reward CSV ──────────────────────────────────
        val_data_dir = self.config.trainer.get("val_data_dir", None)

        if val_data_dir:
            self._dump_response_reward_csv(
            uids=sample_uids,
            inputs=sample_inputs,
            outputs=sample_outputs,
            gts=sample_gts,
            scores=sample_scores,
            reward_extra_infos_dict=reward_extra_infos_dict,
            dataset_name=dataset_name,
        )
    # ─────────────────────────────────────────────────────────────────────

        # dump generations
        val_data_dir = self.config.trainer.get("validation_data_dir", None)
        if val_data_dir:
            self._dump_generations(
                inputs=sample_inputs,
                outputs=sample_outputs,
                gts=sample_gts,
                scores=sample_scores,
                reward_extra_infos_dict=reward_extra_infos_dict,
                dump_path=val_data_dir,
            )

        for key_info, lst in reward_extra_infos_dict.items():
            assert len(lst) == 0 or len(lst) == len(sample_scores), f"{key_info}: {len(lst)=}, {len(sample_scores)=}"

        if merged:
            print("_merge_validation_results validate result will be merged")
            return {
                "data_sources": data_source_lst,
                "sample_uids": sample_uids,
                "sample_turns": sample_turns,
                "reward_extra_infos_dict": reward_extra_infos_dict,
            }
        data_sources = np.concatenate(data_source_lst, axis=0)
        return self._val_metrics_update(data_sources, sample_uids, reward_extra_infos_dict, sample_turns)

    def _validate_multi(self) -> dict:
        """Run _validate for each dataset in multi_val_dataloaders and prefix metrics by name.

        Falls back to single _validate() when no multi_val_dataloaders are configured.
        """
        if not self.multi_val_dataloaders:
            return self._validate()

        combined: dict = {}
        for ds_name, dl in self.multi_val_dataloaders.items():
            print(f"[validate_multi] Running validation for dataset: {ds_name}")
            ds_metrics = self._validate(val_dataloader=dl, dataset_name=ds_name)
            for k, v in ds_metrics.items():
                # Insert ds_name after the first segment so wandb groups nicely.
                # e.g. "val-core/unknown/reward/mean@8" → "val-core/2wiki/unknown/reward/mean@8"
                parts = k.split("/", 1)
                if len(parts) == 2:
                    combined[f"{parts[0]}/{ds_name}/{parts[1]}"] = v
                else:
                    combined[f"{k}/{ds_name}"] = v
        return combined

    def _val_metrics_update(self, data_sources, sample_uids, reward_extra_infos_dict, sample_turns):
        data_src2var2metric2val = process_validation_metrics(data_sources, sample_uids, reward_extra_infos_dict)
        metric_dict = {}
        for data_source, var2metric2val in data_src2var2metric2val.items():
            core_var = "acc" if "acc" in var2metric2val else "reward"
            for var_name, metric2val in var2metric2val.items():
                n_max = max([int(name.split("@")[-1].split("/")[0]) for name in metric2val.keys()])
                for metric_name, metric_val in metric2val.items():
                    is_avg_pass_at_k = metric_name.startswith(("Avg@", "Pass@"))
                    if (
                        (var_name == core_var)
                        and (
                            is_avg_pass_at_k
                            or (
                                any(metric_name.startswith(pfx) for pfx in ["mean", "maj", "best"])
                                and (f"@{n_max}" in metric_name)
                            )
                        )
                    ):
                        metric_sec = "val-core"
                    else:
                        metric_sec = "val-aux"
                    pfx = f"{metric_sec}/{data_source}/{var_name}/{metric_name}"
                    metric_dict[pfx] = metric_val

        if len(sample_turns) > 0:
            sample_turns = np.concatenate(sample_turns)
            metric_dict["val-aux/num_turns/min"] = sample_turns.min()
            metric_dict["val-aux/num_turns/max"] = sample_turns.max()
            metric_dict["val-aux/num_turns/mean"] = sample_turns.mean()

        return metric_dict

    def _merge_validation_results(self, result_a, result_b):
        if result_a is None and result_b is None:
            return {}
        if result_a is None:
            result_a = {"data_sources": [], "sample_uids": [], "sample_turns": [], "reward_extra_infos_dict": {}}
        if result_b is None:
            result_b = {"data_sources": [], "sample_uids": [], "sample_turns": [], "reward_extra_infos_dict": {}}

        if not result_a.get("data_sources") and not result_b.get("data_sources"):
            return {}

        data_sources = np.concatenate(result_a["data_sources"] + result_b["data_sources"], axis=0)
        sample_uids = result_a["sample_uids"] + result_b["sample_uids"]
        sample_turns = result_a["sample_turns"] + result_b["sample_turns"]

        reward_extra_infos_dict = {}
        all_keys = set(result_a["reward_extra_infos_dict"].keys()) | set(result_b["reward_extra_infos_dict"].keys())
        for key in all_keys:
            list_a = result_a["reward_extra_infos_dict"].get(key, [])
            list_b = result_b["reward_extra_infos_dict"].get(key, [])
            reward_extra_infos_dict[key] = list_a + list_b

        return self._val_metrics_update(data_sources, sample_uids, reward_extra_infos_dict, sample_turns)

    def init_workers(self):
        """Initialize distributed training workers using Ray backend.

        Creates:
        1. Ray resource pools from configuration
        2. Worker groups for each role (actor, critic, etc.)
        """
        self.resource_pool_manager.create_resource_pool()

        self.resource_pool_to_cls = {pool: {} for pool in self.resource_pool_manager.resource_pool_dict.values()}

        # create actor and rollout
        actor_role = Role.ActorRolloutRef if Role.ActorRolloutRef in self.role_worker_mapping else Role.ActorRollout
        if self.hybrid_engine:
            actor_rollout_resource_pool = self.resource_pool_manager.get_resource_pool(actor_role)
            actor_rollout_cls = RayClassWithInitArgs(
                cls=self.role_worker_mapping[actor_role],
                config=self.config.actor_rollout_ref,
                role=str(actor_role),
            )
            self.resource_pool_to_cls[actor_rollout_resource_pool][str(actor_role)] = actor_rollout_cls
        else:
            raise NotImplementedError

        # create critic
        if self.use_critic:
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.Critic)

            from verl.workers.config import CriticConfig

            critic_cfg: CriticConfig = omega_conf_to_dataclass(self.config.critic)

            if self.use_legacy_worker_impl == "disable":
                # convert critic_cfg into TrainingWorkerConfig
                from verl.workers.engine_workers import TrainingWorkerConfig

                orig_critic_cfg = critic_cfg
                if orig_critic_cfg.strategy == "fsdp":
                    engine_config: FSDPEngineConfig = orig_critic_cfg.model.fsdp_config
                    engine_config.infer_max_token_len_per_gpu = critic_cfg.ppo_infer_max_token_len_per_gpu
                    engine_config.max_token_len_per_gpu = critic_cfg.ppo_max_token_len_per_gpu
                else:
                    raise NotImplementedError(f"Unknown strategy {orig_critic_cfg.strategy=}")

                critic_cfg = TrainingWorkerConfig(
                    model_type="value_model",
                    model_config=orig_critic_cfg.model_config,
                    engine_config=engine_config,
                    optimizer_config=orig_critic_cfg.optim,
                    checkpoint_config=orig_critic_cfg.checkpoint,
                )

            critic_cls = RayClassWithInitArgs(cls=self.role_worker_mapping[Role.Critic], config=critic_cfg)
            self.resource_pool_to_cls[resource_pool][str(Role.Critic)] = critic_cls

        # create reference policy if needed
        if self.use_reference_policy and Role.RefPolicy in self.role_worker_mapping:
            resource_pool = self.resource_pool_manager.get_resource_pool(Role.RefPolicy)
            ref_policy_cls = RayClassWithInitArgs(
                self.role_worker_mapping[Role.RefPolicy],
                config=self.config.actor_rollout_ref,
                role=str(Role.RefPolicy),
            )
            self.resource_pool_to_cls[resource_pool][str(Role.RefPolicy)] = ref_policy_cls

        # initialize WorkerGroup
        # NOTE: if you want to use a different resource pool for each role, which can support different parallel size,
        # you should not use `create_colocated_worker_cls`.
        # Instead, directly pass different resource pool to different worker groups.
        # See https://github.com/volcengine/verl/blob/master/examples/ray/tutorial.ipynb for more information.
        all_wg = {}
        wg_kwargs = {}  # Setting up kwargs for RayWorkerGroup
        if OmegaConf.select(self.config.trainer, "ray_wait_register_center_timeout") is not None:
            wg_kwargs["ray_wait_register_center_timeout"] = self.config.trainer.ray_wait_register_center_timeout
        if OmegaConf.select(self.config.global_profiler, "steps") is not None:
            wg_kwargs["profile_steps"] = OmegaConf.select(self.config.global_profiler, "steps")
            # Only require nsight worker options when tool is nsys
            if OmegaConf.select(self.config.global_profiler, "tool") == "nsys":
                assert (
                    OmegaConf.select(self.config.global_profiler.global_tool_config.nsys, "worker_nsight_options")
                    is not None
                ), "worker_nsight_options must be set when using nsys with profile_steps"
                wg_kwargs["worker_nsight_options"] = OmegaConf.to_container(
                    OmegaConf.select(self.config.global_profiler.global_tool_config.nsys, "worker_nsight_options")
                )
        wg_kwargs["device_name"] = self.device_name

        for resource_pool, class_dict in self.resource_pool_to_cls.items():
            if not class_dict:
                continue
            worker_dict_cls = create_colocated_worker_cls(class_dict=class_dict)
            wg_dict = self.ray_worker_group_cls(
                resource_pool=resource_pool,
                ray_cls_with_init=worker_dict_cls,
                **wg_kwargs,
            )
            spawn_wg = wg_dict.spawn(prefix_set=class_dict.keys())
            all_wg.update(spawn_wg)

        if self.use_critic:
            self.critic_wg = all_wg[str(Role.Critic)]
            if self.use_legacy_worker_impl == "disable":
                self.critic_wg.reset()
                # assign critic loss
                from functools import partial

                from verl.workers.utils.losses import value_loss

                value_loss_ = partial(value_loss, config=orig_critic_cfg)
                self.critic_wg.set_loss_fn(value_loss_)
            else:
                self.critic_wg.init_model()

        if self.use_reference_policy and not self.ref_in_actor:
            if str(Role.RefPolicy) in all_wg:
                self.ref_policy_wg = all_wg[str(Role.RefPolicy)]
                self.ref_policy_wg.init_model()
            else:
                # Model engine: ActorRolloutRefWorker
                assert str(Role.ActorRolloutRef) in all_wg, f"{all_wg.keys()=}"
                self.ref_policy_wg = all_wg[str(Role.ActorRolloutRef)]

        # we should create rollout at the end so that vllm can have a better estimation of kv cache memory
        self.actor_rollout_wg = all_wg[str(actor_role)]
        self.actor_rollout_wg.init_model()

        if self.ref_in_actor:
            self.ref_policy_wg = self.actor_rollout_wg

        # create reward loop manager
        from verl.experimental.reward_loop import RewardLoopManager

        # initalize reward loop manager
        # reward model (colocate or standalone): get resource_pool
        # no reward model: resource_pool = None
        resource_pool = self.resource_pool_manager.get_resource_pool(Role.RewardModel) if self.use_rm else None
        self.reward_loop_manager = RewardLoopManager(
            config=self.config,
            rm_resource_pool=resource_pool,
        )

        # create async rollout manager and request scheduler
        # Note: mode is always "async" since sync mode is deprecated
        self.async_rollout_mode = True

        # Support custom AgentLoopManager via config
        manager_class_fqn = self.config.actor_rollout_ref.rollout.get("agent", {}).get("agent_loop_manager_class")
        if manager_class_fqn:
            AgentLoopManager = load_class_from_fqn(manager_class_fqn, "AgentLoopManager")
        else:
            from verl.experimental.agent_loop import AgentLoopManager

        # infrastructure overview: https://verl.readthedocs.io/en/latest/advance/reward_loop.html#architecture-design
        # agent_reward_loop: streaming reward computation with actor rollout
        # two conditions satisfied: (1) no reward model, or (2) reward model with extra resource pool
        enable_agent_reward_loop = not self.use_rm or self.config.reward.reward_model.enable_resource_pool

        # if enable_agent_reward_loop, we directly pass reward_loop_workers to agent loop manager
        # to stream reward computation with actor rollout
        reward_loop_worker_handles = self.reward_loop_manager.reward_loop_workers if enable_agent_reward_loop else None
        self.async_rollout_manager = AgentLoopManager.create(
            config=self.config,
            worker_group=self.actor_rollout_wg,
            rollout_resource_pool=actor_rollout_resource_pool,
            reward_loop_worker_handles=reward_loop_worker_handles,
        )
        checkpoint_engine_config = omega_conf_to_dataclass(self.config.actor_rollout_ref.rollout.checkpoint_engine)
        self.checkpoint_manager = CheckpointEngineManager(
            config=checkpoint_engine_config,
            trainer=self.actor_rollout_wg,
            replicas=self.async_rollout_manager.rollout_replicas,
        )

        # sleep all replicas to load checkpoint
        self.checkpoint_manager.sleep_replicas()

    def _save_checkpoint(self):
        from verl.utils.fs import local_mkdir_safe

        # path: given_path + `/global_step_{global_steps}` + `/actor`
        local_global_step_folder = os.path.join(
            self.config.trainer.default_local_dir, f"global_step_{self.global_steps}"
        )

        print(f"local_global_step_folder: {local_global_step_folder}")
        actor_local_path = os.path.join(local_global_step_folder, "actor")

        actor_remote_path = (
            None
            if self.config.trainer.default_hdfs_dir is None
            else os.path.join(self.config.trainer.default_hdfs_dir, f"global_step_{self.global_steps}", "actor")
        )

        remove_previous_ckpt_in_save = self.config.trainer.get("remove_previous_ckpt_in_save", False)
        if remove_previous_ckpt_in_save:
            print(
                "Warning: remove_previous_ckpt_in_save is deprecated,"
                + " set max_actor_ckpt_to_keep=1 and max_critic_ckpt_to_keep=1 instead"
            )
        max_actor_ckpt_to_keep = (
            self.config.trainer.get("max_actor_ckpt_to_keep", None) if not remove_previous_ckpt_in_save else 1
        )
        max_critic_ckpt_to_keep = (
            self.config.trainer.get("max_critic_ckpt_to_keep", None) if not remove_previous_ckpt_in_save else 1
        )

        self.actor_rollout_wg.save_checkpoint(
            actor_local_path, actor_remote_path, self.global_steps, max_ckpt_to_keep=max_actor_ckpt_to_keep
        )

        if self.use_critic:
            critic_local_path = os.path.join(local_global_step_folder, str(Role.Critic))
            critic_remote_path = (
                None
                if self.config.trainer.default_hdfs_dir is None
                else os.path.join(
                    self.config.trainer.default_hdfs_dir, f"global_step_{self.global_steps}", str(Role.Critic)
                )
            )
            self.critic_wg.save_checkpoint(
                critic_local_path, critic_remote_path, self.global_steps, max_ckpt_to_keep=max_critic_ckpt_to_keep
            )

        # save dataloader
        local_mkdir_safe(local_global_step_folder)
        dataloader_local_path = os.path.join(local_global_step_folder, "data.pt")
        dataloader_state_dict = self.train_dataloader.state_dict()
        torch.save(dataloader_state_dict, dataloader_local_path)

        # latest checkpointed iteration tracker (for atomic usage)
        if (
            hasattr(self.config.actor_rollout_ref.actor.checkpoint, "async_save")
            and self.config.actor_rollout_ref.actor.checkpoint.async_save
        ) or (
            "async_save" in self.config.actor_rollout_ref.actor.checkpoint
            and self.config.actor_rollout_ref.actor.checkpoint["async_save"]
        ):
            print("skip write latest_checkpointed_iteration.txt when async_save is True")
            return
        local_latest_checkpointed_iteration = os.path.join(
            self.config.trainer.default_local_dir, "latest_checkpointed_iteration.txt"
        )
        with open(local_latest_checkpointed_iteration, "w") as f:
            f.write(str(self.global_steps))

    def _load_checkpoint(self):
        if self.config.trainer.resume_mode == "disable":
            return 0

        # load from hdfs
        if self.config.trainer.default_hdfs_dir is not None:
            raise NotImplementedError("load from hdfs is not implemented yet")
        else:
            checkpoint_folder = self.config.trainer.default_local_dir  # TODO: check path
            if not os.path.isabs(checkpoint_folder):
                working_dir = os.getcwd()
                checkpoint_folder = os.path.join(working_dir, checkpoint_folder)
            global_step_folder = find_latest_ckpt_path(checkpoint_folder)  # None if no latest

        # find global_step_folder
        if self.config.trainer.resume_mode == "auto":
            if global_step_folder is None:
                print("Training from scratch")
                return 0
        else:
            if self.config.trainer.resume_mode == "resume_path":
                assert isinstance(self.config.trainer.resume_from_path, str), "resume ckpt must be str type"
                assert "global_step_" in self.config.trainer.resume_from_path, (
                    "resume ckpt must specify the global_steps"
                )
                global_step_folder = self.config.trainer.resume_from_path
                if not os.path.isabs(global_step_folder):
                    working_dir = os.getcwd()
                    global_step_folder = os.path.join(working_dir, global_step_folder)
        print(f"Load from checkpoint folder: {global_step_folder}")
        # set global step
        self.global_steps = int(global_step_folder.split("global_step_")[-1])

        print(f"Setting global step to {self.global_steps}")
        print(f"Resuming from {global_step_folder}")

        actor_path = os.path.join(global_step_folder, "actor")
        critic_path = os.path.join(global_step_folder, str(Role.Critic))
        # load actor
        self.actor_rollout_wg.load_checkpoint(
            actor_path, del_local_after_load=self.config.trainer.del_local_ckpt_after_load
        )
        # load critic
        if self.use_critic:
            self.critic_wg.load_checkpoint(
                critic_path, del_local_after_load=self.config.trainer.del_local_ckpt_after_load
            )

        # load dataloader,
        # TODO: from remote not implemented yet
        dataloader_local_path = os.path.join(global_step_folder, "data.pt")
        if os.path.exists(dataloader_local_path):
            dataloader_state_dict = torch.load(dataloader_local_path, weights_only=False)
            self.train_dataloader.load_state_dict(dataloader_state_dict)
        else:
            print(f"Warning: No dataloader state found at {dataloader_local_path}, will start from scratch")

    def _start_profiling(self, do_profile: bool) -> None:
        """Start profiling for all worker groups if profiling is enabled."""
        if do_profile:
            self.actor_rollout_wg.start_profile(role="e2e", profile_step=self.global_steps)
            if self.use_reference_policy:
                self.ref_policy_wg.start_profile(profile_step=self.global_steps)
            if self.use_critic:
                self.critic_wg.start_profile(profile_step=self.global_steps)

    def _stop_profiling(self, do_profile: bool) -> None:
        """Stop profiling for all worker groups if profiling is enabled."""
        if do_profile:
            self.actor_rollout_wg.stop_profile()
            if self.use_reference_policy:
                self.ref_policy_wg.stop_profile()
            if self.use_critic:
                self.critic_wg.stop_profile()

    def _get_dp_size(self, worker_group, role: str) -> int:
        """Get data parallel size from worker group dispatch info.

        This method retrieves the data parallel size by querying the dispatch info
        for the specified role. The dispatch info is cached for subsequent calls.

        Args:
            worker_group: The worker group to query dispatch info from.
            role: The role name (e.g., "actor", "critic") to get DP size for.

        Returns:
            The data parallel size (number of DP ranks).
        """
        if role not in worker_group._dispatch_info:
            dp_rank_mapping = worker_group._query_dispatch_info(role)
            worker_group._dispatch_info[role] = dp_rank_mapping
        else:
            dp_rank_mapping = worker_group._dispatch_info[role]
        return max(dp_rank_mapping) + 1

    def _balance_batch(self, batch: DataProto, metrics, logging_prefix="global_seqlen", keep_minibatch=False):
        """Reorder the data on single controller such that each dp rank gets similar total tokens.

        When use_prefix_grouper is enabled, uses group-level balancing to keep samples with
        the same uid together on the same rank for prefix sharing optimization.
        """
        attention_mask = batch.batch["attention_mask"]
        batch_size = attention_mask.shape[0]
        global_seqlen_lst = batch.batch["attention_mask"].view(batch_size, -1).sum(-1)  # (train_batch_size,)
        workload_lst = calculate_workload(global_seqlen_lst)
        # Get dp_size from dispatch info to correctly balance across data parallel ranks
        # Note: world_size may include tensor/pipeline parallel dimensions, but we only want DP
        dp_size = self._get_dp_size(self.actor_rollout_wg, "actor")

        # Use group-level balancing for PrefixGrouper to keep same-uid samples together
        if getattr(self, "use_prefix_grouper", False) and "uid" in batch.non_tensor_batch:
            from verl.utils.seqlen_balancing import get_group_balanced_partitions

            uid_list = list(batch.non_tensor_batch["uid"])
            seqlen_list = global_seqlen_lst.tolist()

            # Count number of uid groups
            num_groups = len(set(uid_list))

            if num_groups % dp_size != 0:
                raise ValueError(
                    f"PrefixGrouper with balance_batch requires num_uid_groups ({num_groups}) "
                    f"% dp_size ({dp_size}) == 0. "
                    f"This ensures each rank gets equal number of groups. "
                    f"Current batch_size={batch_size}, adjust batch_size to be a multiple of "
                    f"dp_size * rollout.n."
                )

            global_partition_lst = get_group_balanced_partitions(
                seqlen_list=seqlen_list,
                uid_list=uid_list,
                k_partitions=dp_size,
            )

        elif keep_minibatch:
            # Decouple the DP balancing and mini-batching.
            minibatch_size = self.config.actor_rollout_ref.actor.get("ppo_mini_batch_size")
            minibatch_num = len(workload_lst) // minibatch_size
            global_partition_lst = [[] for _ in range(dp_size)]
            for i in range(minibatch_num):
                rearrange_minibatch_lst = get_seqlen_balanced_partitions(
                    workload_lst[i * minibatch_size : (i + 1) * minibatch_size],
                    k_partitions=dp_size,
                    equal_size=True,
                )
                for j, part in enumerate(rearrange_minibatch_lst):
                    global_partition_lst[j].extend([x + minibatch_size * i for x in part])
        else:
            global_partition_lst = get_seqlen_balanced_partitions(workload_lst, k_partitions=dp_size, equal_size=True)
        # Place smaller micro-batches at both ends to reduce the bubbles in pipeline parallel.
        # Skip reordering within partitions for PrefixGrouper to maintain uid grouping
        if not getattr(self, "use_prefix_grouper", False):
            for idx, partition in enumerate(global_partition_lst):
                partition.sort(key=lambda x: (workload_lst[x], x))
                ordered_partition = partition[::2] + partition[1::2][::-1]
                global_partition_lst[idx] = ordered_partition

        # reorder based on index. The data will be automatically equally partitioned by dispatch function
        global_idx = torch.tensor([j for partition in global_partition_lst for j in partition])
        batch.reorder(global_idx)
        global_balance_stats = log_seqlen_unbalance(
            seqlen_list=global_seqlen_lst.tolist(), partitions=global_partition_lst, prefix=logging_prefix
        )
        metrics.update(global_balance_stats)

    def _compute_values(self, batch: DataProto) -> DataProto:
        if self.use_legacy_worker_impl == "disable":
            batch_td = batch.to_tensordict()
            # step 2: convert from padding to nopadding
            batch_td = left_right_2_no_padding(batch_td)
            # step 3: add meta info
            tu.assign_non_tensor(batch_td, compute_loss=False)
            output = self.critic_wg.infer_batch(batch_td)
            output = output.get()
            values = tu.get(output, "values")
            values = no_padding_2_padding(values, batch_td)
            values = tu.get_tensordict({"values": values.float()})
            values = DataProto.from_tensordict(values)
        else:
            values = self.critic_wg.compute_values(batch)
        return values

    def _compute_ref_log_prob(self, batch: DataProto) -> DataProto:
        if self.use_legacy_worker_impl == "disable":
            # step 1: convert dataproto to tensordict.
            batch_td = batch.to_tensordict()
            # step 2: convert from padding to nopadding
            batch_td = left_right_2_no_padding(batch_td)
            # step 3: add meta info
            metadata = {"calculate_entropy": False, "compute_loss": False}
            if self.ref_in_actor:
                metadata["no_lora_adapter"] = True
            tu.assign_non_tensor(batch_td, **metadata)
            if self.ref_in_actor:
                output = self.actor_rollout_wg.compute_log_prob(batch_td)
            else:
                output = self.ref_policy_wg.compute_ref_log_prob(batch_td)
            # gather output
            log_probs = tu.get(output, "log_probs")
            # step 4. No padding to padding
            log_probs = no_padding_2_padding(log_probs, batch_td)
            # step 5: rebuild a tensordict and convert to dataproto
            ref_log_prob = tu.get_tensordict({"ref_log_prob": log_probs.float()})
            ref_log_prob = DataProto.from_tensordict(ref_log_prob)
        else:
            ref_log_prob = self.ref_policy_wg.compute_ref_log_prob(batch)

        return ref_log_prob

    def _compute_old_log_prob(self, batch: DataProto):
        if self.use_legacy_worker_impl == "disable":
            # TODO: remove step 1, 2, 4 after we make the whole training tensordict and padding free
            # step 1: convert dataproto to tensordict.
            batch_td = batch.to_tensordict()
            # step 2: convert from padding to nopadding
            batch_td = left_right_2_no_padding(batch_td)
            # step 3: add meta info
            tu.assign_non_tensor(batch_td, calculate_entropy=True, compute_loss=False)
            output = self.actor_rollout_wg.compute_log_prob(batch_td)
            # gather output
            entropy = tu.get(output, "entropy")
            log_probs = tu.get(output, "log_probs")
            sum_pi_squared = tu.get(output, "sum_pi_squared")
            update_sketch = tu.get(output, "update_sketch")
            routed_experts = tu.get(output, "routed_experts")
            old_log_prob_mfu = tu.get(output, "metrics")["mfu"]
            # step 4. No padding to padding
            entropy = no_padding_2_padding(entropy, batch_td)
            log_probs = no_padding_2_padding(log_probs, batch_td)
            if sum_pi_squared is not None:
                sum_pi_squared = no_padding_2_padding(sum_pi_squared, batch_td)
            if update_sketch is not None:
                update_sketch = no_padding_2_padding(update_sketch, batch_td)

            if getattr(self.config.actor_rollout_ref.actor, "use_igpo", False):
                ig_output = self.actor_rollout_wg.compute_igpo_log_probs(batch_td)

                extra_log_probs = tu.get(ig_output, "extra_log_probs")   # (E, response_length)
                meta_json       = tu.get_non_tensor_data(ig_output, "igpo_meta", default=True)

                import json
                meta = json.loads(meta_json)
                ig_token_rewards = _compute_ig_rewards_cpu(
                    log_probs        = log_probs,          # (B, response_length) already on            CPU
                    extra_log_probs  = extra_log_probs,    # (E, response_length)
                    meta             = meta,
                    turn_segs_ll     = batch_td["turn_segs"],
                    reward_extra_ll  = batch_td["reward_extra_info"],
                    response_length  = self.config.actor_rollout_ref.rollout.response_length
                )
            else:
                ig_token_rewards = None

           # step 5: assemble DataProto
            if routed_experts is not None:
                proto_dict = {
                    "old_log_probs": log_probs.float(),
                    "entropys": entropy.float(),
                    "routed_experts": routed_experts,
                }
            else:
                proto_dict = {
                    "old_log_probs": log_probs.float(),
                    "entropys": entropy.float(),
                }

            if ig_token_rewards is not None:
                proto_dict["ig_token_rewards"] = ig_token_rewards.float()
            if sum_pi_squared is not None:
                proto_dict["sum_pi_squared"] = sum_pi_squared.float()
            if update_sketch is not None:
                proto_dict["update_sketch"] = update_sketch.float()

            old_log_prob = tu.get_tensordict(proto_dict)
            old_log_prob = DataProto.from_tensordict(old_log_prob)
        else:
            old_log_prob = self.actor_rollout_wg.compute_log_prob(batch)
            old_log_prob_mfu = 0
        return old_log_prob, old_log_prob_mfu

    def _compute_mi_proxy_reward(self, batch: DataProto) -> tuple[DataProto, dict[str, Any]] | None:
        """
        Compute a cheap in-batch contrastive proxy for I(X; Z) and inject it as an
        auxiliary terminal reward.

        We keep the vanilla GRPO/PPO loss unchanged; the MI proxy is turned into an
        extra reward so it flows through the existing advantage estimator.
        """
        # breakpoint()
        mi_reward_coef = float(self.config.algorithm.get("mi_reward_coef", 0.0))
        if mi_reward_coef <= 0.0:
            return None

        if "prompts" not in batch.batch or "responses" not in batch.batch:
            return batch, {"actor/mi_proxy/skipped": 1.0}

        prompts = batch.batch["prompts"]
        responses = batch.batch["responses"]
        input_ids = batch.batch["input_ids"]
        attention_mask = batch.batch["attention_mask"]
        response_mask = batch.batch["response_mask"]
        position_ids = batch.batch["position_ids"]

        if prompts.is_nested or responses.is_nested or input_ids.is_nested or attention_mask.is_nested:
            return batch, {"actor/mi_proxy/skipped": 1.0}

        if position_ids.dim() not in (2, 3):
            return batch, {"actor/mi_proxy/skipped": 1.0}

        batch_size = prompts.shape[0]
        if batch_size < 2:
            return batch, {"actor/mi_proxy/skipped": 1.0}

        prompt_len = prompts.shape[1]
        response_len = responses.shape[1]
        uid_list = None
        if "uid" in batch.non_tensor_batch:
            uid_list = list(batch.non_tensor_batch["uid"])

        max_negatives = int(self.config.algorithm.get("mi_reward_num_negatives", 3))

        pair_prompt_indices: list[int] = []
        pair_response_indices: list[int] = []
        row_slices: list[tuple[int, int]] = []

        for i in range(batch_size):
            if uid_list is not None:
                eligible = [j for j in range(batch_size) if j != i and uid_list[j] != uid_list[i]]
            else:
                eligible = [j for j in range(batch_size) if j != i]

            if not eligible:
                eligible = [j for j in range(batch_size) if j != i]

            if max_negatives > 0 and len(eligible) > max_negatives:
                offset = (i * max_negatives) % len(eligible)
                eligible = (eligible[offset:] + eligible[:offset])[:max_negatives]

            candidates = [i] + eligible
            start = len(pair_prompt_indices)
            for j in candidates:
                pair_prompt_indices.append(j)
                pair_response_indices.append(i)
            row_slices.append((start, len(pair_prompt_indices)))

        if len(pair_prompt_indices) == batch_size:
            return batch, {"actor/mi_proxy/skipped": 1.0}

        device = prompts.device
        pair_prompt_idx = torch.tensor(pair_prompt_indices, device=device, dtype=torch.long)
        pair_response_idx = torch.tensor(pair_response_indices, device=device, dtype=torch.long)

        pair_prompts = prompts[pair_prompt_idx]
        pair_responses = responses[pair_response_idx]
        pair_prompt_mask = attention_mask[pair_prompt_idx, :prompt_len].to(attention_mask.dtype)
        pair_response_attn_mask = attention_mask[
            pair_response_idx, prompt_len : prompt_len + response_len
        ].to(attention_mask.dtype)
        pair_response_mask = response_mask[pair_response_idx].to(attention_mask.dtype)
        pair_input_ids = torch.cat([pair_prompts, pair_responses], dim=1)
        pair_attention_mask = torch.cat([pair_prompt_mask, pair_response_attn_mask], dim=1)
        if position_ids.dim() == 2:
            pair_position_ids = torch.cat(
                [
                    position_ids[pair_prompt_idx, :prompt_len],
                    position_ids[pair_response_idx, prompt_len : prompt_len + response_len],
                ],
                dim=1,
            )
        else:
            pair_position_ids = torch.cat(
                [
                    position_ids[pair_prompt_idx, :, :prompt_len],
                    position_ids[pair_response_idx, :, prompt_len : prompt_len + response_len],
                ],
                dim=2,
            )

        pair_non_tensors = {}
        pair_prompt_idx_np = pair_prompt_idx.detach().cpu().numpy()
        for key in ("multi_modal_inputs", "uid"):
            if key in batch.non_tensor_batch:
                pair_non_tensors[key] = batch.non_tensor_batch[key][pair_prompt_idx_np]

        pair_batch = DataProto.from_dict(
            tensors={
                "input_ids": pair_input_ids,
                "prompts": pair_prompts,
                "responses": pair_responses,
                "attention_mask": pair_attention_mask,
                "response_mask": pair_response_mask,
                "position_ids": pair_position_ids,
            },
            non_tensors=pair_non_tensors,
            meta_info=dict(batch.meta_info),
        )
        pair_batch.meta_info["global_token_num"] = pair_attention_mask.sum(dim=-1).tolist()

        pair_old_log_prob, _ = self._compute_old_log_prob(pair_batch)
        pair_log_prob = pair_old_log_prob.batch["old_log_probs"]
        pair_seq_scores = masked_mean(pair_log_prob, pair_response_mask, axis=-1)

        mi_temperature = float(self.config.algorithm.get("mi_reward_temperature", 1.0))
        mi_temperature = max(mi_temperature, 1e-6)
        mi_zscore_eps = float(self.config.algorithm.get("mi_reward_zscore_eps", 1e-3))
        ema_alpha = float(self.config.algorithm.get("mi_reward_ema_alpha", 0.9))

        matched_scores = []
        marginal_scores = []
        raw_scores = []
        retrieval_hits = []
        contrastive_losses = []
        candidate_counts = []

        for row_idx, (start, end) in enumerate(row_slices):
            row_scores = pair_seq_scores[start:end]
            if row_scores.numel() < 2:
                continue

            candidate_counts.append(row_scores.numel())
            matched = row_scores[0]
            marginal = torch.logsumexp(row_scores, dim=0) - torch.log(
                torch.tensor(float(row_scores.numel()), device=row_scores.device, dtype=row_scores.dtype)
            )
            raw = matched - marginal
            retrieval_hits.append((torch.argmax(row_scores) == 0).float())
            matched_scores.append(matched)
            marginal_scores.append(marginal)
            raw_scores.append(raw)
            contrastive_losses.append(-torch.log_softmax(row_scores / mi_temperature, dim=0)[0])

        if not raw_scores:
            return batch, {"actor/mi_proxy/skipped": 1.0}

        matched_tensor = torch.stack(matched_scores)
        marginal_tensor = torch.stack(marginal_scores)
        raw_tensor = torch.stack(raw_scores)
        retrieval_tensor = torch.stack(retrieval_hits)
        contrastive_tensor = torch.stack(contrastive_losses)
        candidate_tensor = torch.tensor(candidate_counts, device=raw_tensor.device, dtype=raw_tensor.dtype)

        current_std = marginal_tensor.std(unbiased=False)
        ema_key = "_mi_proxy_marginal_std_ema"
        prev_ema = getattr(self, ema_key, None)
        if prev_ema is None:
            ema_value = current_std.detach().item()
        else:
            ema_value = ema_alpha * prev_ema + (1.0 - ema_alpha) * current_std.detach().item()
        setattr(self, ema_key, ema_value)

        denom = max(ema_value, mi_zscore_eps)
        mi_zscore = raw_tensor / denom
        mi_reward_signal = mi_zscore
        zscore_clip = float(self.config.algorithm.get("mi_reward_zscore_clip", 0.0))
        if zscore_clip > 0.0:
            mi_reward_signal = mi_reward_signal.clamp(min=-zscore_clip, max=zscore_clip)

        warmup_steps = int(self.config.algorithm.get("mi_reward_warmup_steps", 0))
        if warmup_steps > 0:
            warmup_scale = min(1.0, max(0.0, float(self.global_steps)) / float(warmup_steps))
        else:
            warmup_scale = 1.0

        target_zscore = self.config.algorithm.get("mi_reward_target_zscore", None)
        target_scale = 1.0
        if target_zscore is not None:
            target_zscore = float(target_zscore)
            batch_zscore_mean = mi_zscore.detach().mean().item()
            if target_zscore > 0.0:
                target_scale = (target_zscore - batch_zscore_mean) / target_zscore
                target_scale = min(1.0, max(0.0, target_scale))
            else:
                target_scale = 0.0 if batch_zscore_mean >= target_zscore else 1.0

        mi_reward = mi_reward_coef * warmup_scale * target_scale * mi_reward_signal
        reward_clip = float(self.config.algorithm.get("mi_reward_clip", 0.0))
        if reward_clip > 0.0:
            mi_reward = mi_reward.clamp(min=-reward_clip, max=reward_clip)

        success_threshold = self.config.algorithm.get("mi_reward_success_threshold", None)
        success_mask = None
        if success_threshold is not None and "token_level_scores" in batch.batch:
            base_scores = batch.batch["token_level_scores"].sum(dim=-1)
            success_mask = base_scores > float(success_threshold)
            mi_reward = torch.where(success_mask, mi_reward, torch.zeros_like(mi_reward))

        valid_response_mask = response_mask.sum(dim=-1) > 0
        if valid_response_mask.any():
            response_positions = torch.arange(response_len, device=response_mask.device).unsqueeze(0)
            last_response_idx = torch.where(
                response_mask.bool(),
                response_positions.expand_as(response_mask),
                torch.zeros_like(response_mask, dtype=torch.long),
            ).max(dim=-1).values
            batch_reward = torch.zeros_like(batch.batch["token_level_rewards"])
            row_ids = torch.arange(batch_size, device=batch_reward.device)[valid_response_mask]
            batch_reward[row_ids, last_response_idx[valid_response_mask]] = mi_reward[valid_response_mask]
            batch.batch["token_level_rewards"] = batch.batch["token_level_rewards"] + batch_reward

        metrics = {
            "actor/mi_proxy/enabled": 1.0,
            "actor/mi_proxy/coef": mi_reward_coef,
            "actor/mi_proxy/coef_effective": mi_reward_coef * warmup_scale,
            "actor/mi_proxy/target_scale": target_scale,
            "actor/mi_proxy/matched_mean": matched_tensor.mean().detach().item(),
            "actor/mi_proxy/marginal_mean": marginal_tensor.mean().detach().item(),
            "actor/mi_proxy/raw_mean": raw_tensor.mean().detach().item(),
            "actor/mi_proxy/zscore_mean": mi_zscore.mean().detach().item(),
            "actor/mi_proxy/reward_mean": mi_reward.mean().detach().item(),
            "actor/mi_proxy/reward_max": mi_reward.max().detach().item(),
            "actor/mi_proxy/reward_min": mi_reward.min().detach().item(),
            "actor/mi_proxy/retrieval_acc": retrieval_tensor.mean().detach().item(),
            "actor/mi_proxy/contrastive_loss": contrastive_tensor.mean().detach().item(),
            "actor/mi_proxy/marginal_std": current_std.detach().item(),
            "actor/mi_proxy/marginal_std_ema": ema_value,
            "actor/mi_proxy/candidate_count_mean": candidate_tensor.mean().detach().item(),
        }
        if success_mask is not None:
            metrics["actor/mi_proxy/success_gate_rate"] = success_mask.float().mean().detach().item()

        return batch, metrics

    def _update_actor(self, batch: DataProto) -> DataProto:
        rollout_config = self.config.actor_rollout_ref.rollout
        batch.meta_info["multi_turn"] = rollout_config.multi_turn.enable
        # TODO: Make "temperature" single source of truth from generation.
        batch.meta_info["temperature"] = rollout_config.temperature
        
        # Decide whether to use SeeUPO update (requires turn_index built during compute_advantage)

        loss_mode = self.config.actor_rollout_ref.actor.policy_loss.get("loss_mode", "vanilla")
        use_seeupo = (loss_mode in ("seeupo_turn", "seeupo_turn_new") and "turn_index" in batch.batch)
        # update actor

        if self.use_legacy_worker_impl == "disable":
            batch_td = batch.to_tensordict()
            # step 2: convert from padding to no-padding
            batch_td = left_right_2_no_padding(batch_td)
            needs_token_entropy = loss_mode in {"entropy_safe_token", "tespo", "entropy_reward_first", "erf"}
            calculate_entropy = self.config.actor_rollout_ref.actor.entropy_coeff != 0.0 or needs_token_entropy
            ppo_mini_batch_size = self.config.actor_rollout_ref.actor.ppo_mini_batch_size
            ppo_mini_batch_size = ppo_mini_batch_size * self.config.actor_rollout_ref.rollout.n
            ppo_epochs = self.config.actor_rollout_ref.actor.ppo_epochs
            seed = self.config.actor_rollout_ref.actor.data_loader_seed
            shuffle = self.config.actor_rollout_ref.actor.shuffle
            tu.assign_non_tensor(
                batch_td,
                calculate_entropy=calculate_entropy,
                global_batch_size=ppo_mini_batch_size,
                mini_batch_size=ppo_mini_batch_size,
                epochs=ppo_epochs,
                seed=seed,
                dataloader_kwargs={"shuffle": shuffle},
            )
            
            if use_seeupo:
                actor_output = self.actor_rollout_wg.update_actor_seeupo(batch_td)
            else:
                actor_output = self.actor_rollout_wg.update_actor(batch_td)
                
            erl_distill_coeff = self.config.algorithm.get("erl_distill_coeff", 0.0)
            if erl_distill_coeff > 0.0:
                distill_td = build_distill_batch(
                    rollout_batch=batch,           # your DataProto rollout batch
                    prompt_length=self.config.data.max_prompt_length,
                    response_length=self.config.data.max_response_length,
                    dp_size=int(self.config.trainer.n_gpus_per_node / self.config.actor_rollout_ref.actor.megatron.tensor_model_parallel_size / self.config.actor_rollout_ref.actor.megatron.context_parallel_size),  # or however you access dp_size

                )
                if distill_td is not None:
                    # Scale loss if coeff != 1
                    if erl_distill_coeff != 1.0:
                        # Inject coeff so loss_fn can use it (optional — or just bake into          nll)
                        tu.assign_non_tensor(distill_td,
                            erl_distill_coeff=NonTensorData(erl_distill_coeff))
                    distill_output = self.actor_rollout_wg.update_actor_distill(distill_td)
                  
            actor_output = tu.get(actor_output, "metrics")
            
            # ── merge distill metrics into actor_output before rename ────────────
            if erl_distill_coeff > 0.0 and distill_td is not None and distill_output is not None:
                distill_metrics = tu.get(distill_output, "metrics", default={})
                if isinstance(distill_metrics, dict):
                    actor_output.update({f"erl/{k}": v for k, v in distill_metrics.items()})
                    
            actor_output = rename_dict(actor_output, "actor/")
            # modify key name
            actor_output["perf/mfu/actor"] = actor_output.pop("actor/mfu")
            actor_output = DataProto.from_single_dict(data={}, meta_info={"metrics": actor_output})
        else:
            if use_seeupo:
                actor_output = self.actor_rollout_wg.update_actor_seeupo(batch)
            else:
                actor_output = self.actor_rollout_wg.update_actor(batch)


            # ── ERL distillation update ─────────────────────────────────────────
            erl_distill_coeff = self.config.algorithm.get("erl_distill_coeff", 0.0)
            if erl_distill_coeff > 0:
                distill_batch = build_distill_batch(batch)
                if distill_batch is not None:
                    # Tag so forward_backward_batch uses SFT loss (ratio=1, no clipping)
                    distill_batch.meta_info["erl_distill_coeff"] = erl_distill_coeff
                    distill_output = self.actor_rollout_ref_wg.update_actor_distill(distill_batch)
                    # Log distill metrics
                    distill_metrics = distill_output.meta_info.get("metrics", {})
                    self._log_metrics({f"erl/{k}": v for k, v in distill_metrics.items()})
        return actor_output

    def _update_critic(self, batch: DataProto) -> DataProto:
        if self.use_legacy_worker_impl == "disable":
            batch_td = batch.to_tensordict()
            # step 2: convert from padding to no-padding
            batch_td = left_right_2_no_padding(batch_td)
            ppo_mini_batch_size = self.config.critic.ppo_mini_batch_size
            ppo_mini_batch_size = ppo_mini_batch_size * self.config.actor_rollout_ref.rollout.n
            ppo_epochs = self.config.critic.ppo_epochs
            seed = self.config.critic.data_loader_seed
            shuffle = self.config.critic.shuffle
            tu.assign_non_tensor(
                batch_td,
                global_batch_size=ppo_mini_batch_size,
                mini_batch_size=ppo_mini_batch_size,
                epochs=ppo_epochs,
                seed=seed,
                dataloader_kwargs={"shuffle": shuffle},
            )

            output = self.critic_wg.train_mini_batch(batch_td)
            output = output.get()
            output = tu.get(output, "metrics")
            output = rename_dict(output, "critic/")
            # modify key name
            output["perf/mfu/critic"] = output.pop("critic/mfu")
            critic_output = DataProto.from_single_dict(data={}, meta_info={"metrics": output})
        else:
            critic_output = self.critic_wg.update_critic(batch)
        return critic_output

    def fit(self):
        """
        The training loop of PPO.
        The driver process only need to call the compute functions of the worker group through RPC
        to construct the PPO dataflow.
        The light-weight advantage computation is done on the driver process.
        """
        from omegaconf import OmegaConf

        from verl.utils.tracking import Tracking

        logger = Tracking(
            project_name=self.config.trainer.project_name,
            experiment_name=self.config.trainer.experiment_name,
            default_backend=self.config.trainer.logger,
            config=OmegaConf.to_container(self.config, resolve=True),
        )

        self.global_steps = 0

        # load checkpoint and update weights before doing anything
        self._load_checkpoint()
        self.checkpoint_manager.update_weights(self.global_steps)

        current_epoch = self.global_steps // len(self.train_dataloader)

        # perform validation before training
        # currently, we only support validation using the reward_function.
        validate_before_train  = getattr(self.config.trainer, "validate_before_train", False)

        if validate_before_train:
            val_metrics = self._validate_multi()
            assert val_metrics, f"{val_metrics=}"
            pprint(f"Initial validation metrics: {val_metrics}")
            logger.log(data=val_metrics, step=self.global_steps)
            if self.config.trainer.get("val_only", False):
                return

        if self.config.actor_rollout_ref.rollout.get("skip_rollout", False):
            rollout_skip = RolloutSkip(self.config, self.async_rollout_manager)
            rollout_skip.wrap_generate_sequences()

        # add tqdm
        progress_bar = tqdm(total=self.total_training_steps, initial=self.global_steps, desc="Training Progress")

        # we start from step 1
        self.global_steps += 1
        last_val_metrics = None
        self.max_steps_duration = 0

        prev_step_profile = False
        curr_step_profile = (
            self.global_steps in self.config.global_profiler.steps
            if self.config.global_profiler.steps is not None
            else False
        )
        next_step_profile = False

        for epoch in range(current_epoch, self.config.trainer.total_epochs):
            for batch_dict in self.train_dataloader:
                if hasattr(self.actor_rollout_wg, "async_calls_finalize_fn_exec"):
                    self.actor_rollout_wg.async_calls_finalize_fn_exec(blocking=False)
                metrics = {}
                timing_raw = {}

                with marked_timer("start_profile", timing_raw):
                    self._start_profiling(
                        not prev_step_profile and curr_step_profile
                        if self.config.global_profiler.profile_continuous_steps
                        else curr_step_profile
                    )
                batch: DataProto = DataProto.from_single_dict(batch_dict)
                batch.meta_info["temperature"] = self.config.actor_rollout_ref.rollout.temperature

                # add uid to batch
                batch.non_tensor_batch["uid"] = np.array(
                    [str(uuid.uuid4()) for _ in range(len(batch.batch))], dtype=object
                )

                gen_batch = self._get_gen_batch(batch)

                # pass global_steps to trace
                gen_batch.meta_info["global_steps"] = self.global_steps
                gen_batch_output = gen_batch.repeat(
                    repeat_times=self.config.actor_rollout_ref.rollout.n, interleave=True
                )

                is_last_step = self.global_steps >= self.total_training_steps
                with marked_timer("step", timing_raw):
                    # generate a batch
                    with marked_timer("gen", timing_raw, color="red"):
                        if curr_step_profile:
                            self.async_rollout_manager.start_profile()
                        gen_batch_output = self.async_rollout_manager.generate_sequences(gen_batch_output)
                        self.checkpoint_manager.sleep_replicas()
                        if curr_step_profile:
                            self.async_rollout_manager.stop_profile()

                        timing_raw.update(gen_batch_output.meta_info["timing"])
                        gen_batch_output.meta_info.pop("timing", None)

                    if self.config.algorithm.adv_estimator == AdvantageEstimator.REMAX:
                        with marked_timer("gen_max", timing_raw, color="purple"):
                            gen_baseline_batch = deepcopy(gen_batch)
                            gen_baseline_batch.meta_info["do_sample"] = False
                            if curr_step_profile:
                                self.async_rollout_manager.start_profile()
                            gen_baseline_output = self.async_rollout_manager.generate_sequences(gen_baseline_batch)
                            self.checkpoint_manager.sleep_replicas()
                            if curr_step_profile:
                                self.async_rollout_manager.stop_profile()
                            batch = batch.union(gen_baseline_output)
                            # compute reward model score on batch
                            rm_scores = None
                            if self.use_rm and "rm_scores" not in batch.batch.keys():
                                batch_reward = self._compute_reward_colocate(batch)
                                batch = batch.union(batch_reward)

                            # Compute or extract reward for REMAX baseline
                            reward_baseline_tensor = batch.batch["rm_scores"].sum(dim=-1)

                            keys_to_pop = set(gen_baseline_output.batch.keys())
                            if rm_scores is not None:
                                keys_to_pop.update(rm_scores.batch.keys())
                            batch.pop(batch_keys=list(keys_to_pop))

                            batch.batch["reward_baselines"] = reward_baseline_tensor

                            del rm_scores, gen_baseline_batch, gen_baseline_output
                    # repeat to align with repeated responses in rollout
                    batch = batch.repeat(repeat_times=self.config.actor_rollout_ref.rollout.n, interleave=True)
                    batch = batch.union(gen_batch_output)

                    if "response_mask" not in batch.batch.keys():
                        batch.batch["response_mask"] = compute_response_mask(batch)
                    # Balance the number of valid tokens across DP ranks.
                    # NOTE: This usually changes the order of data in the `batch`,
                    # which won't affect the advantage calculation (since it's based on uid),
                    # but might affect the loss calculation (due to the change of mini-batching).
                    if self.config.trainer.balance_batch:
                        self._balance_batch(batch, metrics=metrics)

                    # compute global_valid tokens
                    batch.meta_info["global_token_num"] = torch.sum(batch.batch["attention_mask"], dim=-1).tolist()
                    # get images_seqlens
                    images_seqlens_all = []
                    for multi_modal_input in batch.non_tensor_batch["multi_modal_inputs"]:
                        if "image_grid_thw" not in multi_modal_input.keys():
                            continue
                        images_seqlens_all.extend(multi_modal_input["images_seqlens"].tolist())
                    batch.meta_info["images_seqlens"] = images_seqlens_all
                    with marked_timer("reward", timing_raw, color="yellow"):
                        # compute reward model score
                        if self.use_rm and "rm_scores" not in batch.batch.keys():
                            batch_reward = self._compute_reward_colocate(batch)
                            batch = batch.union(batch_reward)

                        # extract reward_tensor and reward_extra_infos_dict for training
                        reward_tensor, reward_extra_infos_dict = extract_reward(batch)

                    # Operating Mode Selection:
                    # - Bypass mode: Sets old_log_probs = rollout_log_probs (2 policies: π_rollout, π_θ)
                    # - Decoupled mode: Recomputes old_log_probs as proximal anchor (3 policies: π_rollout, π_old, π_θ)
                    #   Note: π_old computed once per data batch, serves as stable reference during mini-batch updates
                    rollout_corr_config = self.config.algorithm.get("rollout_correction", None)
                    bypass_recomputing_logprobs = rollout_corr_config and rollout_corr_config.get("bypass_mode", False)
                    if bypass_recomputing_logprobs:  # Use `rollout_log_probs`
                        from verl.trainer.ppo.rollout_corr_helper import apply_bypass_mode

                        apply_bypass_mode(
                            batch=batch,
                            rollout_corr_config=rollout_corr_config,
                            policy_loss_config=self.config.actor_rollout_ref.actor.policy_loss,
                        )
                    else:  # Recompute old_log_probs
                        with marked_timer("old_log_prob", timing_raw, color="blue"):
                            old_log_prob, old_log_prob_mfu = self._compute_old_log_prob(batch)
                            entropys = old_log_prob.batch["entropys"]
                            response_masks = batch.batch["response_mask"]
                            actor_config = self.config.actor_rollout_ref.actor
                            entropy_agg = agg_loss(
                                loss_mat=entropys,
                                loss_mask=response_masks,
                                loss_agg_mode=actor_config.loss_agg_mode,
                                loss_scale_factor=actor_config.loss_scale_factor,
                            )
                            old_log_prob_metrics = {
                                "actor/entropy": entropy_agg.detach().item(),
                                "perf/mfu/actor_infer": old_log_prob_mfu,
                            }
                            metrics.update(old_log_prob_metrics)
                            old_log_prob.batch.pop("entropys")
                            if "routed_experts" in batch.batch and "routed_experts" in old_log_prob.batch:
                                raise ValueError(
                                    "Detected conflicting router replay configuration: "
                                    "router_replay.mode='R2' and enable_rollout_routing_replay=True "
                                    "cannot be enabled simultaneously. "
                                    "The enable_rollout_routing_replay option is only used in R3 mode; "
                                    "it should not be set when using R2 mode."
                                )
                            batch = batch.union(old_log_prob)
                            if "rollout_log_probs" in batch.batch.keys():
                                # TODO: we may want to add diff of probs too.
                                from verl.utils.debug.metrics import calculate_debug_metrics

                                metrics.update(calculate_debug_metrics(batch))

                    assert "old_log_probs" in batch.batch, f'"old_log_prob" not in {batch.batch.keys()=}'

                    if self.use_reference_policy:
                        # compute reference log_prob
                        with marked_timer(str(Role.RefPolicy), timing_raw, color="olive"):
                            ref_log_prob = self._compute_ref_log_prob(batch)
                            batch = batch.union(ref_log_prob)

                    # compute values
                    if self.use_critic:
                        with marked_timer("values", timing_raw, color="cyan"):
                            values = self._compute_values(batch)
                            batch = batch.union(values)

                    with marked_timer("adv", timing_raw, color="brown"):
                        # we combine with rule-based rm
                        reward_extra_infos_dict: dict[str, list]
                        batch.batch["token_level_scores"] = reward_tensor

                        if reward_extra_infos_dict:
                            batch.non_tensor_batch.update({k: np.array(v) for k, v in reward_extra_infos_dict.items()})

                        # compute rewards. apply_kl_penalty if available
                        if self.config.algorithm.use_kl_in_reward:
                            batch, kl_metrics = apply_kl_penalty(
                                batch, kl_ctrl=self.kl_ctrl_in_reward, kl_penalty=self.config.algorithm.kl_penalty
                            )
                            metrics.update(kl_metrics)
                        else:
                            batch.batch["token_level_rewards"] = batch.batch["token_level_scores"]

                        # Compute rollout correction: IS weights, rejection sampling, and metrics
                        # Only runs in decoupled mode (computes once per batch using stable π_old)
                        # In bypass mode, this is skipped - actor computes metrics from evolving π_θ vs π_rollout
                        if (
                            rollout_corr_config is not None
                            and "rollout_log_probs" in batch.batch
                            and not bypass_recomputing_logprobs  # Only in decoupled mode
                        ):
                            from verl.trainer.ppo.rollout_corr_helper import compute_rollout_correction_and_add_to_batch

                            # Compute IS weights, apply rejection sampling, compute metrics
                            batch, is_metrics = compute_rollout_correction_and_add_to_batch(batch, rollout_corr_config)
                            # IS and off-policy metrics already have rollout_corr/ prefix
                            metrics.update(is_metrics)

                        mi_reward_coef = float(self.config.algorithm.get("mi_reward_coef", 0.0))
                        if mi_reward_coef > 0.0:
                            mi_reward_output = self._compute_mi_proxy_reward(batch)
                            if mi_reward_output is not None:
                                batch, mi_metrics = mi_reward_output
                                metrics.update(mi_metrics)

                        # compute advantages, executed on the driver process
                        norm_adv_by_std_in_grpo = self.config.algorithm.get(
                            "norm_adv_by_std_in_grpo", True
                        )  # GRPO adv normalization factor

                        batch = compute_advantage(
                            batch,
                            adv_estimator=self.config.algorithm.adv_estimator,
                            gamma=self.config.algorithm.gamma,
                            lam=self.config.algorithm.lam,
                            num_repeat=self.config.actor_rollout_ref.rollout.n,
                            norm_adv_by_std_in_grpo=norm_adv_by_std_in_grpo,
                            config=self.config.algorithm,
                        )
                        adv_metrics = batch.meta_info.pop("adv_metrics", None)
                        if adv_metrics:
                            metrics.update(adv_metrics)

                    # update critic
                    if self.use_critic:
                        with marked_timer("update_critic", timing_raw, color="pink"):
                            critic_output = self._update_critic(batch)
                        critic_output_metrics = reduce_metrics(critic_output.meta_info["metrics"])
                        metrics.update(critic_output_metrics)

                    # implement critic warmup
                    if self.config.trainer.critic_warmup <= self.global_steps:
                        # update actor
                   # With:
                        with marked_timer("update_actor", timing_raw, color="red"):
                            actor_output = self._update_actor(batch)   # single-turn fallback


                        # Check if the ESI (Elastic Server Instance)/training plan is close to expiration.
                        esi_close_to_expiration = should_save_ckpt_esi(
                            max_steps_duration=self.max_steps_duration,
                            redundant_time=self.config.trainer.esi_redundant_time,
                        )
                        # Check if the conditions for saving a checkpoint are met.
                        # The conditions include a mandatory condition (1) and
                        # one of the following optional conditions (2/3/4):
                        # 1. The save frequency is set to a positive value.
                        # 2. It's the last training step.
                        # 3. The current step number is a multiple of the save frequency.
                        # 4. The ESI(Elastic Server Instance)/training plan is close to expiration.
                        if self.config.trainer.save_freq > 0 and (
                            is_last_step
                            or self.global_steps % self.config.trainer.save_freq == 0
                            or esi_close_to_expiration
                        ):
                            if esi_close_to_expiration:
                                print("Force saving checkpoint: ESI instance expiration approaching.")
                            with marked_timer("save_checkpoint", timing_raw, color="green"):
                                self._save_checkpoint()

                        # update weights from trainer to rollout
                        with marked_timer("update_weights", timing_raw, color="red"):
                            self.checkpoint_manager.update_weights(self.global_steps)

                        actor_output_metrics = reduce_metrics(actor_output.meta_info["metrics"])
                        metrics.update(actor_output_metrics)

                    # Log rollout generations if enabled
                    rollout_data_dir = self.config.trainer.get("rollout_data_dir", None)
                    if rollout_data_dir:
                        self._log_rollout_data(batch, reward_extra_infos_dict, timing_raw, rollout_data_dir)

                # validate
                if self.config.trainer.test_freq > 0 and (
                    is_last_step or self.global_steps % self.config.trainer.test_freq == 0
                ):
                    with marked_timer("testing", timing_raw, color="green"):
                        val_metrics: dict = self._validate_multi()
                        if is_last_step:
                            last_val_metrics = val_metrics
                    metrics.update(val_metrics)

                with marked_timer("stop_profile", timing_raw):
                    next_step_profile = (
                        self.global_steps + 1 in self.config.global_profiler.steps
                        if self.config.global_profiler.steps is not None
                        else False
                    )
                    self._stop_profiling(
                        curr_step_profile and not next_step_profile
                        if self.config.global_profiler.profile_continuous_steps
                        else curr_step_profile
                    )
                    prev_step_profile = curr_step_profile
                    curr_step_profile = next_step_profile

                steps_duration = timing_raw["step"]
                self.max_steps_duration = max(self.max_steps_duration, steps_duration)

                # training metrics
                metrics.update(
                    {
                        "training/global_step": self.global_steps,
                        "training/epoch": epoch,
                    }
                )
                # collect metrics
                metrics.update(compute_data_metrics(batch=batch, use_critic=self.use_critic))
                metrics.update(compute_reward_extra_metrics(reward_extra_infos_dict))
                # GDPO per-component reward metrics
                gdpo_reward_keys = self.config.algorithm.get("gdpo_reward_keys", None)
                if gdpo_reward_keys and self.config.algorithm.adv_estimator in ("gdpo", AdvantageEstimator.GDPO):
                    for key in gdpo_reward_keys:
                        if key in batch.non_tensor_batch:
                            vals = np.asarray(batch.non_tensor_batch[key], dtype=np.float32)
                            metrics[f"gdpo/{key}/mean"] = float(np.mean(vals))
                            metrics[f"gdpo/{key}/std"] = float(np.std(vals))
                            metrics[f"gdpo/{key}/max"] = float(np.max(vals))
                            metrics[f"gdpo/{key}/min"] = float(np.min(vals))
                metrics.update(compute_timing_metrics(batch=batch, timing_raw=timing_raw))
                # TODO: implement actual tflpo and theoretical tflpo
                n_gpus = self.resource_pool_manager.get_n_gpus()
                metrics.update(compute_throughout_metrics(batch=batch, timing_raw=timing_raw, n_gpus=n_gpus))
                # compute variance proxy metrics
                gradient_norm = metrics.get("actor/grad_norm", None)
                metrics.update(compute_variance_proxy_metrics(batch=batch, gradient_norm=gradient_norm))
                # Note: mismatch metrics (KL, PPL, etc.) are collected at line 1179 after advantage computation

                # this is experimental and may be changed/removed in the future in favor of a general-purpose one
                if isinstance(self.train_dataloader.sampler, AbstractCurriculumSampler):
                    self.train_dataloader.sampler.update(batch=batch)

                # TODO: make a canonical logger that supports various backend
                logger.log(data=metrics, step=self.global_steps)

                progress_bar.update(1)
                self.global_steps += 1

                if (
                    hasattr(self.config.actor_rollout_ref.actor, "profiler")
                    and self.config.actor_rollout_ref.actor.profiler.tool == "torch_memory"
                ):
                    self.actor_rollout_wg.dump_memory_snapshot(
                        tag=f"post_update_step{self.global_steps}", sub_dir=f"step{self.global_steps}"
                    )

                if is_last_step:
                    if hasattr(self.actor_rollout_wg, "async_calls_finalize_fn_exec"):
                        self.actor_rollout_wg.async_calls_finalize_fn_exec(blocking=True)
                    pprint(f"Final validation metrics: {last_val_metrics}")
                    progress_bar.close()
                    return

                # this is experimental and may be changed/removed in the future
                # in favor of a general-purpose data buffer pool
                if hasattr(self.train_dataset, "on_batch_end"):
                    # The dataset may be changed after each training batch
                    self.train_dataset.on_batch_end(batch=batch)
