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
import functools
import logging
import os
from contextlib import nullcontext
from copy import deepcopy
from functools import partial
from itertools import chain

import torch
from codetiming import Timer
from omegaconf import DictConfig, open_dict
from tensordict import NonTensorData, TensorDict
from torch.distributed.device_mesh import init_device_mesh

from verl.checkpoint_engine import CheckpointEngineRegistry
from verl.single_controller.base import Worker
from verl.single_controller.base.decorator import Dispatch, make_nd_compute_dataproto_dispatch_fn, register
from verl.trainer.ppo.core_algos import build_state_predictive_index_from_update_sketch
from verl.utils import tensordict_utils as tu
from verl.utils.config import omega_conf_to_dataclass
from verl.utils.device import get_device_id, get_device_name, set_expandable_segments
from verl.utils.distributed import initialize_global_process_group_ray
from verl.utils.flops_counter import FlopsCounter
from verl.utils.memory_utils import aggressive_empty_cache
from verl.utils.metric.utils import Metric
from verl.utils.profiler import DistProfiler, DistProfilerExtension, ProfilerConfig, log_gpu_memory_usage
from verl.utils.py_functional import append_to_dict
from verl.utils.tensordict_utils import maybe_fix_3d_position_ids
from verl.utils.torch_functional import allgather_dict_into_dict
from verl.workers.config import ActorConfig, HFModelConfig, MtpConfig, RolloutConfig, TrainingWorkerConfig
from verl.workers.rollout.base import BaseRollout, get_rollout_class
from verl.workers.utils.losses import ppo_loss

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))
import math
from collections import defaultdict

MAX_UPDATES = 3

def _nested_to_dense(
    nt: torch.Tensor,
    pad_value: int,
    target_len: int,
) -> torch.Tensor:
    """Convert a NestedTensor (B, j1) → dense (B, target_len) with left-padding."""
    sequences = nt.unbind()
    B = len(sequences)
    out = torch.full((B, target_len), pad_value,
                     dtype=sequences[0].dtype, device=sequences[0].device)
    for i, seq in enumerate(sequences):
        L = min(seq.shape[0], target_len)
        out[i, target_len - L:] = seq[-L:]   # left-pad convention
    return out


def _pad_dense_to(
    t: torch.Tensor,
    target_len: int,
    pad_value: int = 0,
) -> torch.Tensor:
    """Pad a dense (B, seq_len) tensor to (B, target_len) on the left."""
    cur_len = t.shape[-1]
    if cur_len >= target_len:
        return t[..., -target_len:]
    pad_shape = list(t.shape)
    pad_shape[-1] = target_len - cur_len
    pad = t.new_full(pad_shape, pad_value)
    return torch.cat([pad, t], dim=-1)


def _with_routing_replay_flag(enabled: bool):
    """Decorator to set 'enable_routing_replay' flag on the data TensorDict."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, data: TensorDict, *args, **kwargs):
            if self.enable_routing_replay:
                tu.assign_non_tensor_data(data, "enable_routing_replay", enabled)
            return func(self, data, *args, **kwargs)

        return wrapper

    return decorator


def _geomean_prob(log_probs: torch.Tensor) -> float:
    """
    pi_theta(a|ctx) = exp( (1/L) * sum_j log_pi(a_j | ctx, a_{<j}) )  [Eq. 3]
    """
    if log_probs.numel() == 0:
        return 0.0
    return math.exp(log_probs.float().mean().item())
 
 
def _build_position_ids(attention_mask: torch.Tensor) -> torch.Tensor:
    """1-D position IDs, left-padding aware."""
    return (attention_mask.long().cumsum(dim=-1) - 1).clamp(min=0)
 
 
def _get_item(container, i: int):
    """Safely index a LinkedList or plain list/tuple."""
    try:
        return container[i]
    except (IndexError, KeyError, TypeError):
        return None
 
 
def _has_key(data, key: str) -> bool:
    """Check if key exists in a TensorDict or plain dict."""
    try:
        _ = data[key]
        return True
    except (KeyError, AttributeError):
        return False
 
 
def _pad_extra_data_to_multiple(extra_data, divisor: int):
    """
    Pad extra_data batch dim to be divisible by `divisor`.
    Returns (padded_extra_data, original_E, n_pad).
    """
    E = extra_data.batch_size[0]
    remainder = E % divisor
    if remainder == 0:
        return extra_data, E, 0

    n_pad = divisor - remainder
    # Repeat the last row n_pad times as dummy rows
    # (their log_probs will be discarded)
    last_idx = E - 1
    pad_rows = extra_data[last_idx:last_idx + 1].expand(n_pad)  # TensorDict broadcast
    # If expand isn't supported, clone explicitly:
    pad_list = [extra_data[last_idx:last_idx+1] for _ in range(n_pad)]
    from tensordict import torch as td_torch
    padded = torch.cat([extra_data] + pad_list, dim=0)  # TensorDict cat along batch dim
    return padded, E, n_pad
# ---------------------------------------------------------------------------
# 1.  IGPORewardBuilder
# ---------------------------------------------------------------------------
 
class IGPORewardBuilder:
    """
    Augments a compute_log_prob batch with gold-answer scoring rows, then
    post-processes the log-probs to produce per-token IG rewards.
 
    Reads directly from the TensorDict passed to compute_log_prob:
        data['turn_segs']         - per-sample turn segment lists
        data['reward_model']      - per-sample gold answer codes
        data['reward_extra_info'] - per-sample outcome rewards (.score)
 
    Usage
    -----
        builder, n_original = IGPORewardBuilder.maybe_create(
            data, tokenizer, response_length
        )
 
        output    = self.actor.infer_batch(data)   # unchanged
        log_probs = output["log_probs"]            # (B+E, response_length)
 
        IGPORewardBuilder.maybe_extract(builder, log_probs, data, n_original)
        # data["ig_token_rewards"] shape: (n_original, response_length)
        # All tensors trimmed back to n_original rows.
    """
 
    ANSWER_PREFIX = "<FINISHED>\n股票代码候选："

    def __init__(self, tokenizer, response_length, max_seg, max_turns_per_sample=5,
             infer_micro_batch_size_per_gpu=1, force_group_size=1):
        self.tokenizer        = tokenizer
        self.response_length  = response_length
        self.max_seg = max_seg
        self.max_turns_per_sample  = max_turns_per_sample   # <-- new
        self.pad_token_id     = getattr(tokenizer, "pad_token_id", 0) or 0
        self._prefix_ids: list[int] = tokenizer.encode(
            self.ANSWER_PREFIX, add_special_tokens=False
        )
        self._meta: list[dict]     = []
        self._extra_data           = None   # TensorDict of E scoring rows
        self.infer_micro_batch_size_per_gpu = infer_micro_batch_size_per_gpu
        self.force_group_size               = force_group_size
        self._E_orig                        = 0


    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def maybe_create(
        cls,
        data,
        tokenizer,
        response_length: int,
        seg_length:int,
        max_turns_per_sample: int = 100,   # <-- new
        infer_micro_batch_size_per_gpu: int = 1,
        force_group_size: int = 1,

    ) -> "IGPORewardBuilder | None":
        """
        Build scoring rows and store them internally.
        Returns builder, or None if IGPO data is absent.
        """
        if not (_has_key(data, "turn_segs") and _has_key(data, "reward_model")):
            return None

        builder = cls(tokenizer=tokenizer, response_length=response_length, max_seg=seg_length,  max_turns_per_sample=max_turns_per_sample, infer_micro_batch_size_per_gpu=infer_micro_batch_size_per_gpu, force_group_size=force_group_size)
        builder._build_extra_data(data)
        return builder

    @staticmethod
    def maybe_extract(
        builder: "IGPORewardBuilder | None",
        log_probs: torch.Tensor,
        data,
        infer_fn,
    ) -> None:
        """No-op if builder is None."""
        if builder is None:
            return
        builder.extract_ig_rewards(log_probs, data, infer_fn)

    # ------------------------------------------------------------------
    # Step A: augment
    # ------------------------------------------------------------------
 
    def _answer_ids(self, stock_code: str) -> list[int]:
        code_ids = self.tokenizer.encode(str(stock_code), add_special_tokens=False)
        return self._prefix_ids + code_ids
    
    def _build_extra_data(self, data) -> None:

        input_ids_nt   = data["input_ids"].clone()
        attention_mask = data["attention_mask"].clone()
        response_mask  = data["response_mask"].clone()   # original data uses response_mask
        loss_mask_src  = data.get("loss_mask", response_mask)  # fallback if loss_mask absent

        B          = input_ids_nt.size(0)
        sequences  = input_ids_nt.unbind()
        seq_lens   = torch.tensor(
            [s.shape[0] for s in sequences],
            dtype=torch.long, device=attention_mask.device,
        )
        prompt_lens = seq_lens - self.response_length
        # max_seq_len = int(seq_lens.max().item())
        max_seq_len = self.max_seg
        turn_segs_ll    = data["turn_segs"]
        reward_model_ll = data["reward_model"]

        extra_ids, extra_am, extra_lm = [], [], []   # lm = loss_mask
        self._meta = []
        
        for i in range(B):
            seq_i = sequences[i]
            print(f"[debug] i={i}, seq_i.shape={seq_i.shape}, "
                  f"attention_mask[i].sum()={attention_mask[i].sum().item()}, "
                  f"prompt_len_i={int(prompt_lens[i].item())}")
            break

        for i in range(B):
            segs_entry = _get_item(turn_segs_ll, i)
            rm_entry   = _get_item(reward_model_ll, i)
            if segs_entry is None or rm_entry is None:
                continue

            segs         = segs_entry
            gold_code    = str(rm_entry.get("ground_truth", ""))
            ans_ids      = self._answer_ids(gold_code)
            n_ans        = len(ans_ids)
            device       = attention_mask.device
            ans_t        = torch.tensor(ans_ids, dtype=torch.long, device=device)
            ans_am_t     = torch.ones(n_ans, dtype=attention_mask.dtype, device=device)

            seq_i        = sequences[i]
            # prompt_len_i = int(prompt_lens[i].item())
            actual_len   = seq_i.shape[0]
            response_mask_i = response_mask[i]  # (response_length,) = (2000,)
            actual_resp_total = int(response_mask_i.sum().item())  # real response tokens
            prompt_len_i = actual_len - actual_resp_total

            # ── turn subsampling ──────────────────────────────────────────
            N        = self.max_turns_per_sample
            T        = len(segs)
            # Divide T turns into min(T, N) buckets; each bucket's representative
            # is the LAST seg in the bucket (largest resp_end = most context).
            n_buckets = min(T, N)
            # Compute bucket boundary indices (exclusive end).
            # bucket k covers segs[ boundaries[k] : boundaries[k+1] ]
            boundaries = [int(round(T * k / n_buckets)) for k in range(n_buckets + 1)]
            bucket_segs = []
            for k in range(n_buckets):
                lo, hi = boundaries[k], boundaries[k + 1]
                if lo < hi:                          # guard against empty slice
                    bucket_segs.append(segs[hi - 1]) # last seg in bucket = largest context
            # ─────────────────────────────────────────────────────────────


            for seg in bucket_segs:
                t_idx    = int(seg["turn"]) - 1
                resp_end = int(seg["resp_end"])
                
                # Clamp resp_end to what actually exists in the sequence
                actual_resp_len = min(resp_end, actual_len - prompt_len_i)
                actual_resp_len = max(actual_resp_len, 0)           # guard against negatives


                p_ids = seq_i[:prompt_len_i]
                p_am  = attention_mask[i, :prompt_len_i]
                r_ids = seq_i[prompt_len_i : prompt_len_i + actual_resp_len]
                r_am  = attention_mask[i, prompt_len_i : prompt_len_i + actual_resp_len]
                
                row = torch.cat([p_ids, r_ids, ans_t])
                am  = torch.cat([p_am,  r_am,  ans_am_t])

                cur = row.shape[0]
                assert cur == am.shape[0], f"row/am length mismatch: {cur} vs {am.shape[0]}"
                if cur >= max_seq_len:
                    row = row[-max_seq_len:]
                    am  = am[-max_seq_len:]
                else:
                    pad = max_seq_len - cur
                    row = torch.cat([row.new_full((pad,), self.pad_token_id), row])
                    am  = torch.cat([am.new_zeros(pad), am])
                assert row.shape[0] == max_seq_len
                assert am.shape[0]  == max_seq_len
                # loss_mask: 1 only on gold-answer token positions (last n_ans of response window)
                # Shape must be (max_seq_len,) to match input_ids — NOT response_length
                lm = row.new_zeros(max_seq_len, dtype=torch.bool)
                n_ans_clamped = min(n_ans, max_seq_len)
                lm[-n_ans_clamped:] = True   # gold answer is always at the tail after our construction

                extra_ids.append(row)
                extra_am.append(am)
                extra_lm.append(lm)
                self._meta.append({
                    "sample_idx": i,
                    "turn_idx":   t_idx,
                    "n_ans":      n_ans,
                })

        if not extra_ids:
            self._extra_data = None
            return

        # ── pad to divisor before building nested tensors ────────────────
        micro_bsz        = getattr(self, "infer_micro_batch_size_per_gpu", 1)
        force_group_size = getattr(self, "force_group_size", 1)
        divisor          = micro_bsz * force_group_size
        E_orig           = len(extra_ids)
        n_pad            = (-E_orig) % divisor

        if n_pad > 0:
            # repeat last real row for each padding slot
            for _ in range(n_pad):
                extra_ids.append(extra_ids[-1].clone())
                extra_am.append(extra_am[-1].clone())
                extra_lm.append(extra_lm[-1].clone())
                # pad meta with a sentinel that _compute_ig_rewards_cpu won't see
                # (sample_idx=-1 ensures it's never matched)
                self._meta.append({"sample_idx": -1, "turn_idx": -1, "n_ans": 0})
        # ─────────────────────────────────────────────────────────────────
        self._E_orig = E_orig
        E_ids = torch.stack(extra_ids)   # (E, max_seq_len)
        E_am  = torch.stack(extra_am)    # (E, max_seq_len)
        E_lm  = torch.stack(extra_lm)    # (E, max_seq_len)  ← loss_mask


        E_ids_nested_list = []
        for row_idx in range(E_ids.shape[0]):
            am = E_am[row_idx]               # (max_seq_len,)
            valid = am.bool()
            E_ids_nested_list.append(E_ids[row_idx][valid])   # keep only non-padded tokens

        E_ids_nt = torch.nested.nested_tensor(
            E_ids_nested_list,
            dtype=E_ids.dtype,
            device=E_ids.device,
            layout=torch.jagged,             # same layout as original input_ids
        )
        from tensordict import TensorDict

        E_prompts_list = []
        E_responses_list = []

        for row_idx, meta in enumerate(self._meta):
            i = meta["sample_idx"]
            prompt_len_i = int(prompt_lens[i].item())
            am = E_am[row_idx]
            valid_ids = E_ids[row_idx][am.bool()]  #    actual     non-padded tokens

            # prompt is the first prompt_len_i tokens of    the    valid sequence
            # response is the remainder (original resp  slice    + gold answer)
            p = valid_ids[:prompt_len_i]
            r = valid_ids[prompt_len_i:]
            E_prompts_list.append(p)
            E_responses_list.append(r)

            E_prompts_nt = torch.nested.nested_tensor(
                E_prompts_list,
                dtype=E_ids.dtype,
                device=E_ids.device,
                layout=torch.jagged,
            )
            E_responses_nt = torch.nested.nested_tensor(
                E_responses_list,
                dtype=E_ids.dtype,
                device=E_ids.device,
                layout=torch.jagged,
            )
        extra = TensorDict(
        {
            "input_ids":      E_ids_nt,
            "attention_mask": E_am,
            "loss_mask":      E_lm,
            "prompts":        E_prompts_nt,   # ← added
            "responses":      E_responses_nt, # ← added
        },
        batch_size=[len(extra_ids)],
        device=attention_mask.device,
    )

        # position_ids
        try:
            pid  = data["position_ids"]
            epos = _build_position_ids(E_am)
            if pid.dim() == 3:
                epos = epos.unsqueeze(1).expand(-1, pid.shape[1], -1)
            extra["position_ids"] = epos
        except Exception:
            pass


       # temperature — handle both scalar and tensor
        try:
            t_orig = data["temperature"]
            E = len(self._meta)
            if isinstance(t_orig, torch.Tensor) and t_orig.dim() >= 1 and t_orig.shape[0] == B:
                # per-sample tensor: copy each sample's temperature
                extra_temps = torch.stack([t_orig[m["sample_idx"]] for m in self._meta])
                tu.assign_non_tensor(extra, temperature=extra_temps)
            else:
                # scalar float/int — just replicate it as a non-tensor
                scalar_val = float(t_orig) if not isinstance(t_orig, float) else t_orig
                tu.assign_non_tensor(extra, temperature=scalar_val)
        except Exception:
            # fallback: default temperature 1.0
            tu.assign_non_tensor(extra, temperature=1.0)
            
        
        # After building extra TensorDict, copy non-tensor metadata from original data
        # that the loss function requires

        _NON_TENSOR_PASSTHROUGH = [
            "global_steps",
            "compute_loss",
            "multi_modal_inputs"
        ]

        for key in _NON_TENSOR_PASSTHROUGH:
            try:
                val = tu.get_non_tensor_data(data, key=key, default=True)
                tu.assign_non_tensor(extra, **{key: val})
            except Exception:
                pass
            
        self._extra_data = extra


    def extract_ig_rewards(
        self,
        log_probs: torch.Tensor,   # (B, response_length) from original pass
        data,
        infer_fn,                  # callable: TensorDict → output dict
    ) -> torch.Tensor:
        """
        1. Run a second forward pass on self._extra_data to get extra log-probs.
        2. Compute geometric-mean probs per (sample, turn).
        3. Compute IG rewards (Eq. 4) + outcome rewards.
        4. Scatter to last token of each turn.
        5. Write data["ig_token_rewards"].
        """
        n_original = log_probs.shape[0]
        device     = log_probs.device

        # Nothing to do
        if not self._meta or self._extra_data is None:
            data["ig_token_rewards"] = torch.zeros(
                n_original, self.response_length,
                dtype=torch.float32, device=device,
            )
            return data["ig_token_rewards"]

        # ---- second forward pass on E scoring rows -------------------
        with torch.no_grad():
            extra_output   = infer_fn(self._extra_data)
            extra_log_probs = extra_output.get(
                "log_probs", extra_output.get("old_log_probs")
            )                                    # (E, response_length)

        turn_segs_ll    = data["turn_segs"]
        reward_extra_ll = data["reward_extra_info"]

        # Collect per-(sample, turn) geometric-mean probs
        probs: dict[int, dict[int, float]] = defaultdict(dict)

        for row_idx, meta in enumerate(self._meta):
            i     = meta["sample_idx"]
            t_idx = meta["turn_idx"]

            n_ans  = meta["n_ans"]
            n_ans_clamped = min(n_ans, self.response_length)
        
            # Gold answer occupies the last n_ans_clamped positions of the response window
            ans_lp = extra_log_probs[row_idx, -n_ans_clamped:]
            probs[i][t_idx] = _geomean_prob(ans_lp)

        # Build token reward tensor
        ig_token_rewards = torch.zeros(
            n_original, self.response_length,
            dtype=torch.float32, device=device,
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
                if not (0 <= last_tok < self.response_length):
                    prev_prob = prob
                    continue

                if rank < T - 1:
                    ig_token_rewards[i, last_tok] = prob - prev_prob
                else:
                    ig_token_rewards[i, last_tok] = outcome_r

                prev_prob = prob

        data["ig_token_rewards"] = ig_token_rewards
        return ig_token_rewards
 
    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
 
    @staticmethod
    def _trim_batch(data, n_original: int) -> None:
        """Remove extra scoring rows from all tensors in data."""
        try:
            keys = list(data.keys())
        except AttributeError:
            return
        for key in keys:
            try:
                val = data[key]
                if (
                    isinstance(val, torch.Tensor)
                    and val.dim() >= 1
                    and val.shape[0] > n_original
                ):
                    data[key] = val[:n_original]
            except Exception:
                pass


class TrainingWorker(Worker, DistProfilerExtension):
    """
    TrainingWorker provides a Tinker-like API (https://thinkingmachines.ai/tinker/) as a RayWorkerGroup
    to a single controller. Currently, we only provide more coarse grained APIs,
    and do not provide exact APIs as Tinker does. But this can be added in the future.
    """

    def __init__(self, config: TrainingWorkerConfig):
        Worker.__init__(self)

        from verl.workers.engine import BaseEngine, EngineRegistry

        initialize_global_process_group_ray(timeout_second=None)

        self.config = config
        self.model_config = self.config.model_config
        self.engine_config = self.config.engine_config
        self.optimizer_config = self.config.optimizer_config
        self.checkpoint_config = self.config.checkpoint_config
        self.device_name = get_device_name()

        if self.engine_config is None:
            assert self.optimizer_config is None
            if self.config.auto_select_engine_optim_fn is None:
                raise ValueError(
                    "engine_config is not provided and auto_select_engine_optim_fn is not set. "
                    "Cannot determine engine backend."
                )
            # Support automatically select engine backend given model config
            self.engine_config, self.optimizer_config = self.config.auto_select_engine_optim_fn(
                self.model_config, self.device_name
            )

        # we use the one defined in model
        # TODO: this is not elegant and should refactor later
        self.engine_config.use_remove_padding = self.model_config.use_remove_padding
        self.engine_config.use_fused_kernels = self.model_config.use_fused_kernels

        # TODO: add DistProfilerExtension
        self.profiler_config = self.config.profiler_config
        if self.profiler_config is not None:
            self.profiler_tool_config = self.profiler_config.tool_config.get(self.profiler_config.tool, {})
        else:
            self.profiler_tool_config = None

        DistProfilerExtension.__init__(
            self, DistProfiler(rank=self.rank, config=self.profiler_config, tool_config=self.profiler_tool_config)
        )

        self.engine: BaseEngine = EngineRegistry.new(
            model_type=self.config.model_type,
            backend=self.engine_config.strategy,
            model_config=self.model_config,
            engine_config=self.engine_config,
            optimizer_config=self.optimizer_config,
            checkpoint_config=self.checkpoint_config,
        )

        # build dispatch info
        self._register_dispatch_collect_info(
            mesh_name="train",
            dp_rank=self.engine.get_data_parallel_rank(),
            is_collect=self.engine.is_mp_src_rank_with_outputs(),
        )

        self.flops_counter = FlopsCounter(self.model_config.hf_config)

        self.loss_fn = None

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def to(self, device, model=True, optimizer=True, grad=True):
        """Manual control of load/offload"""
        assert device in ["cpu", "device"]

        if device == "device":
            device = get_device_name()

        self.engine.to(device=device, model=model, optimizer=optimizer, grad=grad)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def set_loss_fn(self, loss_fn):
        self.loss_fn = loss_fn

    def _maybe_precompute_state_predictive_index(self, data: TensorDict) -> None:
        """Precompute state-predictive segments at local mini-batch scope when safe."""
        loss_config = getattr(self.loss_fn, "keywords", {}).get("config", None)
        if loss_config is None:
            return
        policy_loss = getattr(loss_config, "policy_loss", None)
        if policy_loss is None:
            return
        loss_mode = policy_loss.get("loss_mode", "vanilla") if hasattr(policy_loss, "get") else policy_loss.loss_mode
        state_predictive_modes = {
            "state_predictive_grpo",
            "state_predictive_grpo_normalized",
            "state_agreement_grpo",
            "state_agreement_grpo_normalized",
            "state_xdomain_grpo",
            "state_xdomain_grpo_normalized",
            "state_evidence_joint_grpo",
        }
        if loss_mode not in state_predictive_modes:
            return
        precompute = (
            policy_loss.get("state_predictive_precompute_state_index", False)
            if hasattr(policy_loss, "get")
            else getattr(policy_loss, "state_predictive_precompute_state_index", False)
        )
        if not precompute or "state_index" in data.keys() or "update_sketch" not in data.keys():
            return

        if self.device_name == "cpu":
            target_device = torch.device("cpu")
        else:
            target_device = torch.device(self.device_name, get_device_id())

        with torch.no_grad():
            state_index, _ = build_state_predictive_index_from_update_sketch(
                advantages=data["advantages"].to(device=target_device, non_blocking=True),
                response_mask=data["response_mask"].to(device=target_device, non_blocking=True),
                update_sketch=data["update_sketch"].to(device=target_device, non_blocking=True),
                config=loss_config,
            )
        if state_index is not None:
            data["state_index"] = state_index.to(device=data["response_mask"].device, non_blocking=True)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def reset(self):
        """
        Reset the model engine to the initial state. If the engine is not initialized,
        we initialize it. Otherwise, reload ckpt and reset states
        """
        self.engine.initialize()

    def _postprocess_output(self, output, *, global_token_num, delta_time, forward_only, images_seqlens):
        """

        Args:
            output: a dictionary containing loss, model_outputs and metrics

        Returns:

        """
        # TODO: whether to log memory
        # metrics["perf/max_memory_allocated_gb"] = get_torch_device().max_memory_allocated() / (1024 ** 3)
        # metrics["perf/max_memory_reserved_gb"] = get_torch_device().max_memory_reserved() / (1024 ** 3)
        # metrics["perf/cpu_memory_used_gb"] = psutil.virtual_memory().used / (1024 ** 3)

        metrics: dict = output.pop("metrics")
        # perform all gather in dp group to ensure that it's correct.
        # Here each metric in metrics can be a list (micro-batch metrics) or a singleton
        # we should always sum the loss of each micro-batch as we scale by global_bsz/global_token
        loss = torch.sum(torch.tensor(output.pop("loss"), device=self.device_name))
        dp_group = self.engine.get_data_parallel_group()
        if dp_group is not None:
            torch.distributed.all_reduce(loss, op=torch.distributed.ReduceOp.AVG, group=dp_group)
        loss = loss.item()

        # For grad_norm, we do not perform all reduce because it is already been done when clipping grad
        grad_norm = metrics.pop("grad_norm", None)
        lr = metrics.pop("lr", None)

        # For other metrics, we perform all gather in dp group (only if DP > 1)
        if dp_group is not None:
            final_metrics = allgather_dict_into_dict(data=metrics, group=dp_group)
        else:
            final_metrics = metrics
        final_metrics["loss"] = loss
        if grad_norm is not None:
            final_metrics["grad_norm"] = grad_norm
        if lr is not None:
            final_metrics["lr"] = lr

        # TODO: confirm the mtp loss IS same across dp
        for k, v in final_metrics.items():
            if k.startswith("mtp_losses"):
                flatten_v = [sublist[0] for sublist in v]  # sublist should be single element
                final_metrics[k] = sum(flatten_v) / len(flatten_v)
        # compute mfu
        if global_token_num is not None:
            estimated_flops, promised_flops = self.flops_counter.estimate_flops(
                global_token_num, delta_time, images_seqlens=images_seqlens
            )
            final_metrics["mfu"] = estimated_flops / promised_flops / torch.distributed.get_world_size()
            if forward_only:
                final_metrics["mfu"] /= 3.0
        # model outputs
        model_output = output.pop("model_output", {})
        # We only return final_metrics
        final_output = tu.get_tensordict(tensor_dict=model_output, non_tensor_dict={"metrics": final_metrics})
        return final_output

    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="train"), blocking=False)
    def train_mini_batch(self, data: TensorDict) -> TensorDict:
        """Split a batch into N mini-batches run for multiple epochs

        Args:
            data:

        Returns:

        """
        maybe_fix_3d_position_ids(data)
        batch_size_per_dp = data.shape[0]
        disable_auto_offload = tu.pop(data, key="disable_auto_offload", default=False)
        mini_batch_size = tu.pop(data, key="mini_batch_size", default=None)
        num_mini_batch = tu.pop(data, key="num_mini_batch", default=None)
        epochs = tu.pop(data, key="epochs", default=1)
        seed = tu.pop(data, key="seed", default=42)
        dataloader_kwargs = tu.pop(data, key="dataloader_kwargs", default={})

        assert mini_batch_size is not None or num_mini_batch is not None

        if mini_batch_size is None:
            assert batch_size_per_dp % num_mini_batch == 0, f"Got {batch_size_per_dp=} and {num_mini_batch=}"
            mini_batch_size_per_gpu = batch_size_per_dp // num_mini_batch
        else:
            assert mini_batch_size % self.engine.get_data_parallel_size() == 0, (
                f"Got {mini_batch_size=} and {self.engine.get_data_parallel_size()=}"
            )
            mini_batch_size_per_gpu = mini_batch_size // self.engine.get_data_parallel_size()

        self._maybe_precompute_state_predictive_index(data)

        # make iterator
        dataloader = tu.make_iterator(
            data,
            mini_batch_size=mini_batch_size_per_gpu,
            epochs=epochs,
            seed=seed + self.engine.get_data_parallel_rank(),
            dataloader_kwargs=dataloader_kwargs,
        )

        with (
            self.engine.train_mode(disable_auto_offload=disable_auto_offload),
            Timer(name="train_batch", logger=None),
        ):
            # update
            output_lst = []
            total_num_iterations = data.shape[0] // mini_batch_size_per_gpu * epochs

            for batch_idx, mini_batch_td in enumerate(dataloader):
                # add global token num
                global_token_num = mini_batch_td["input_ids"].offsets().diff().tolist()  # (total_nnz,)
                # allgather from dp rank
                global_token_num_output = [None] * self.engine.get_data_parallel_size()
                torch.distributed.all_gather_object(
                    global_token_num_output, global_token_num, self.engine.get_data_parallel_group()
                )
                global_token_num = [x for xs in global_token_num_output for x in xs]
                tu.assign_non_tensor(
                    mini_batch_td,
                    global_token_num=NonTensorData(global_token_num),
                    update_lr_scheduler=batch_idx == total_num_iterations - 1,
                    disable_auto_offload=True,
                )
                actor_output = self.train_batch(mini_batch_td)
                output_lst.append(actor_output)

            if self.engine.is_mp_src_rank_with_outputs():
                actor_output = [tu.get(output, "metrics") for output in output_lst]
                metrics = {}
                for output in actor_output:
                    for key, val in output.items():
                        # flattn dp and micro batch
                        if isinstance(val, list):
                            output[key] = (
                                Metric.aggregate_dp(val)
                                if isinstance(val[0], Metric)
                                else list(chain.from_iterable(val))
                            )
                    append_to_dict(metrics, output)

                output = tu.get_tensordict(tensor_dict={}, non_tensor_dict={"metrics": metrics}).cpu()
            else:
                output = None
        return output

    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="train"), blocking=False)
    def train_mini_batch_seeupo(self, data: TensorDict) -> TensorDict:
        """
        SeeUPO: T optimizer steps per mini-batch in reverse turn order.
        Uses the same ppo_loss → seeupo_turn dispatch path as vanilla training.
        seeupo_seg_mask and seeupo_M are injected into data before each engine.train_batch call
        and read by ppo_loss → config.global_batch_info → compute_policy_loss_seeupo_turn.
        """
        maybe_fix_3d_position_ids(data)
        batch_size_per_dp    = data.shape[0]
        disable_auto_offload = tu.pop(data, key="disable_auto_offload", default=False)
        mini_batch_size      = tu.pop(data, key="mini_batch_size", default=None)
        num_mini_batch       = tu.pop(data, key="num_mini_batch", default=None)
        epochs               = tu.pop(data, key="epochs", default=1)
        seed                 = tu.pop(data, key="seed", default=42)
        dataloader_kwargs    = tu.pop(data, key="dataloader_kwargs", default={})

        assert mini_batch_size is not None or num_mini_batch is not None
        if mini_batch_size is None:
            mini_batch_size_per_gpu = batch_size_per_dp // num_mini_batch
        else:
            mini_batch_size_per_gpu = mini_batch_size // self.engine.get_data_parallel_size()

        dataloader = tu.make_iterator(
            data,
            mini_batch_size=mini_batch_size_per_gpu,
            epochs=epochs,
            seed=seed + self.engine.get_data_parallel_rank(),
            dataloader_kwargs=dataloader_kwargs,
        )

        with (
            self.engine.train_mode(disable_auto_offload=disable_auto_offload),
            Timer(name="train_mini_batch_seeupo", logger=None),
        ):
            output_lst = []
            total_num_iterations = data.shape[0] // mini_batch_size_per_gpu * epochs

            for batch_idx, mini_batch_td in enumerate(dataloader):
                # ── shared per-mini-batch setup (mirrors train_mini_batch) ────
                global_token_num = mini_batch_td["input_ids"].offsets().diff().tolist()
                global_token_num_output = [None] * self.engine.get_data_parallel_size()
                torch.distributed.all_gather_object(
                    global_token_num_output, global_token_num,
                    self.engine.get_data_parallel_group()
                )
                global_token_num = [x for xs in global_token_num_output for x in xs]
                tu.assign_non_tensor(
                    mini_batch_td,
                    global_token_num=NonTensorData(global_token_num),
                    update_lr_scheduler=batch_idx == total_num_iterations - 1,
                    disable_auto_offload=True,
                )

                # ── check for turn structure ──────────────────────────────────
  
                turn_index = mini_batch_td.get("turn_index", None)
                if turn_index is None or turn_index[turn_index >= 0].numel() == 0:
                    # No multi-turn — vanilla train_batch
                    actor_output = self.train_batch(mini_batch_td)
                    output_lst.append(actor_output)
                    continue

                num_turns     = int(turn_index[turn_index >= 0].max().item()) + 1
                response_mask = mini_batch_td["response_mask"]               # (B, L)

                # ── M_{T+1} = scalar advantage per trajectory ─────────────────
                resp_counts = response_mask.float().sum(-1).clamp(min=1)     # (B,)
                traj_adv    = (
                    mini_batch_td["advantages"] * response_mask.float()
                ).sum(-1) / resp_counts                                      # (B,)
                M = traj_adv.clone().cpu()

                # Cache multi_modal_inputs — engine may pop it after each forward
                cached_mm = mini_batch_td.get("multi_modal_inputs", None)

                all_turns = list(range(num_turns - 1, -1, -1))
                num_updates = min(num_turns, MAX_UPDATES)
                chunk_size = (num_turns + num_updates - 1) // num_updates  # ceiling division

                buckets = []
                for i in range(num_updates):
                    start = num_turns - 1 - i * chunk_size          # highest turn in this chunk
                    end   = max(num_turns - 1 - (i + 1) * chunk_size + 1, 0)  # lowest turn in chunk, clamp to 0
                    chunk = list(range(start, end - 1, -1))         # descending within chunk
                    if chunk:
                        buckets.append(chunk)
                # buckets = [all_turns[i::num_updates] for i in range(num_updates)]
                # Each bucket is a list of turn indices belonging to that gradient step

                turn_metrics_lst = []

                for bucket in buckets:
                    # Build a combined seg_mask for all turns in this bucket
                    seg_mask = torch.zeros_like(response_mask, dtype=torch.bool)
                    for t in bucket:
                        seg_mask |= (turn_index == t) & response_mask.bool()

                    if seg_mask.sum() == 0:
                        continue
                    
                    mini_batch_td["seeupo_seg_mask"] = seg_mask.to(torch.bool)
                    mini_batch_td["seeupo_M"]        = M.clone().to(torch.float32)
                    # mini_batch_td["seeupo_M"]  = M.clone().to(torch.float32)          # kept for buggy baseline
                    mini_batch_td["seeupo_IS"] = (M / traj_adv.cpu()).to(torch.float32)  # IS-only weight
                    mini_batch_td["seeupo_A"]  = traj_adv.cpu().to(torch.float32)        # pure advantage

                    if cached_mm is not None and "multi_modal_inputs" not in mini_batch_td.keys():
                        mini_batch_td["multi_modal_inputs"] = cached_mm

                    # ── gradient step for this bucket ────────────────────────
                    turn_output = self.train_batch(mini_batch_td)
                    output_lst.append(turn_output)

                    if self.engine.is_mp_src_rank_with_outputs():
                        turn_metrics_lst.append(tu.get(turn_output, "metrics"))

                    # ── re-forward to compute IS for this bucket ─────────────
                    if cached_mm is not None and "multi_modal_inputs" not in mini_batch_td.keys():
                        mini_batch_td["multi_modal_inputs"] = cached_mm

                    new_log_probs = self._reforward_log_probs_new(mini_batch_td)

                    old_log_probs = mini_batch_td["old_log_probs"].cpu()
                    log_ratio_bucket = (
                        (new_log_probs - old_log_probs) * seg_mask.float().cpu()
                    ).clamp(-20.0, 20.0)
                    IS_bucket = torch.exp(log_ratio_bucket.sum(dim=-1))   # (B,)

                    # M = IS_bucket * M  (propagate importance weight across the bucket)
                    M = IS_bucket * M

                    for key in ("seeupo_seg_mask", "seeupo_M"):
                        if key in mini_batch_td.keys():
                            del mini_batch_td[key]

            # ── aggregate across mini-batches (same as train_mini_batch) ─────
            if self.engine.is_mp_src_rank_with_outputs():
                actor_output = [tu.get(output, "metrics") for output in output_lst]
                metrics = {}
                for output in actor_output:
                    for key, val in output.items():
                        if isinstance(val, list):
                            output[key] = (
                                Metric.aggregate_dp(val)
                                if isinstance(val[0], Metric)
                                else list(chain.from_iterable(val))
                            )
                    append_to_dict(metrics, output)
                result = tu.get_tensordict(
                    tensor_dict={},
                    non_tensor_dict={"metrics": metrics},
                ).cpu()
            else:
                result = None

        return result


    def _reforward_log_probs_new(self, data: TensorDict) -> torch.Tensor:
        """
        Forward-only pass with current (just-updated) params.
        Returns log_probs (B, response_length) on CPU.
        """
        from verl.workers.utils.padding import no_padding_2_padding
        from megatron.core import parallel_state as mpu
        from verl.utils.device import get_device_id, get_device_name

        captured = []

        def capture_loss_fn(model_output, data, dp_group=None):
            lp = model_output["log_probs"]
            # no_padding_2_padding converts back to (B, L) padded format
            log_prob_padded = no_padding_2_padding(lp, data)
            captured.append(log_prob_padded.detach().cpu())
            return torch.tensor(0.0, device=lp.device), {}

        with torch.no_grad():
            self.engine.forward_backward_batch(
                data,
                loss_function=capture_loss_fn,
                forward_only=True,
            )

        if mpu.is_pipeline_last_stage(ignore_virtual=True):
            new_log_probs = torch.cat(captured, dim=0).to(torch.float32)
        else:
            B = data["responses"].shape[0]
            L = data["responses"].shape[1]
            new_log_probs = torch.zeros(B, L, dtype=torch.float32)

        new_log_probs = new_log_probs.to(get_device_id())
        torch.distributed.broadcast(
            tensor=new_log_probs,
            src=mpu.get_pipeline_model_parallel_last_rank(),
            group=mpu.get_pipeline_model_parallel_group(),
            async_op=False,
        )
        return new_log_probs.cpu()
    
    
    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="train"), blocking=False)
    def train_mini_batch_distill(self, data: TensorDict) -> TensorDict:
        """
        Single-epoch SFT pass for ERL distillation.
        Reuses train_mini_batch machinery; just pins epochs=1 and
        injects is_erl_distill so loss_fn takes the distill branch.
        """
        from tensordict import NonTensorData  # or your tu equivalent

        # train_mini_batch pops these keys at the start — inject them
        tu.assign_non_tensor(data, epochs=NonTensorData(1))
        tu.assign_non_tensor(data, num_mini_batch=NonTensorData(1))  # ← use this instead of mini_batch_size
        tu.assign_non_tensor(data, seed=NonTensorData(42))
        tu.assign_non_tensor(data, dataloader_kwargs=NonTensorData({}))
        tu.assign_non_tensor(data, disable_auto_offload=NonTensorData(False))

        # is_erl_distill is already a tensor in the batch (set in build_distill_batch),
        # so loss_fn will detect it automatically — nothing else to inject.

        return self.train_mini_batch(data=data)

    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="train"), blocking=False)
    def train_batch(self, data: TensorDict) -> TensorDict:
        assert self.loss_fn is not None, "loss function can't be None when calling train_batch"
        assert not self.engine_config.forward_only, "Can't run `train_batch` when forward_only is in the engine config."
        # global_token_num should be a list of number of tokens of each seq in this batch
        global_token_num = tu.get(data, key="global_token_num")
        disable_auto_offload = tu.get(data, key="disable_auto_offload", default=False)
        images_seqlens = tu.get(data, key="images_seqlens", default=None)

        # inject engineering parameters if not specified
        default_keys = dict(
            use_remove_padding=self.model_config.use_remove_padding,
            use_dynamic_bsz=self.engine_config.use_dynamic_bsz,
            max_token_len_per_gpu=self.engine_config.max_token_len_per_gpu,
            micro_batch_size_per_gpu=self.engine_config.micro_batch_size_per_gpu,
            use_fused_kernels=self.engine_config.use_fused_kernels,
        )

        for key, val in default_keys.items():
            if key not in data.keys():
                tu.assign_non_tensor(data, **{key: val})

        with (
            self.engine.train_mode(disable_auto_offload=disable_auto_offload),
            Timer(name="train_batch", logger=None) as timer,
        ):
            output = self.engine.train_batch(data, loss_function=self.loss_fn)
            # containing loss, model_output and metrics
            # for training, we only care about loss and metrics
        delta_time = timer.last

        update_lr_scheduler = tu.get(data, key="update_lr_scheduler", default=False)
        # update lr scheduler
        if update_lr_scheduler:
            lr = self.engine.lr_scheduler_step()
        else:
            lr = None

        if self.engine.is_mp_src_rank_with_outputs():
            # we don't need model_output in training. Maybe we change out mind later
            output.pop("model_output")
            if lr is not None:
                output["metrics"]["lr"] = lr
            final_output = self._postprocess_output(
                output,
                global_token_num=global_token_num,
                delta_time=delta_time,
                forward_only=False,
                images_seqlens=images_seqlens,
            ).cpu()
        else:
            final_output = None

        return final_output

    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="train"), blocking=False)
    def infer_batch(self, data: TensorDict) -> TensorDict:
        # add mfu calculator
        global_token_num = tu.get(data, key="global_token_num")
        compute_loss = tu.get(data, key="compute_loss", default=True)
        disable_auto_offload = tu.get(data, key="disable_auto_offload", default=False)
        no_lora_adapter = tu.pop(data, key="no_lora_adapter", default=False)
        images_seqlens = tu.get(data, key="images_seqlens", default=None)

        default_keys = dict(
            use_remove_padding=self.model_config.use_remove_padding,
            use_dynamic_bsz=self.engine_config.use_dynamic_bsz,
            max_token_len_per_gpu=self.engine_config.infer_max_token_len_per_gpu,
            micro_batch_size_per_gpu=self.engine_config.infer_micro_batch_size_per_gpu,
            use_fused_kernels=self.engine_config.use_fused_kernels,
            calculate_sum_pi_squared=self.engine_config.get("calculate_sum_pi_squared", False),
            calculate_update_sketch=self.engine_config.get("calculate_update_sketch", False),
            update_sketch_dim=self.engine_config.get("update_sketch_dim", 64),
            update_sketch_seed=self.engine_config.get("update_sketch_seed", 17),
        )

        for key, val in default_keys.items():
            if key not in data.keys():
                tu.assign_non_tensor(data, **{key: val})

        # for sft training, we need to compute loss in eval
        loss_function = self.loss_fn if compute_loss else None

        with (
            self.engine.eval_mode(disable_auto_offload=disable_auto_offload),
            Timer(name="eval_batch", logger=None) as timer,
        ):
            adapter_ctx = self.engine.disable_adapter() if no_lora_adapter else nullcontext()
            with adapter_ctx:
                output = self.engine.infer_batch(data, loss_function=loss_function)
        delta_time = timer.last

        if self.engine.is_mp_src_rank_with_outputs():
            final_output = self._postprocess_output(
                output,
                global_token_num=global_token_num,
                delta_time=delta_time,
                forward_only=True,
                images_seqlens=images_seqlens,
            ).cpu()
        else:
            final_output = None

        return final_output

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def save_checkpoint(self, local_path, hdfs_path=None, global_step=0, max_ckpt_to_keep=None):
        return self.engine.save_checkpoint(local_path, hdfs_path, global_step, max_ckpt_to_keep)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_checkpoint(self, local_path, hdfs_path=None, del_local_after_load=False):
        return self.engine.load_checkpoint(local_path, hdfs_path, del_local_after_load)


class ActorRolloutRefWorker(Worker, DistProfilerExtension):
    """Hybrid worker that includes actor model, rollout and optional ref model.
    For standalone actor or rollout, use ActorWorker or BaseRollout respectively.

    NOTE: ActorRolloutRefWorker no longer support spmd mode and run native server mode.
    """

    def __init__(self, config: DictConfig, role: str, **kwargs):
        Worker.__init__(self)
        self.config = config
        self.role = role
        self.actor: TrainingWorker = None
        self.ref: TrainingWorker = None
        self.rollout: BaseRollout = None
        assert self.role in ["actor", "rollout", "ref", "actor_rollout", "actor_rollout_ref"]
        self._is_actor = self.role in ["actor", "actor_rollout", "actor_rollout_ref"]
        self._is_rollout = self.role in ["rollout", "actor_rollout", "actor_rollout_ref"]
        self._is_ref = self.role in ["ref", "actor_rollout_ref"]

        if self._is_actor:
            omega_profiler_config = config.actor.get("profiler", {})
        elif self._is_rollout:
            # NOTE: In colocation mode, rollout config may not take effect (follow the actor config)
            # This is for extendability in AsyncRL cases
            omega_profiler_config = config.rollout.get("profiler", {})
        else:
            omega_profiler_config = config.ref.get("profiler", {})

        profiler_config = omega_conf_to_dataclass(omega_profiler_config, dataclass_type=ProfilerConfig)
        if omega_profiler_config.get("tool", None) in ["npu", "nsys", "torch", "torch_memory"]:
            tool_config = omega_conf_to_dataclass(
                omega_profiler_config.get("tool_config", {}).get(omega_profiler_config.get("tool"))
            )
        else:
            tool_config = None

        self.enable_routing_replay = (
            self.config.actor.strategy == "megatron" and self.config.actor.megatron.router_replay.mode != "disabled"
        )

        DistProfilerExtension.__init__(
            self, DistProfiler(rank=self.rank, config=profiler_config, tool_config=tool_config)
        )

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def set_loss_fn(self, loss_fn):
        self.actor.set_loss_fn(loss_fn=loss_fn)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def to(self, device, model=True, optimizer=True, grad=True):
        """Manual control of load/offload"""
        self.actor.to(device=device, model=model, optimizer=optimizer, grad=grad)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def init_model(self):
        model_config: HFModelConfig = omega_conf_to_dataclass(self.config.model)

        # 1. build reference model
        if "ref" in self.role:
            # TODO: align ref config with actor config
            with open_dict(self.config.ref):
                self.config.ref.ppo_mini_batch_size = self.config.actor.ppo_mini_batch_size
                self.config.ref.ppo_micro_batch_size = self.config.ref.pop("log_prob_micro_batch_size", None)
                self.config.ref.ppo_micro_batch_size_per_gpu = self.config.ref.pop(
                    "log_prob_micro_batch_size_per_gpu", None
                )
                self.config.ref.use_dynamic_bsz = self.config.ref.pop("log_prob_use_dynamic_bsz", False)
                self.config.ref.ppo_max_token_len_per_gpu = self.config.ref.pop("log_prob_max_token_len_per_gpu", None)
            ref_config: ActorConfig = omega_conf_to_dataclass(self.config.ref)

            # The ref model does not need to enable MTP; force it to false.
            ref_config.model_config = deepcopy(model_config)
            ref_config.model_config.mtp = MtpConfig(enable=False)

            # construct TrainingWorkerConfig
            ref_training_config = TrainingWorkerConfig(
                model_type="language_model",
                model_config=ref_config.model_config,
                engine_config=ref_config.engine,
                optimizer_config=ref_config.optim,
                checkpoint_config=ref_config.checkpoint,
            )

            # assign engine configs
            ref_training_config.engine_config.use_dynamic_bsz = self.config.ref.use_dynamic_bsz
            ref_training_config.engine_config.infer_max_token_len_per_gpu = self.config.ref.ppo_max_token_len_per_gpu
            ref_training_config.engine_config.infer_micro_batch_size_per_gpu = (
                self.config.ref.ppo_micro_batch_size_per_gpu
            )
            ref_training_config.engine_config.use_remove_padding = model_config.use_remove_padding

            self.ref = TrainingWorker(config=ref_training_config)
            self.ref.reset()
            self.set_dispatch_collect(mesh_name="ref", **self.ref.get_dispatch_collect())

        # 2. build actor model
        if "actor" in self.role:
            actor_config: ActorConfig = omega_conf_to_dataclass(self.config.actor)
            actor_config.model_config = model_config
            actor_training_config = TrainingWorkerConfig(
                model_type="language_model",
                model_config=actor_config.model_config,
                engine_config=actor_config.engine,
                optimizer_config=actor_config.optim,
                checkpoint_config=actor_config.checkpoint,
            )

            assert self.config.actor.use_dynamic_bsz == self.config.rollout.log_prob_use_dynamic_bsz

            # assign engine configs
            actor_training_config.engine_config.use_dynamic_bsz = self.config.actor.use_dynamic_bsz
            actor_training_config.engine_config.infer_max_token_len_per_gpu = (
                self.config.rollout.log_prob_max_token_len_per_gpu
            )
            actor_training_config.engine_config.infer_micro_batch_size_per_gpu = (
                self.config.rollout.log_prob_micro_batch_size_per_gpu
            )
            actor_training_config.engine_config.max_token_len_per_gpu = self.config.actor.ppo_max_token_len_per_gpu
            actor_training_config.engine_config.micro_batch_size_per_gpu = (
                self.config.actor.ppo_micro_batch_size_per_gpu
            )
            actor_training_config.engine_config.use_remove_padding = model_config.use_remove_padding

            if self.config.actor.use_dynamic_bsz:
                assert self.config.rollout.log_prob_max_token_len_per_gpu is not None
                assert self.config.actor.ppo_max_token_len_per_gpu is not None
            else:
                assert self.config.rollout.log_prob_micro_batch_size_per_gpu is not None
                assert self.config.actor.ppo_micro_batch_size_per_gpu is not None

            self.loss_fn = partial(ppo_loss, config=actor_config)
            self.actor = TrainingWorker(config=actor_training_config)
            self.actor.reset()
            self.actor.set_loss_fn(self.loss_fn)
            self.set_dispatch_collect(mesh_name="actor", **self.actor.get_dispatch_collect())

        # 3. build rollout engine
        if "rollout" in self.role:
            rollout_config: RolloutConfig = omega_conf_to_dataclass(self.config.rollout)

            # TODO: move rollout_device_mesh into ServerAdapter
            # 3.1 build rollout device mesh (sglang need only)
            infer_tp = rollout_config.tensor_model_parallel_size * rollout_config.data_parallel_size
            infer_pp = rollout_config.pipeline_model_parallel_size
            infer_world_size = infer_tp * infer_pp
            dp = self.world_size // infer_world_size
            assert self.world_size % infer_world_size == 0, (
                f"rollout world_size: {self.world_size} is not divisible by infer_world_size: {infer_world_size}"
            )
            rollout_device_mesh = init_device_mesh(
                get_device_name(), mesh_shape=(dp, infer_tp, infer_pp), mesh_dim_names=["dp", "infer_tp", "infer_pp"]
            )

            # 3.2 initialize rollout engine
            rollout_cls: type[BaseRollout] = get_rollout_class(rollout_config.name, rollout_config.mode)
            self.rollout = rollout_cls(
                config=rollout_config, model_config=model_config, device_mesh=rollout_device_mesh
            )

            # used for LoRA
            self.base_sync_done: bool = "dummy" not in self.config.rollout.load_format
            self.layered_summon = self.config.rollout.get("layered_summon", False)
            self.peft_merge: bool = model_config.lora.get("merge", False)

        # 4. build checkpoint engine
        if "actor" in self.role:
            checkpoint_engine_config = omega_conf_to_dataclass(self.config.rollout.checkpoint_engine)
            backend = checkpoint_engine_config.backend
            bucket_size = checkpoint_engine_config.update_weights_bucket_megabytes << 20
            engine_kwargs = checkpoint_engine_config.engine_kwargs.get(backend, {})
            self.checkpoint_engine = CheckpointEngineRegistry.new(
                backend, is_master=(torch.distributed.get_rank() == 0), bucket_size=bucket_size, **engine_kwargs
            )

        # Free cached GPU memory so colocated vLLM processes can see it via cudaMemGetInfo
        aggressive_empty_cache(force_sync=True)

    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="ref"))
    @DistProfiler.annotate(color="olive", role="ref_compute_log_prob")
    @_with_routing_replay_flag(enabled=False)
    def compute_ref_log_prob(self, data: TensorDict) -> TensorDict:
        output = self.ref.infer_batch(data=data)
        return output.cpu() if output is not None else None

    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="actor"))
    @DistProfiler.annotate(color="blue", role="actor_compute_log_prob")
    @_with_routing_replay_flag(enabled=True)
    def compute_log_prob(self, data: TensorDict) -> TensorDict:
        output = self.actor.infer_batch(data)
        return output.cpu() if output is not None else None


    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="actor"))
    @DistProfiler.annotate(color="teal", role="actor_compute_igpo_log_probs")
    def compute_igpo_log_probs(self, data: TensorDict) -> TensorDict:
        """
        Builds E scoring rows from data, runs a forward pass,
        returns extra log-probs for the gold-answer tokens.
        All ranks participate; only rank 0 returns output (same contract as compute_log_prob).
        """
        use_igpo = getattr(self.config.actor, "use_igpo", False)
        if not use_igpo:
            return None

        builder = IGPORewardBuilder.maybe_create(
            data,
            self.rollout.model_config.tokenizer,
            self.config.rollout.response_length,
            self.config.rollout.response_length + self.config.rollout.prompt_length ,
            infer_micro_batch_size_per_gpu=self.config.rollout.log_prob_micro_batch_size_per_gpu
        )
        if builder is None or builder._extra_data is None:
            return None
    

        #  # ── determine the required divisor ───────────────────────────────
        # # breakpoint()
        # micro_bsz = self.config.rollout.log_prob_micro_batch_size_per_gpu  # e.g. 4
        # # force_group_size comes from tensor/pipeline parallelism world size
        # # replicate the same logic prepare_micro_batches uses
        # try:
        #     force_group_size = self.actor.engine.get_force_group_size()   # if such method exists
        # except Exception:
        #     force_group_size = 1   # safe fallback; set to TP*PP if you know it statically
        # divisor = micro_bsz * force_group_size
        # ## ── pad to divisor ───────────────────────────────────────────────
        # E_orig = builder._extra_data.batch_size[0]
        # n_pad = (E_orig) % divisor          # elegant: 0 if already divisible
        # if n_pad > 0:
        #     # clone last row n_pad times as harmless dummy rows
        #     last       = builder._extra_data[E_orig - 1 : E_orig]   # shape [1, ...]
        #     pad_chunks = [builder._extra_data] + [last] * n_pad
        #     from tensordict import TensorDict
        #     padded_extra = torch.cat(pad_chunks, dim=0)
        # else:
        #     padded_extra = builder._extra_data

        # ── forward pass ─────────────────────────────────────────────────
        output = self.actor.infer_batch(builder._extra_data)
    
        if output is None:
            return None
    
        log_probs = output.get("log_probs", output.get("old_log_probs"))

        # ── strip padding rows ───────────────────────────────────────────
        # log_probs = log_probs[:E_orig]   # discard the dummy rows
    
        # Also ship the builder metadata so the main process can do the IG math
        import json
        meta_json = json.dumps(builder._meta[:builder._E_orig])  # safety: trim sentinel metas
        result = tu.get_tensordict({"extra_log_probs": log_probs.float()})
        tu.assign_non_tensor(result, igpo_meta=meta_json)
        return result.cpu()

    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="actor"))
    @DistProfiler.annotate(color="red", role="actor_update")
    @_with_routing_replay_flag(enabled=True)
    def update_actor(self, data: TensorDict) -> TensorDict:
        output = self.actor.train_mini_batch(data=data)
        return output.cpu() if output is not None else None
    
    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="actor"))
    @DistProfiler.annotate(color="red", role="actor_update_seeupo")
    @_with_routing_replay_flag(enabled=True)
    def update_actor_seeupo(self, data: TensorDict) -> TensorDict:

        output = self.actor.train_mini_batch_seeupo(data=data)
        return output.cpu() if output is not None else None
    
    @register(dispatch_mode=make_nd_compute_dataproto_dispatch_fn(mesh_name="actor"))
    @DistProfiler.annotate(color="blue", role="actor_distill")
    def update_actor_distill(self, data: TensorDict) -> TensorDict:
        """ERL internalization step: SFT on (x → y2) for successful reflections."""
        output = self.actor.train_mini_batch_distill(data=data)
        return output.cpu() if output is not None else None

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def load_checkpoint(self, local_path, hdfs_path=None, del_local_after_load=False):
        assert "actor" in self.role, "load_checkpoint only support actor role"
        self.actor.load_checkpoint(local_path, hdfs_path, del_local_after_load)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL)
    def save_checkpoint(self, local_path, hdfs_path=None, global_step=0, max_ckpt_to_keep=None):
        assert "actor" in self.role, "save_checkpoint only support actor role"
        self.actor.save_checkpoint(local_path, hdfs_path, global_step, max_ckpt_to_keep)

    @register(dispatch_mode=Dispatch.ONE_TO_ALL, blocking=False)
    async def update_weights(self, global_steps: int = None):
        """Update weights from trainer to rollout.

        1. For sync training with colocated trainer and rollout, update rollout directly from model engine.
           - before update_weights: rollout should be in sleep mode.
           - after update_weights: rollout should be in wake_up mode.
        2. For async training with disaggregated trainer and rollout, send_weights only by checkpoint engine.
        """

        # 0. send_weights only for async training with disaggregated trainer and rollout
        if self.config.rollout.checkpoint_engine.backend != "naive":
            per_tensor_param, _ = self.actor.engine.get_per_tensor_param()
            await self.checkpoint_engine.send_weights(per_tensor_param)
            return

        set_expandable_segments(False)
        log_gpu_memory_usage("Before resume weights", logger=logger)

        # 1. resume weights and update weights
        if self.config.rollout.free_cache_engine:
            await self.rollout.resume(tags=["weights"])
        log_gpu_memory_usage("After resume weights", logger=logger)

        # 2. get per tensor generator from engine, this will load model to gpu
        per_tensor_param, peft_config = self.actor.engine.get_per_tensor_param(
            layered_summon=self.layered_summon, base_sync_done=True
        )

        await self.rollout.update_weights(
            per_tensor_param, peft_config=peft_config, base_sync_done=True, global_steps=global_steps
        )

        do_lora_base_sync = False
        if not self.peft_merge and peft_config is not None:
            # set sleep level for LoRA adapter weights only sync
            # TODO: make this configurable so that users with small
            # main memory can trade sync time to avoid OOM
            self.rollout.sleep_level = 1

            do_lora_base_sync = (not self.base_sync_done) or (
                self.rollout.sleep_level != 1 and self.config.rollout.free_cache_engine
            )

        if do_lora_base_sync:
            per_tensor_base_params, _ = self.actor.engine.get_per_tensor_param(
                layered_summon=self.layered_summon, base_sync_done=False
            )
            await self.rollout.update_weights(per_tensor_base_params, peft_config=peft_config, base_sync_done=False)

        log_gpu_memory_usage("After update_weights", logger=logger)

        # 3. offload model to cpu
        self.actor.engine.to("cpu", model=True, optimizer=False, grad=False)
        aggressive_empty_cache(force_sync=True)

        # 4. resume kv_cache
        if self.config.rollout.free_cache_engine:
            await self.rollout.resume(tags=["kv_cache"])
        log_gpu_memory_usage("After resume kv_cache", logger=logger)

        self.base_sync_done = True
        set_expandable_segments(True)

    @register(dispatch_mode=Dispatch.DP_COMPUTE, blocking=False)
    def execute_checkpoint_engine(self, method: str, *args, **kwargs):
        """Execute checkpoint engine method.

        Args:
            method (str): Checkpoint engine method name.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        """
        return getattr(self.checkpoint_engine, method)(*args, **kwargs)
