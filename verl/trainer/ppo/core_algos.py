# Copyright 2024 Bytedance Ltd. and/or its affiliates
# Copyright 2022 The HuggingFace Team. All rights reserved.
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
Core functions to implement PPO algorithms.
The function implemented in this file should be used by trainer with different distributed strategies to
implement PPO-like algorithms.
"""

__all__ = ["register_adv_est", "get_adv_estimator_fn", "AdvantageEstimator"]

import math
import time
from collections import defaultdict
from enum import Enum
from typing import Any, Callable, Optional

import numpy as np
import torch
from omegaconf import DictConfig

import verl.utils.torch_functional as verl_F
from verl.trainer.config import AlgoConfig
from verl.utils import as_torch_index, group_mean_std
from verl.utils.import_utils import deprecated
from verl.workers.config import ActorConfig

PolicyLossFn = Callable[
    [
        torch.Tensor,  # old_log_prob
        torch.Tensor,  # log_prob
        torch.Tensor,  # advantages
        torch.Tensor,  # response_mask
        str,  # loss_agg_mode
        Optional[DictConfig | ActorConfig],  # config
        torch.Tensor | None,  # rollout_log_probs
    ],
    tuple[torch.Tensor, dict[str, Any]],
]

POLICY_LOSS_REGISTRY: dict[str, PolicyLossFn] = {}
_LATENT_FACTOR_GRPO_STATE: dict[tuple[str, int, int, int], dict[str, torch.Tensor]] = {}


def register_policy_loss(name: str) -> Callable[[PolicyLossFn], PolicyLossFn]:
    """Register a policy loss function with the given name.

    Args:
        name (str): The name to register the policy loss function under.

    Returns:
        function: Decorator function that registers the policy loss function.
    """

    def decorator(func: PolicyLossFn) -> PolicyLossFn:
        POLICY_LOSS_REGISTRY[name] = func
        return func

    return decorator


def get_policy_loss_fn(name):
    """Get the policy loss with a given name.

    Args:
        name: `(str)`
            The name of the policy loss.

    Returns:
        `(callable)`: The policy loss function.
    """
    loss_name = name
    if loss_name not in POLICY_LOSS_REGISTRY:
        raise ValueError(
            f"Unsupported loss mode: {loss_name}. Supported modes are: {list(POLICY_LOSS_REGISTRY.keys())}"
        )
    return POLICY_LOSS_REGISTRY[loss_name]


class AdvantageEstimator(str, Enum):
    """Using an enumeration class to avoid spelling errors in adv_estimator.

    Note(haibin.lin): this enum class is immutable after creation. Extending this
    enum for new estimators may not be necessary since users can always just call
    `verl.trainer.ppo.core_algos.register` with string name for a custom advantage
    estimator instead.
    """

    GAE = "gae"
    GRPO = "grpo"
    REINFORCE_PLUS_PLUS = "reinforce_plus_plus"
    REINFORCE_PLUS_PLUS_BASELINE = "reinforce_plus_plus_baseline"
    REMAX = "remax"
    RLOO = "rloo"
    OPO = "opo"
    GRPO_PASSK = "grpo_passk"
    GPG = "gpg"
    RLOO_VECTORIZED = "rloo_vectorized"
    GRPO_VECTORIZED = "grpo_vectorized"
    OPTIMAL_TOKEN_BASELINE = "optimal_token_baseline"
    TIR_OPTIMAL_TOKEN_BASELINE = "tir_optimal_token_baseline"
    GDPO = "gdpo",
    MAXRL = "maxrl"
    LPO = "lpo"
    LPO_ADAPTIVE = "lpo_adaptive"
    MSE_GATE = "mse_gate"
    BOS_GRPO = "batch_opt_subspace_grpo"
    MULTI_DOMAIN_BOS_GRPO = "multi_domain_bos_grpo"
    SHARED_PRIVATE_MULTI_DOMAIN_BOS_GRPO = "shared_private_multi_domain_bos_grpo"
    SNR_MULTI_DOMAIN_GRPO = "snr_multi_domain_grpo"
    GIGPO  = "gigpo"    # NEW
    IGPO = "igpo"
    grpo_erl_split = "grpo_erl_split"


ADV_ESTIMATOR_REGISTRY: dict[str, Any] = {}


def _cfg_get(config: Any, key: str, default: Any) -> Any:
    if config is None:
        return default
    if hasattr(config, "get"):
        return config.get(key, default)
    return getattr(config, key, default)


def _cfg_get_bool(config: Any, key: str, default: Any) -> bool:
    value = _cfg_get(config, key, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _resolve_maxrl_reward_floor(
    scores: torch.Tensor,
    config: Optional[AlgoConfig],
    default: float = -1.0,
) -> torch.Tensor:
    """Return the reward floor used to convert rewards into nonnegative utilities."""
    floor = default
    if config is not None:
        floor = config.get("maxrl_reward_floor", config.get("mse_gate_maxrl_reward_floor", floor))

    if isinstance(floor, str):
        floor = floor.strip().lower()
        if floor in {"auto", "batch_min", "min"}:
            return scores.detach().amin().to(device=scores.device, dtype=scores.dtype)
        floor = float(floor)

    return torch.as_tensor(float(floor), device=scores.device, dtype=scores.dtype)


def register_adv_est(name_or_enum: str | AdvantageEstimator) -> Any:
    """Decorator to register a advantage estimator function with a given name.

    Args:
        name_or_enum: `(str)` or `(AdvantageEstimator)`
            The name or enum of the advantage estimator.

    """

    def decorator(fn):
        name = name_or_enum.value if isinstance(name_or_enum, Enum) else name_or_enum
        if name in ADV_ESTIMATOR_REGISTRY and ADV_ESTIMATOR_REGISTRY[name] != fn:
            raise ValueError(
                f"Adv estimator {name} has already been registered: {ADV_ESTIMATOR_REGISTRY[name]} vs {fn}"
            )
        ADV_ESTIMATOR_REGISTRY[name] = fn
        return fn

    return decorator


def get_adv_estimator_fn(name_or_enum):
    """Get the advantage estimator function with a given name.

    Args:
        name_or_enum: `(str)` or `(AdvantageEstimator)`
            The name or enum of the advantage estimator.

    Returns:
        `(callable)`: The advantage estimator function.
    """
    name = name_or_enum.value if isinstance(name_or_enum, Enum) else name_or_enum
    if name not in ADV_ESTIMATOR_REGISTRY:
        raise ValueError(f"Unknown advantage estimator simply: {name}")
    return ADV_ESTIMATOR_REGISTRY[name]


class AdaptiveKLController:
    """
    Adaptive KL controller described in the paper:
    https://arxiv.org/pdf/1909.08593.pdf
    """

    def __init__(self, init_kl_coef, target_kl, horizon):
        self.value = init_kl_coef
        self.target = target_kl
        self.horizon = horizon

    def update(self, current_kl, n_steps):
        """Update the KL coefficient based on current KL divergence.

        Args:
            current_kl (float): Current KL divergence value.
            n_steps (int): Number of steps taken.
        """
        target = self.target
        proportional_error = np.clip(current_kl / target - 1, -0.2, 0.2)
        mult = 1 + proportional_error * n_steps / self.horizon
        self.value *= mult


class FixedKLController:
    """Fixed KL controller."""

    def __init__(self, kl_coef):
        self.value = kl_coef

    def update(self, current_kl, n_steps):
        """Update method for fixed KL controller (no-op).

        Args:
            current_kl (float): Current KL divergence value (unused).
            n_steps (int): Number of steps taken (unused).
        """
        pass


def get_kl_controller(kl_ctrl):
    """Factory function to create appropriate KL controller based on configuration.

    Args:
        kl_ctrl: Configuration object containing KL controller settings.

    Returns:
        KL controller instance (FixedKLController or AdaptiveKLController).

    Raises:
        NotImplementedError: If controller type is not supported.
        AssertionError: If adaptive controller horizon is not positive.
    """
    if kl_ctrl.type == "fixed":
        return FixedKLController(kl_coef=kl_ctrl.kl_coef)
    elif kl_ctrl.type == "adaptive":
        assert kl_ctrl.horizon > 0, f"horizon must be larger than 0. Got {kl_ctrl.horizon}"
        return AdaptiveKLController(init_kl_coef=kl_ctrl.kl_coef, target_kl=kl_ctrl.target_kl, horizon=kl_ctrl.horizon)
    else:
        raise NotImplementedError


@register_adv_est(AdvantageEstimator.GAE)  # or simply: @register_adv_est("gae")
def compute_gae_advantage_return(
    token_level_rewards: torch.Tensor,
    values: torch.Tensor,
    response_mask: torch.Tensor,
    gamma: torch.Tensor,
    lam: torch.Tensor,
):
    """Adapted from https://github.com/huggingface/trl/blob/main/trl/trainer/ppo_trainer.py

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape is (bs, response_length)
        values: `(torch.Tensor)`
            shape is (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape is (bs, response_length). [EOS] mask. The token after [EOS] have mask zero.
        gamma is `(float)`
            discounted factor used in RL
        lam: `(float)`
            lambda value when computing Generalized Advantage Estimation (https://arxiv.org/abs/1506.02438)

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)

    """
    with torch.no_grad():
        nextvalues = 0
        lastgaelam = 0
        advantages_reversed = []
        gen_len = token_level_rewards.shape[-1]

        for t in reversed(range(gen_len)):
            delta = token_level_rewards[:, t] + gamma * nextvalues - values[:, t]
            lastgaelam_ = delta + gamma * lam * lastgaelam

            # skip values and TD-error on observation tokens
            nextvalues = values[:, t] * response_mask[:, t] + (1 - response_mask[:, t]) * nextvalues
            lastgaelam = lastgaelam_ * response_mask[:, t] + (1 - response_mask[:, t]) * lastgaelam

            advantages_reversed.append(lastgaelam)
        advantages = torch.stack(advantages_reversed[::-1], dim=1)

        returns = advantages + values
        advantages = verl_F.masked_whiten(advantages, response_mask)
    return advantages, returns


def compute_turn_level_value_advantage_return(
    returns: torch.Tensor,
    values: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
    anchor: str = "first",
):
    """
    Convert token-level value targets into turn-level value targets.

    For each assistant turn, choose one return/value anchor and broadcast
    A_turn = return_anchor - value_anchor to every token in that turn.
    The actor can then use normal token-level PPO clipping with a turn-constant
    advantage.
    """
    with torch.no_grad():
        anchor = (anchor or "first").lower()
        if anchor not in {"first", "last", "mean"}:
            raise ValueError(f"Invalid turn_level_value_anchor: {anchor}. Expected first, last, or mean.")

        valid_mask = response_mask.bool() & (turn_index >= 0)
        valid_turn_ids = turn_index[valid_mask]
        if valid_turn_ids.numel() == 0:
            advantages = returns - values
            advantages = verl_F.masked_whiten(advantages, response_mask)
            return advantages, returns

        batch_size, response_length = returns.shape
        turn_returns = torch.zeros_like(returns)
        turn_advantages = torch.zeros_like(returns)
        num_turns = int(valid_turn_ids.max().item()) + 1

        for turn_id in range(num_turns):
            turn_mask = valid_mask & (turn_index == turn_id)
            turn_len = turn_mask.float().sum(-1)
            has_turn = turn_len > 0
            if not has_turn.any():
                continue

            if anchor == "mean":
                anchor_return = (returns * turn_mask.float()).sum(-1) / turn_len.clamp(min=1.0)
                anchor_value = (values * turn_mask.float()).sum(-1) / turn_len.clamp(min=1.0)
            else:
                if anchor == "first":
                    anchor_idx = turn_mask.float().argmax(dim=-1)
                else:
                    anchor_idx = response_length - 1 - turn_mask.flip(dims=[-1]).float().argmax(dim=-1)
                anchor_return = returns.gather(1, anchor_idx.unsqueeze(1)).squeeze(1)
                anchor_value = values.gather(1, anchor_idx.unsqueeze(1)).squeeze(1)

            anchor_return = torch.where(has_turn, anchor_return, torch.zeros_like(anchor_return))
            anchor_advantage = torch.where(has_turn, anchor_return - anchor_value, torch.zeros_like(anchor_return))
            turn_returns = torch.where(turn_mask, anchor_return.unsqueeze(1).expand_as(turn_returns), turn_returns)
            turn_advantages = torch.where(
                turn_mask, anchor_advantage.unsqueeze(1).expand_as(turn_advantages), turn_advantages
            )

        turn_returns = turn_returns * response_mask
        turn_advantages = verl_F.masked_whiten(turn_advantages, response_mask)
    return turn_advantages, turn_returns


# NOTE(sgm): this implementation only consider outcome supervision, where the reward is a scalar.
@register_adv_est(AdvantageEstimator.GRPO)  # or simply: @register_adv_est("grpo")
def compute_grpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for GRPO, operating only on Outcome reward
    (with only one scalar reward for each response).

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape is (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape is (bs, response_length)
        index: `(np.ndarray)`
            index array for grouping
        epsilon: `(float)`
            small value to avoid division by zero
        norm_adv_by_std_in_grpo: `(bool)`
            whether to scale the GRPO advantage
        config: `(Optional[AlgoConfig])`
            algorithm configuration object

    Note:
        If norm_adv_by_std_in_grpo is True, the advantage is scaled by the std, as in the original GRPO.
        If False, the advantage is not scaled, as in Dr.GRPO (https://arxiv.org/abs/2503.20783).

    Returns:
        advantages: `(torch.Tensor)`
            shape is (bs, response_length)
        Returns: `(torch.Tensor)`
            shape is (bs, response_length)
    """
    scores = token_level_rewards.sum(dim=-1)

    id2score = defaultdict(list)
    id2mean = {}
    id2std = {}

    with torch.no_grad():
        bsz = scores.shape[0]
        for i in range(bsz):
            id2score[index[i]].append(scores[i])
        for idx in id2score:
            if len(id2score[idx]) == 1:
                id2mean[idx] = torch.tensor(0.0)
                id2std[idx] = torch.tensor(1.0)
            elif len(id2score[idx]) > 1:
                scores_tensor = torch.stack(id2score[idx])
                id2mean[idx] = torch.mean(scores_tensor)
                id2std[idx] = torch.std(scores_tensor)
            else:
                raise ValueError(f"no score in prompt index: {idx}")
        for i in range(bsz):
            if norm_adv_by_std_in_grpo:
                scores[i] = (scores[i] - id2mean[index[i]]) / (id2std[index[i]] + epsilon)
            else:
                scores[i] = scores[i] - id2mean[index[i]]
        scores = scores.unsqueeze(-1) * response_mask

    return scores, scores


@register_adv_est(AdvantageEstimator.GRPO_VECTORIZED)
def compute_grpo_vectorized_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Vectorized GRPO（outcome-only）:
      For each group g:
      a_i = \\frac{r_i - \\mu_g}{\\sigma_g} (or without dividing by \\sigma_g),
      then broadcast the scalar across the token dimension (multiplied by response_mask).。
    """
    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1)
        g = as_torch_index(index, device=scores.device)
        mean_g, std_g, _ = group_mean_std(scores, g, eps=epsilon, device=scores.device)
        if norm_adv_by_std_in_grpo:
            scalars = (scores - mean_g[g]) / (std_g[g] + epsilon)
        else:
            scalars = scores - mean_g[g]
        advantages = scalars.unsqueeze(-1) * response_mask
        return advantages, advantages


def _latent_factor_get_state(
    *,
    feature_dim: int,
    hidden_dim: int,
    num_factors: int,
    device: torch.device,
    dtype: torch.dtype,
) -> dict[str, torch.Tensor]:
    key = ("latent_factor_grpo", feature_dim, hidden_dim, num_factors)
    state = _LATENT_FACTOR_GRPO_STATE.get(key)
    needs_init = state is None
    if state is not None:
        sample = next(iter(state.values()))
        needs_init = sample.device != device or sample.dtype != dtype
    if needs_init:
        scale = 0.02
        state = {
            "w_feat": (torch.randn(feature_dim, hidden_dim, device=device, dtype=dtype) * scale).requires_grad_(),
            "b_feat": torch.zeros(hidden_dim, device=device, dtype=dtype, requires_grad=True),
            "w_assign": (torch.randn(hidden_dim, num_factors, device=device, dtype=dtype) * scale).requires_grad_(),
            "b_assign": torch.zeros(num_factors, device=device, dtype=dtype, requires_grad=True),
            "w_reward": (torch.randn(hidden_dim, num_factors, device=device, dtype=dtype) * scale).requires_grad_(),
            "b_reward": torch.zeros(num_factors, device=device, dtype=dtype, requires_grad=True),
        }
        _LATENT_FACTOR_GRPO_STATE[key] = state
    return state


def _latent_factor_assignment(
    logits: torch.Tensor,
    response_mask: torch.Tensor,
    *,
    tau: float,
    balance: bool,
    balance_iters: int,
    epsilon: float,
) -> torch.Tensor:
    mask = response_mask.to(dtype=logits.dtype).unsqueeze(-1)
    alpha = torch.softmax(logits / max(tau, epsilon), dim=-1) * mask
    if not balance:
        return alpha

    valid_tokens = mask.sum().clamp_min(epsilon)
    target_mass = valid_tokens / logits.shape[-1]
    for _ in range(max(balance_iters, 0)):
        factor_mass = alpha.sum(dim=(0, 1), keepdim=True).clamp_min(epsilon)
        alpha = alpha * (target_mass / factor_mass)
        token_mass = alpha.sum(dim=-1, keepdim=True).clamp_min(epsilon)
        alpha = torch.where(mask.bool(), alpha / token_mass, torch.zeros_like(alpha))
    return alpha


def _latent_factor_forward(
    features: torch.Tensor,
    response_mask: torch.Tensor,
    state: dict[str, torch.Tensor],
    *,
    tau: float,
    balance: bool,
    balance_iters: int,
    epsilon: float,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    hidden = torch.tanh(features @ state["w_feat"] + state["b_feat"])
    logits = hidden @ state["w_assign"] + state["b_assign"]
    alpha = _latent_factor_assignment(
        logits,
        response_mask,
        tau=tau,
        balance=balance,
        balance_iters=balance_iters,
        epsilon=epsilon,
    )
    factor_mass = alpha.sum(dim=1).clamp_min(epsilon)
    z = torch.einsum("blh,blk->bkh", hidden, alpha) / factor_mass.unsqueeze(-1)
    raw_factor_adv = torch.einsum("bkh,hk->bk", z, state["w_reward"]) + state["b_reward"]
    return raw_factor_adv, alpha, factor_mass, z


def _latent_factor_token_features(
    *,
    response_mask: torch.Tensor,
    responses: Optional[torch.Tensor],
    old_log_probs: Optional[torch.Tensor],
    epsilon: float,
) -> torch.Tensor:
    device = response_mask.device
    dtype = torch.float32
    mask = response_mask.to(device=device, dtype=dtype)
    bsz, response_length = mask.shape

    token_rank = mask.cumsum(dim=-1) - 1.0
    valid_len = mask.sum(dim=-1, keepdim=True).clamp_min(1.0)
    denom = (valid_len - 1.0).clamp_min(1.0)
    pos = (token_rank / denom).clamp(min=0.0, max=1.0) * mask
    pos2 = pos.square()
    sin_pos = torch.sin(2.0 * math.pi * pos) * mask
    cos_pos = torch.cos(2.0 * math.pi * pos) * mask

    if old_log_probs is None:
        logp_feat = torch.zeros_like(mask)
    else:
        old_log_probs = old_log_probs.to(device=device, dtype=dtype)
        logp_feat = (old_log_probs.clamp(min=-30.0, max=0.0) / 30.0) * mask

    if responses is None:
        token_mod_1 = torch.zeros_like(mask)
        token_mod_2 = torch.zeros_like(mask)
    else:
        responses_f = responses.to(device=device, dtype=torch.long)
        token_mod_1 = ((responses_f.remainder(997)).to(dtype=dtype) / 996.0) * mask
        token_mod_2 = ((responses_f.remainder(1009)).to(dtype=dtype) / 1008.0) * mask

    return torch.stack([pos, pos2, sin_pos, cos_pos, logp_feat, token_mod_1, token_mod_2, mask], dim=-1)


def _latent_factor_proxy_features(
    *,
    response_mask: torch.Tensor,
    responses: Optional[torch.Tensor],
    old_log_probs: Optional[torch.Tensor],
    update_sketch: Optional[torch.Tensor],
    epsilon: float,
) -> tuple[torch.Tensor, bool]:
    """Build per-token latent-factor features, preferring S(e_y - pi) sketches."""
    if update_sketch is not None and update_sketch.dim() == 3:
        features = update_sketch.to(device=response_mask.device, dtype=torch.float32)
        mask = response_mask.to(device=features.device, dtype=torch.float32).unsqueeze(-1)
        features = features * mask
        valid = mask.squeeze(-1).bool()
        if valid.any() and features.shape[-1] > 0:
            valid_features = features[valid]
            feat_mean = valid_features.mean(dim=0, keepdim=True)
            feat_std = valid_features.std(dim=0, unbiased=False, keepdim=True).clamp_min(epsilon)
            features = ((features - feat_mean.view(1, 1, -1)) / feat_std.view(1, 1, -1)) * mask
        return features.detach(), True

    return (
        _latent_factor_token_features(
            response_mask=response_mask,
            responses=responses,
            old_log_probs=old_log_probs,
            epsilon=epsilon,
        ).detach(),
        False,
    )


@register_adv_est("latent_factor_grpo")
def compute_latent_factor_grpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: Optional[np.ndarray] = None,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    responses: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    update_sketch: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, float]]:
    """Latent-factor GRPO advantage allocation without a critic.

    This estimator keeps GRPO's prompt-group scalar advantage, learns a small
    latent factorizer on the rollout batch, and redistributes each sequence's
    scalar advantage to tokens through learned factor memberships. It is a
    runnable approximation of the hidden-state formulation: the default token
    features are detached rollout-side features available during advantage
    computation, so no actor-forward internals are required.
    """
    del kwargs
    device = token_level_rewards.device
    reward_dtype = token_level_rewards.dtype

    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1).to(device=device, dtype=torch.float32)
        if index is None:
            group_index = torch.arange(scores.numel(), device=device, dtype=torch.long)
        else:
            group_index = as_torch_index(index, device=device)
        mean_g, std_g, _ = group_mean_std(scores, group_index, eps=epsilon, device=device)
        if norm_adv_by_std_in_grpo:
            scalar_adv = (scores - mean_g[group_index]) / (std_g[group_index] + epsilon)
        else:
            scalar_adv = scores - mean_g[group_index]

    num_factors = int(_cfg_get(config, "latent_factor_k", 8))
    hidden_dim = int(_cfg_get(config, "latent_factor_hidden_dim", 32))
    aux_steps = int(_cfg_get(config, "latent_factor_aux_steps", 8))
    lr = float(_cfg_get(config, "latent_factor_lr", 1e-2))
    tau = float(_cfg_get(config, "latent_factor_tau", 1.0))
    balance = bool(_cfg_get(config, "latent_factor_balance", True))
    balance_iters = int(_cfg_get(config, "latent_factor_balance_iters", 4))
    preserve_scalar_mean = bool(_cfg_get(config, "latent_factor_preserve_scalar_mean", True))
    residual_correction = bool(_cfg_get(config, "latent_factor_residual_correction", True))
    mix_with_vanilla = float(_cfg_get(config, "latent_factor_mix_with_vanilla", 0.0))
    mix_with_vanilla = min(max(mix_with_vanilla, 0.0), 1.0)

    features, used_update_sketch = _latent_factor_proxy_features(
        response_mask=response_mask,
        responses=responses,
        old_log_probs=old_log_probs,
        update_sketch=update_sketch,
        epsilon=epsilon,
    )
    mask_f = response_mask.to(device=device, dtype=torch.float32)
    feature_dim = features.shape[-1]
    state = _latent_factor_get_state(
        feature_dim=feature_dim,
        hidden_dim=hidden_dim,
        num_factors=num_factors,
        device=device,
        dtype=torch.float32,
    )
    params = list(state.values())

    target = scalar_adv.detach()
    if aux_steps > 0:
        optimizer = torch.optim.Adam(params, lr=lr)
        for _ in range(aux_steps):
            optimizer.zero_grad(set_to_none=True)
            raw_factor_adv, alpha, factor_mass, _ = _latent_factor_forward(
                features,
                response_mask,
                state,
                tau=tau,
                balance=balance,
                balance_iters=balance_iters,
                epsilon=epsilon,
            )
            factor_weights = factor_mass / factor_mass.sum(dim=-1, keepdim=True).clamp_min(epsilon)
            pred_scalar = (factor_weights * raw_factor_adv).sum(dim=-1)
            loss = (pred_scalar - target).square().mean()
            loss.backward()
            optimizer.step()

    with torch.no_grad():
        raw_factor_adv, alpha, factor_mass, _ = _latent_factor_forward(
            features,
            response_mask,
            state,
            tau=tau,
            balance=balance,
            balance_iters=balance_iters,
            epsilon=epsilon,
        )
        factor_weights = factor_mass / factor_mass.sum(dim=-1, keepdim=True).clamp_min(epsilon)
        pred_scalar = (factor_weights * raw_factor_adv).sum(dim=-1)
        if residual_correction:
            residual = (scalar_adv - pred_scalar).unsqueeze(-1)
            # Adding the same residual to every factor is the weighted least
            # squares projection that enforces sum_k w_k r_k = scalar_adv.
            factor_adv = raw_factor_adv + residual
        else:
            factor_adv = raw_factor_adv

        latent_token_adv = torch.einsum("blk,bk->bl", alpha, factor_adv) * mask_f
        if preserve_scalar_mean:
            token_mean = latent_token_adv.sum(dim=-1) / mask_f.sum(dim=-1).clamp_min(1.0)
            latent_token_adv = latent_token_adv + (scalar_adv - token_mean).unsqueeze(-1) * mask_f

        vanilla_adv = scalar_adv.unsqueeze(-1) * mask_f
        advantages = (1.0 - mix_with_vanilla) * latent_token_adv + mix_with_vanilla * vanilla_adv
        returns = advantages.clone()

        pred_mse = (pred_scalar - scalar_adv).square().mean()
        token_mean = advantages.sum(dim=-1) / mask_f.sum(dim=-1).clamp_min(1.0)
        token_alpha = alpha[response_mask.bool()]
        if token_alpha.numel() > 0:
            alpha_entropy = -(token_alpha * token_alpha.clamp_min(epsilon).log()).sum(dim=-1).mean()
            effective_factors = torch.exp(alpha_entropy)
        else:
            alpha_entropy = torch.zeros((), device=device)
            effective_factors = torch.zeros((), device=device)
        global_factor_mass = factor_mass.sum(dim=0)
        global_factor_prob = global_factor_mass / global_factor_mass.sum().clamp_min(epsilon)
        global_factor_entropy = -(global_factor_prob * global_factor_prob.clamp_min(epsilon).log()).sum()
        global_effective_factors = torch.exp(global_factor_entropy)
        latent_param_count = (
            state["w_feat"].numel()
            + state["b_feat"].numel()
            + state["w_assign"].numel()
            + state["b_assign"].numel()
            + state["w_reward"].numel()
            + state["b_reward"].numel()
        )
        metrics = {
            "latent_factor_grpo/used_update_sketch": float(used_update_sketch),
            "latent_factor_grpo/feature_dim": float(feature_dim),
            "latent_factor_grpo/pred_mse": pred_mse.detach().item(),
            "latent_factor_grpo/scalar_adv_mean": scalar_adv.mean().detach().item(),
            "latent_factor_grpo/scalar_adv_std": scalar_adv.std(unbiased=False).detach().item(),
            "latent_factor_grpo/token_adv_std": advantages[response_mask.bool()].std(unbiased=False).detach().item()
            if response_mask.bool().any()
            else 0.0,
            "latent_factor_grpo/mean_abs_scalar_preservation_error": (token_mean - scalar_adv)
            .abs()
            .mean()
            .detach()
            .item(),
            "latent_factor_grpo/factor_mass_min": factor_weights.min().detach().item(),
            "latent_factor_grpo/factor_mass_max": factor_weights.max().detach().item(),
            "latent_factor_grpo/alpha_entropy": alpha_entropy.detach().item(),
            "latent_factor_grpo/effective_factors": effective_factors.detach().item(),
            "latent_factor_grpo/global_effective_factors": global_effective_factors.detach().item(),
            "latent_factor_grpo/param_count": float(latent_param_count),
        }

    return advantages.to(dtype=reward_dtype), returns.to(dtype=reward_dtype), metrics


def _bos_grpo_trajectory_features(
    *,
    response_mask: torch.Tensor,
    responses: Optional[torch.Tensor],
    old_log_probs: Optional[torch.Tensor],
    epsilon: float,
    include_std: bool,
    normalize: bool,
) -> torch.Tensor:
    """Build detached per-trajectory proxy features for batch-optimal subspace GRPO.

    The exact method would use per-trajectory parameter gradients. Those are not
    available when advantages are computed on the trainer driver, so this proxy
    summarizes rollout-side token features that are already present in the batch.
    """
    token_features = _latent_factor_token_features(
        response_mask=response_mask,
        responses=responses,
        old_log_probs=old_log_probs,
        epsilon=epsilon,
    ).to(dtype=torch.float32)
    mask = response_mask.to(device=token_features.device, dtype=torch.float32).unsqueeze(-1)
    lengths = mask.sum(dim=1).clamp_min(1.0)
    mean_features = (token_features * mask).sum(dim=1) / lengths

    if include_std:
        centered = (token_features - mean_features.unsqueeze(1)) * mask
        std_features = (centered.square().sum(dim=1) / lengths).sqrt()
        features = torch.cat([mean_features, std_features], dim=-1)
    else:
        features = mean_features

    if normalize and features.shape[0] > 1:
        feat_mean = features.mean(dim=0, keepdim=True)
        feat_std = features.std(dim=0, unbiased=False, keepdim=True).clamp_min(epsilon)
        features = (features - feat_mean) / feat_std

    return features.detach()


def _multi_domain_bos_domain_features(
    *,
    response_mask: torch.Tensor,
    responses: Optional[torch.Tensor],
    old_log_probs: Optional[torch.Tensor],
    epsilon: float,
    include_std: bool,
    normalize: bool,
) -> torch.Tensor:
    """Build detached per-trajectory proxy features for multi-domain BOS-GRPO.

    This keeps the existing rollout-side proxy construction but will be grouped by
    domain instead of prompt identity. The estimator can then find a common subspace
    that preserves useful cross-domain directions while suppressing disagreement.
    """
    return _bos_grpo_trajectory_features(
        response_mask=response_mask,
        responses=responses,
        old_log_probs=old_log_probs,
        epsilon=epsilon,
        include_std=include_std,
        normalize=normalize,
    )


def _multi_domain_bos_proxy_features(
    *,
    response_mask: torch.Tensor,
    responses: Optional[torch.Tensor],
    old_log_probs: Optional[torch.Tensor],
    update_sketch: Optional[torch.Tensor],
    epsilon: float,
    include_std: bool,
    normalize: bool,
) -> tuple[torch.Tensor, bool]:
    """Build multi-domain BOS proxy features, preferring Megatron score sketches.

    ``update_sketch`` is a per-token CountSketch of the categorical score vector
    ``e_y - pi``.  When present, it is a closer policy-gradient proxy than the
    older rollout-side token/logprob features.
    """
    if update_sketch is not None and update_sketch.dim() == 3:
        features_t = update_sketch.to(dtype=torch.float32)
        mask = response_mask.to(device=features_t.device, dtype=torch.float32).unsqueeze(-1)
        lengths = mask.sum(dim=1).clamp_min(1.0)
        mean_features = (features_t * mask).sum(dim=1) / lengths

        if include_std:
            centered = (features_t - mean_features.unsqueeze(1)) * mask
            std_features = (centered.square().sum(dim=1) / lengths).sqrt()
            features = torch.cat([mean_features, std_features], dim=-1)
        else:
            features = mean_features

        if normalize and features.shape[0] > 1:
            feat_mean = features.mean(dim=0, keepdim=True)
            feat_std = features.std(dim=0, unbiased=False, keepdim=True).clamp_min(epsilon)
            features = (features - feat_mean) / feat_std

        return features.detach(), True

    return (
        _multi_domain_bos_domain_features(
            response_mask=response_mask,
            responses=responses,
            old_log_probs=old_log_probs,
            epsilon=epsilon,
            include_std=include_std,
            normalize=normalize,
        ),
        False,
    )


@register_adv_est(AdvantageEstimator.BOS_GRPO)
@register_adv_est("bos_grpo")
def compute_batch_opt_subspace_grpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: Optional[np.ndarray] = None,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    responses: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, float]]:
    """Batch-optimal subspace GRPO with rollout-feature proxy gradients.

    For each prompt group, we form proxy trajectory updates
    ``u_i = A_i phi_i`` from the GRPO scalar advantage ``A_i`` and detached
    trajectory features ``phi_i``.  We then estimate a low-rank subspace by
    maximizing retained batch signal minus trajectory disagreement:

        B = mu mu^T - lambda Sigma.

    The top eigenvectors of ``B`` define a projector ``P``.  Since exact
    parameter-gradient projection is not available in this advantage-estimator
    stage, we convert the proxy retained-energy ratio ``||P u_i||^2 / ||u_i||^2``
    into a sequence-level advantage weight.
    """
    del kwargs
    device = token_level_rewards.device
    reward_dtype = token_level_rewards.dtype
    eps = float(_cfg_get(config, "bos_grpo_eps", epsilon))
    eps = max(eps, 1e-12)

    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1).to(device=device, dtype=torch.float32)
        if index is None:
            group_index = torch.arange(scores.numel(), device=device, dtype=torch.long)
        else:
            group_index = as_torch_index(index, device=device)
        mean_g, std_g, _ = group_mean_std(scores, group_index, eps=eps, device=device)
        if norm_adv_by_std_in_grpo:
            scalar_adv = (scores - mean_g[group_index]) / (std_g[group_index] + eps)
        else:
            scalar_adv = scores - mean_g[group_index]

        include_std = bool(_cfg_get(config, "bos_grpo_include_feature_std", True))
        normalize_features = bool(_cfg_get(config, "bos_grpo_normalize_features", True))
        phi = _bos_grpo_trajectory_features(
            response_mask=response_mask,
            responses=responses,
            old_log_probs=old_log_probs,
            epsilon=eps,
            include_std=include_std,
            normalize=normalize_features,
        ).to(device=device, dtype=torch.float32)

        rank = max(int(_cfg_get(config, "bos_grpo_k", 4)), 0)
        noise_lambda = max(float(_cfg_get(config, "bos_grpo_lambda", 1.0)), 0.0)
        floor = float(_cfg_get(config, "bos_grpo_weight_floor", 0.1))
        floor = min(max(floor, 0.0), 1.0)
        power = max(float(_cfg_get(config, "bos_grpo_weight_power", 1.0)), eps)
        mix_with_vanilla = float(_cfg_get(config, "bos_grpo_mix_with_vanilla", 0.0))
        mix_with_vanilla = min(max(mix_with_vanilla, 0.0), 1.0)
        positive_only = bool(_cfg_get(config, "bos_grpo_positive_eigs_only", True))
        fallback_to_vanilla = bool(_cfg_get(config, "bos_grpo_fallback_to_vanilla", True))

        mask_f = response_mask.to(device=device, dtype=torch.float32)
        seq_weights = torch.ones_like(scalar_adv, dtype=torch.float32)
        retained_ratio = torch.ones_like(scalar_adv, dtype=torch.float32)

        signal_loss_values = []
        retained_mu_values = []
        noise_kept_values = []
        selected_ranks = []
        group_sizes = []

        group_count = int(torch.max(group_index).item()) + 1 if group_index.numel() > 0 else 0
        for group_id in range(group_count):
            group_mask = group_index == group_id
            idx = torch.nonzero(group_mask, as_tuple=False).flatten()
            n = int(idx.numel())
            if n <= 1 or rank == 0:
                group_sizes.append(float(n))
                selected_ranks.append(0.0)
                signal_loss_values.append(0.0)
                retained_mu_values.append(1.0)
                noise_kept_values.append(1.0)
                continue

            phi_g = phi[idx]
            u_g = scalar_adv[idx].unsqueeze(-1) * phi_g
            dim = int(u_g.shape[-1])
            k_eff = min(rank, dim)
            if k_eff >= dim:
                group_sizes.append(float(n))
                selected_ranks.append(float(dim))
                signal_loss_values.append(0.0)
                retained_mu_values.append(1.0)
                noise_kept_values.append(1.0)
                continue

            mu = u_g.mean(dim=0)
            centered = u_g - mu.unsqueeze(0)
            cov = centered.transpose(0, 1).matmul(centered) / max(n - 1, 1)
            B = torch.outer(mu, mu) - noise_lambda * cov
            B = 0.5 * (B + B.transpose(0, 1))

            eigvals, eigvecs = torch.linalg.eigh(B)
            order = torch.argsort(eigvals, descending=True)
            selected = order[:k_eff]
            if positive_only:
                selected = selected[eigvals[selected] > 0]

            if selected.numel() == 0:
                group_sizes.append(float(n))
                selected_ranks.append(0.0)
                if fallback_to_vanilla:
                    signal_loss_values.append(0.0)
                    retained_mu_values.append(1.0)
                    noise_kept_values.append(1.0)
                    continue
                retained = torch.zeros(n, device=device, dtype=torch.float32)
                retained_ratio[idx] = retained
                seq_weights[idx] = floor
                signal_loss_values.append(1.0)
                retained_mu_values.append(0.0)
                noise_kept_values.append(0.0)
                continue

            Q = eigvecs[:, selected]
            proj_u = u_g.matmul(Q).matmul(Q.transpose(0, 1))
            u_norm2 = u_g.square().sum(dim=-1)
            retained = proj_u.square().sum(dim=-1) / u_norm2.clamp_min(eps)
            retained = torch.where(u_norm2 > eps, retained.clamp(0.0, 1.0), torch.ones_like(retained))
            retained_ratio[idx] = retained

            shaped = floor + (1.0 - floor) * retained.pow(power)
            shaped = mix_with_vanilla + (1.0 - mix_with_vanilla) * shaped
            seq_weights[idx] = shaped.clamp(0.0, 1.0)

            mu_norm2 = mu.square().sum()
            proj_mu = mu.matmul(Q).matmul(Q.transpose(0, 1))
            retained_mu = (proj_mu.square().sum() / mu_norm2.clamp_min(eps)).clamp(0.0, 1.0)
            trace_cov = torch.trace(cov).clamp_min(eps)
            noise_kept = (torch.trace(Q.transpose(0, 1).matmul(cov).matmul(Q)) / trace_cov).clamp(min=0.0)

            group_sizes.append(float(n))
            selected_ranks.append(float(selected.numel()))
            retained_mu_values.append(float(retained_mu.detach().item()))
            signal_loss_values.append(float((1.0 - retained_mu).detach().item()))
            noise_kept_values.append(float(noise_kept.detach().item()))

        seq_adv = scalar_adv * seq_weights
        advantages = seq_adv.unsqueeze(-1) * mask_f
        returns = advantages.clone()

        valid = mask_f.bool().any(dim=-1)
        metrics = {
            "bos_grpo/group_count": float(group_count),
            "bos_grpo/group_size_mean": float(sum(group_sizes) / max(len(group_sizes), 1)),
            "bos_grpo/selected_rank_mean": float(sum(selected_ranks) / max(len(selected_ranks), 1)),
            "bos_grpo/signal_loss_ratio_mean": float(sum(signal_loss_values) / max(len(signal_loss_values), 1)),
            "bos_grpo/retained_mu_ratio_mean": float(sum(retained_mu_values) / max(len(retained_mu_values), 1)),
            "bos_grpo/noise_kept_ratio_mean": float(sum(noise_kept_values) / max(len(noise_kept_values), 1)),
            "bos_grpo/seq_weight_mean": seq_weights[valid].mean().detach().item() if valid.any() else 1.0,
            "bos_grpo/seq_weight_min": seq_weights[valid].min().detach().item() if valid.any() else 1.0,
            "bos_grpo/seq_weight_max": seq_weights[valid].max().detach().item() if valid.any() else 1.0,
            "bos_grpo/retained_update_ratio_mean": retained_ratio[valid].mean().detach().item() if valid.any() else 1.0,
            "bos_grpo/scalar_adv_std": scalar_adv.std(unbiased=False).detach().item() if scalar_adv.numel() else 0.0,
            "bos_grpo/token_adv_std": advantages[response_mask.bool()].std(unbiased=False).detach().item()
            if response_mask.bool().any()
            else 0.0,
        }

    return advantages.to(dtype=reward_dtype), returns.to(dtype=reward_dtype), metrics


def _as_numpy_object_array(values: Any, length: int, fill: str = "unknown") -> np.ndarray:
    if values is None:
        return np.array([fill] * length, dtype=object)
    arr = np.asarray(values, dtype=object)
    if arr.ndim == 0:
        return np.array([arr.item()] * length, dtype=object)
    if arr.shape[0] != length:
        return np.array([fill] * length, dtype=object)
    return arr


@register_adv_est(AdvantageEstimator.MULTI_DOMAIN_BOS_GRPO)
@register_adv_est("md_bos_grpo")
def compute_multi_domain_bos_grpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: Optional[np.ndarray] = None,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    responses: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    non_tensor_batch: Optional[dict[str, Any]] = None,
    update_sketch: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, float]]:
    """Multi-domain batch-optimal subspace GRPO using sketch proxy updates.

    The single-domain BOS estimator builds one subspace per prompt group.  This
    variant builds one shared subspace over the current rollout batch by first
    estimating per-domain proxy update means.  Directions are scored by
    cross-domain signal minus domain disagreement and within-domain sampling
    noise:

        B = M - lambda_dom * C_dom - lambda_noise * C_noise.

    When Megatron provides ``update_sketch``, proxy updates are built from a
    CountSketch of ``e_y - pi``.  Otherwise this estimator uses the older
    rollout-side features for compatibility.  Since this stage does not own
    exact parameter gradients, the selected subspace is converted into a
    sequence-level retained-energy weight applied to the usual GRPO scalar
    advantage.
    """
    del kwargs
    device = token_level_rewards.device
    reward_dtype = token_level_rewards.dtype
    eps = max(float(_cfg_get(config, "md_bos_grpo_eps", _cfg_get(config, "bos_grpo_eps", epsilon))), 1e-12)

    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1).to(device=device, dtype=torch.float32)
        batch_size = int(scores.numel())
        if index is None:
            group_index = torch.arange(batch_size, device=device, dtype=torch.long)
        else:
            group_index = as_torch_index(index, device=device)

        mean_g, std_g, _ = group_mean_std(scores, group_index, eps=eps, device=device)
        if norm_adv_by_std_in_grpo:
            scalar_adv = (scores - mean_g[group_index]) / (std_g[group_index] + eps)
        else:
            scalar_adv = scores - mean_g[group_index]

        include_std = bool(_cfg_get(config, "md_bos_grpo_include_feature_std", _cfg_get(config, "bos_grpo_include_feature_std", True)))
        normalize_features = bool(_cfg_get(config, "md_bos_grpo_normalize_features", _cfg_get(config, "bos_grpo_normalize_features", True)))
        phi, used_update_sketch = _multi_domain_bos_proxy_features(
            response_mask=response_mask,
            responses=responses,
            old_log_probs=old_log_probs,
            update_sketch=update_sketch,
            epsilon=eps,
            include_std=include_std,
            normalize=normalize_features,
        )
        phi = phi.to(device=device, dtype=torch.float32)

        u = scalar_adv.unsqueeze(-1) * phi
        dim = int(u.shape[-1])
        rank = max(int(_cfg_get(config, "md_bos_grpo_k", _cfg_get(config, "bos_grpo_k", 4))), 0)
        k_eff = min(rank, dim)
        lambda_dom = max(float(_cfg_get(config, "md_bos_grpo_domain_lambda", 1.0)), 0.0)
        lambda_noise = max(float(_cfg_get(config, "md_bos_grpo_noise_lambda", _cfg_get(config, "bos_grpo_lambda", 1.0))), 0.0)
        floor = min(max(float(_cfg_get(config, "md_bos_grpo_weight_floor", _cfg_get(config, "bos_grpo_weight_floor", 0.1))), 0.0), 1.0)
        power = max(float(_cfg_get(config, "md_bos_grpo_weight_power", _cfg_get(config, "bos_grpo_weight_power", 1.0))), eps)
        mix_with_vanilla = min(max(float(_cfg_get(config, "md_bos_grpo_mix_with_vanilla", _cfg_get(config, "bos_grpo_mix_with_vanilla", 0.0))), 0.0), 1.0)
        positive_only = bool(_cfg_get(config, "md_bos_grpo_positive_eigs_only", _cfg_get(config, "bos_grpo_positive_eigs_only", True)))
        fallback_to_vanilla = bool(_cfg_get(config, "md_bos_grpo_fallback_to_vanilla", _cfg_get(config, "bos_grpo_fallback_to_vanilla", True)))
        min_domain_count = max(int(_cfg_get(config, "md_bos_grpo_min_domain_count", 2)), 1)

        domain_key = str(_cfg_get(config, "md_bos_grpo_domain_key", "domain"))
        domains_np = None
        if non_tensor_batch is not None:
            domains_np = non_tensor_batch.get(domain_key)
            if domains_np is None and domain_key != "ability":
                domains_np = non_tensor_batch.get("ability")
            if domains_np is None and domain_key != "data_source":
                domains_np = non_tensor_batch.get("data_source")
        domains_np = _as_numpy_object_array(domains_np, batch_size)
        domain_names = [str(v) if v is not None else "unknown" for v in domains_np.tolist()]

        domain_to_indices: dict[str, list[int]] = defaultdict(list)
        valid_seq = response_mask.to(device=device, dtype=torch.float32).bool().any(dim=-1)
        for i, name in enumerate(domain_names):
            if bool(valid_seq[i].item()):
                domain_to_indices[name].append(i)

        seq_weights = torch.ones_like(scalar_adv, dtype=torch.float32)
        retained_ratio = torch.ones_like(scalar_adv, dtype=torch.float32)
        selected_rank = 0
        positive_eig_count = 0
        fallback_used = False
        retained_mu_ratio = torch.tensor(1.0, device=device)
        noise_kept_ratio = torch.tensor(1.0, device=device)
        disagreement_kept_ratio = torch.tensor(1.0, device=device)

        usable_domains = [(name, idxs) for name, idxs in domain_to_indices.items() if idxs]
        if k_eff > 0 and dim > 0 and len(usable_domains) >= min_domain_count and batch_size > 1:
            domain_means = []
            domain_vars = []
            omega_values = []
            total_count = float(sum(len(idxs) for _, idxs in usable_domains))
            for _, idxs_list in usable_domains:
                idx_t = torch.as_tensor(idxs_list, device=device, dtype=torch.long)
                u_d = u[idx_t]
                mu_d = u_d.mean(dim=0)
                domain_means.append(mu_d)
                n_d = int(idx_t.numel())
                omega = float(n_d) / max(total_count, 1.0)
                omega_values.append(omega)
                if n_d > 1:
                    centered_d = u_d - mu_d.unsqueeze(0)
                    cov_d = centered_d.transpose(0, 1).matmul(centered_d) / max(n_d - 1, 1)
                else:
                    cov_d = torch.zeros(dim, dim, device=device, dtype=torch.float32)
                domain_vars.append(cov_d)

            omega_t = torch.tensor(omega_values, device=device, dtype=torch.float32)
            mu_stack = torch.stack(domain_means, dim=0)
            mu = (omega_t.unsqueeze(-1) * mu_stack).sum(dim=0)

            M = torch.zeros(dim, dim, device=device, dtype=torch.float32)
            C_dom = torch.zeros_like(M)
            C_noise = torch.zeros_like(M)
            for d_idx, (_, idxs_list) in enumerate(usable_domains):
                omega = omega_t[d_idx]
                mu_d = mu_stack[d_idx]
                diff = mu_d - mu
                M = M + omega * torch.outer(mu_d, mu_d)
                C_dom = C_dom + omega * torch.outer(diff, diff)
                C_noise = C_noise + (omega.square() / max(len(idxs_list), 1)) * domain_vars[d_idx]

            B = M - lambda_dom * C_dom - lambda_noise * C_noise
            B = 0.5 * (B + B.transpose(0, 1))
            eigvals, eigvecs = torch.linalg.eigh(B)
            order = torch.argsort(eigvals, descending=True)
            selected = order[:k_eff]
            if positive_only:
                selected = selected[eigvals[selected] > 0]
            positive_eig_count = int((eigvals > 0).sum().item())

            if selected.numel() == 0:
                fallback_used = bool(fallback_to_vanilla)
                if not fallback_to_vanilla:
                    seq_weights = torch.full_like(seq_weights, floor)
                    retained_ratio = torch.zeros_like(retained_ratio)
                    retained_mu_ratio = torch.tensor(0.0, device=device)
                    noise_kept_ratio = torch.tensor(0.0, device=device)
                    disagreement_kept_ratio = torch.tensor(0.0, device=device)
            else:
                Q = eigvecs[:, selected]
                selected_rank = int(selected.numel())
                proj_u = u.matmul(Q).matmul(Q.transpose(0, 1))
                u_norm2 = u.square().sum(dim=-1)
                retained = proj_u.square().sum(dim=-1) / u_norm2.clamp_min(eps)
                retained = torch.where(u_norm2 > eps, retained.clamp(0.0, 1.0), torch.ones_like(retained))
                retained_ratio = retained

                shaped = floor + (1.0 - floor) * retained.pow(power)
                shaped = mix_with_vanilla + (1.0 - mix_with_vanilla) * shaped
                seq_weights = shaped.clamp(0.0, 1.0)

                mu_norm2 = mu.square().sum().clamp_min(eps)
                proj_mu = mu.matmul(Q).matmul(Q.transpose(0, 1))
                retained_mu_ratio = (proj_mu.square().sum() / mu_norm2).clamp(0.0, 1.0)
                trace_noise = torch.trace(C_noise).clamp_min(eps)
                trace_dom = torch.trace(C_dom).clamp_min(eps)
                noise_kept_ratio = (torch.trace(Q.transpose(0, 1).matmul(C_noise).matmul(Q)) / trace_noise).clamp(min=0.0)
                disagreement_kept_ratio = (torch.trace(Q.transpose(0, 1).matmul(C_dom).matmul(Q)) / trace_dom).clamp(min=0.0)
        else:
            fallback_used = True

        mask_f = response_mask.to(device=device, dtype=torch.float32)
        seq_adv = scalar_adv * seq_weights
        advantages = seq_adv.unsqueeze(-1) * mask_f
        returns = advantages.clone()

        valid = mask_f.bool().any(dim=-1)
        domain_counts = [len(v) for v in domain_to_indices.values()]
        metrics = {
            "md_bos_grpo/domain_count": float(len(usable_domains)),
            "md_bos_grpo/domain_count_min": float(min(domain_counts) if domain_counts else 0),
            "md_bos_grpo/domain_count_max": float(max(domain_counts) if domain_counts else 0),
            "md_bos_grpo/used_update_sketch": float(used_update_sketch),
            "md_bos_grpo/feature_dim": float(dim),
            "md_bos_grpo/selected_rank": float(selected_rank),
            "md_bos_grpo/positive_eig_count": float(positive_eig_count),
            "md_bos_grpo/fallback_used": float(fallback_used),
            "md_bos_grpo/retained_mu_ratio": float(retained_mu_ratio.detach().item()),
            "md_bos_grpo/noise_kept_ratio": float(noise_kept_ratio.detach().item()),
            "md_bos_grpo/disagreement_kept_ratio": float(disagreement_kept_ratio.detach().item()),
            "md_bos_grpo/seq_weight_mean": seq_weights[valid].mean().detach().item() if valid.any() else 1.0,
            "md_bos_grpo/seq_weight_min": seq_weights[valid].min().detach().item() if valid.any() else 1.0,
            "md_bos_grpo/seq_weight_max": seq_weights[valid].max().detach().item() if valid.any() else 1.0,
            "md_bos_grpo/retained_update_ratio_mean": retained_ratio[valid].mean().detach().item() if valid.any() else 1.0,
            "md_bos_grpo/scalar_adv_std": scalar_adv.std(unbiased=False).detach().item() if scalar_adv.numel() else 0.0,
            "md_bos_grpo/token_adv_std": advantages[response_mask.bool()].std(unbiased=False).detach().item()
            if response_mask.bool().any()
            else 0.0,
        }

    return advantages.to(dtype=reward_dtype), returns.to(dtype=reward_dtype), metrics


def _empty_basis(dim: int, *, device: torch.device, dtype: torch.dtype = torch.float32) -> torch.Tensor:
    return torch.empty(dim, 0, device=device, dtype=dtype)


def _select_eigen_basis(
    matrix: torch.Tensor,
    *,
    rank: int,
    positive_only: bool,
    eps: float,
) -> tuple[torch.Tensor, int, int]:
    dim = int(matrix.shape[0])
    if dim == 0 or rank <= 0:
        return _empty_basis(dim, device=matrix.device, dtype=matrix.dtype), 0, 0

    matrix = 0.5 * (matrix + matrix.transpose(0, 1))
    eigvals, eigvecs = torch.linalg.eigh(matrix)
    order = torch.argsort(eigvals, descending=True)
    selected = order[: min(rank, dim)]
    positive_count = int((eigvals > eps).sum().item())
    if positive_only:
        selected = selected[eigvals[selected] > eps]
    if selected.numel() == 0:
        return _empty_basis(dim, device=matrix.device, dtype=matrix.dtype), 0, positive_count
    basis = eigvecs[:, selected]
    return basis, int(basis.shape[1]), positive_count


def _orthonormalize_against(candidates: torch.Tensor, base: torch.Tensor, *, eps: float) -> torch.Tensor:
    dim = int(candidates.shape[0])
    if candidates.numel() == 0 or candidates.shape[1] == 0:
        return _empty_basis(dim, device=candidates.device, dtype=candidates.dtype)

    work = candidates
    if base.numel() > 0 and base.shape[1] > 0:
        work = work - base.matmul(base.transpose(0, 1).matmul(work))

    keep_nonzero = work.norm(dim=0) > eps
    work = work[:, keep_nonzero]
    if work.numel() == 0 or work.shape[1] == 0:
        return _empty_basis(dim, device=candidates.device, dtype=candidates.dtype)

    q, r = torch.linalg.qr(work, mode="reduced")
    keep_independent = torch.abs(torch.diag(r)) > eps
    q = q[:, keep_independent]
    if q.numel() == 0 or q.shape[1] == 0:
        return _empty_basis(dim, device=candidates.device, dtype=candidates.dtype)
    return q


def _project_vector_basis(vector: torch.Tensor, basis: torch.Tensor) -> torch.Tensor:
    if basis.numel() == 0 or basis.shape[1] == 0:
        return torch.zeros_like(vector)
    return vector.matmul(basis).matmul(basis.transpose(0, 1))


def _project_rows_basis(rows: torch.Tensor, basis: torch.Tensor) -> torch.Tensor:
    if basis.numel() == 0 or basis.shape[1] == 0:
        return torch.zeros_like(rows)
    return rows.matmul(basis).matmul(basis.transpose(0, 1))


def _trace_in_basis(matrix: torch.Tensor, basis: torch.Tensor) -> torch.Tensor:
    if basis.numel() == 0 or basis.shape[1] == 0:
        return torch.zeros((), device=matrix.device, dtype=matrix.dtype)
    return torch.trace(basis.transpose(0, 1).matmul(matrix).matmul(basis))


def _metric_safe_domain_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(name))
    safe = safe.strip("_") or "unknown"
    return safe[:64]


@register_adv_est(AdvantageEstimator.SHARED_PRIVATE_MULTI_DOMAIN_BOS_GRPO)
@register_adv_est("shared_private_md_bos_grpo")
@register_adv_est("sp_md_bos_grpo")
def compute_shared_private_multi_domain_bos_grpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: Optional[np.ndarray] = None,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    responses: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    non_tensor_batch: Optional[dict[str, Any]] = None,
    update_sketch: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, float]]:
    """Shared-private multi-domain BOS-GRPO with sketch proxy updates.

    This is a practical advantage-reweighting approximation of the theoretical
    shared-private projector.  It first estimates one shared subspace ``P_s``
    from domain means, then estimates a private residual subspace ``P_{p,d}``
    for each domain in the orthogonal complement of ``P_s``.  Each trajectory is
    weighted by the retained proxy-update energy under its domain projector
    ``P_d = P_s + P_{p,d}``.  When available, the proxy features are trajectory
    means of the Megatron CountSketch of ``e_y - pi``.
    """
    del kwargs
    device = token_level_rewards.device
    reward_dtype = token_level_rewards.dtype
    eps = max(
        float(_cfg_get(config, "sp_md_bos_grpo_eps", _cfg_get(config, "md_bos_grpo_eps", _cfg_get(config, "bos_grpo_eps", epsilon)))),
        1e-12,
    )

    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1).to(device=device, dtype=torch.float32)
        batch_size = int(scores.numel())
        if index is None:
            group_index = torch.arange(batch_size, device=device, dtype=torch.long)
        else:
            group_index = as_torch_index(index, device=device)

        mean_g, std_g, _ = group_mean_std(scores, group_index, eps=eps, device=device)
        if norm_adv_by_std_in_grpo:
            scalar_adv = (scores - mean_g[group_index]) / (std_g[group_index] + eps)
        else:
            scalar_adv = scores - mean_g[group_index]

        include_std = _cfg_get_bool(
            config,
            "sp_md_bos_grpo_include_feature_std",
            _cfg_get(config, "md_bos_grpo_include_feature_std", _cfg_get(config, "bos_grpo_include_feature_std", True)),
        )
        normalize_features = _cfg_get_bool(
            config,
            "sp_md_bos_grpo_normalize_features",
            _cfg_get(config, "md_bos_grpo_normalize_features", _cfg_get(config, "bos_grpo_normalize_features", True)),
        )
        phi, used_update_sketch = _multi_domain_bos_proxy_features(
            response_mask=response_mask,
            responses=responses,
            old_log_probs=old_log_probs,
            update_sketch=update_sketch,
            epsilon=eps,
            include_std=include_std,
            normalize=normalize_features,
        )
        phi = phi.to(device=device, dtype=torch.float32)

        u = scalar_adv.unsqueeze(-1) * phi
        dim = int(u.shape[-1])
        shared_rank = max(
            int(_cfg_get(config, "sp_md_bos_grpo_shared_k", _cfg_get(config, "md_bos_grpo_k", _cfg_get(config, "bos_grpo_k", 4)))),
            0,
        )
        private_rank = max(int(_cfg_get(config, "sp_md_bos_grpo_private_k", 2)), 0)
        k_shared = min(shared_rank, dim)
        k_private = min(private_rank, dim)
        lambda_dom = max(float(_cfg_get(config, "sp_md_bos_grpo_domain_lambda", _cfg_get(config, "md_bos_grpo_domain_lambda", 1.0))), 0.0)
        lambda_shared_noise = max(
            float(_cfg_get(config, "sp_md_bos_grpo_shared_noise_lambda", _cfg_get(config, "md_bos_grpo_noise_lambda", _cfg_get(config, "bos_grpo_lambda", 1.0)))),
            0.0,
        )
        lambda_private_noise = max(
            float(_cfg_get(config, "sp_md_bos_grpo_private_noise_lambda", _cfg_get(config, "md_bos_grpo_noise_lambda", _cfg_get(config, "bos_grpo_lambda", 1.0)))),
            0.0,
        )
        floor = min(
            max(float(_cfg_get(config, "sp_md_bos_grpo_weight_floor", _cfg_get(config, "md_bos_grpo_weight_floor", _cfg_get(config, "bos_grpo_weight_floor", 0.3)))), 0.0),
            1.0,
        )
        power = max(float(_cfg_get(config, "sp_md_bos_grpo_weight_power", _cfg_get(config, "md_bos_grpo_weight_power", _cfg_get(config, "bos_grpo_weight_power", 1.0)))), eps)
        mix_with_vanilla = min(
            max(
                float(
                    _cfg_get(
                        config,
                        "sp_md_bos_grpo_mix_with_vanilla",
                        _cfg_get(config, "md_bos_grpo_mix_with_vanilla", _cfg_get(config, "bos_grpo_mix_with_vanilla", 0.3)),
                    )
                ),
                0.0,
            ),
            1.0,
        )
        positive_only = _cfg_get_bool(
            config,
            "sp_md_bos_grpo_positive_eigs_only",
            _cfg_get(config, "md_bos_grpo_positive_eigs_only", _cfg_get(config, "bos_grpo_positive_eigs_only", True)),
        )
        fallback_to_vanilla = _cfg_get_bool(
            config,
            "sp_md_bos_grpo_fallback_to_vanilla",
            _cfg_get(config, "md_bos_grpo_fallback_to_vanilla", _cfg_get(config, "bos_grpo_fallback_to_vanilla", True)),
        )
        min_domain_count = max(int(_cfg_get(config, "sp_md_bos_grpo_min_domain_count", _cfg_get(config, "md_bos_grpo_min_domain_count", 2))), 1)
        domain_key = str(_cfg_get(config, "sp_md_bos_grpo_domain_key", _cfg_get(config, "md_bos_grpo_domain_key", "domain")))

        domains_np = None
        if non_tensor_batch is not None:
            domains_np = non_tensor_batch.get(domain_key)
            if domains_np is None and domain_key != "ability":
                domains_np = non_tensor_batch.get("ability")
            if domains_np is None and domain_key != "data_source":
                domains_np = non_tensor_batch.get("data_source")
        domains_np = _as_numpy_object_array(domains_np, batch_size)
        domain_names = [str(v) if v is not None else "unknown" for v in domains_np.tolist()]

        domain_to_indices: dict[str, list[int]] = defaultdict(list)
        mask_f = response_mask.to(device=device, dtype=torch.float32)
        valid_seq = mask_f.bool().any(dim=-1)
        for i, name in enumerate(domain_names):
            if bool(valid_seq[i].item()):
                domain_to_indices[name].append(i)

        seq_weights = torch.ones_like(scalar_adv, dtype=torch.float32)
        retained_ratio = torch.ones_like(scalar_adv, dtype=torch.float32)
        usable_domains = [(name, idxs) for name, idxs in domain_to_indices.items() if idxs]
        domain_counts = [len(v) for v in domain_to_indices.values()]

        shared_selected_rank = 0
        shared_positive_eig_count = 0
        private_rank_values: list[float] = []
        private_positive_values: list[float] = []
        fallback_used = False

        shared_lost_signal_norm2 = torch.zeros((), device=device, dtype=torch.float32)
        sp_lost_signal_norm2 = torch.zeros((), device=device, dtype=torch.float32)
        weighted_private_residual_norm2 = torch.zeros((), device=device, dtype=torch.float32)
        weighted_mu_norm2 = torch.zeros((), device=device, dtype=torch.float32)
        private_residual_max_ratio = torch.zeros((), device=device, dtype=torch.float32)
        private_noise_trace = torch.zeros((), device=device, dtype=torch.float32)
        shared_noise_trace = torch.zeros((), device=device, dtype=torch.float32)
        sp_noise_trace = torch.zeros((), device=device, dtype=torch.float32)
        shared_retained_mu_ratio = torch.tensor(1.0, device=device, dtype=torch.float32)
        sp_retained_mu_ratio = torch.tensor(1.0, device=device, dtype=torch.float32)
        noise_kept_ratio = torch.tensor(1.0, device=device, dtype=torch.float32)

        per_domain_metrics: dict[str, float] = {}
        if dim > 0 and len(usable_domains) >= min_domain_count and batch_size > 1 and (k_shared > 0 or k_private > 0):
            domain_means = []
            domain_covs = []
            omega_values = []
            n_values = []
            total_count = float(sum(len(idxs) for _, idxs in usable_domains))
            for _, idxs_list in usable_domains:
                idx_t = torch.as_tensor(idxs_list, device=device, dtype=torch.long)
                u_d = u[idx_t]
                mu_d = u_d.mean(dim=0)
                n_d = int(idx_t.numel())
                if n_d > 1:
                    centered_d = u_d - mu_d.unsqueeze(0)
                    cov_d = centered_d.transpose(0, 1).matmul(centered_d) / max(n_d - 1, 1)
                else:
                    cov_d = torch.zeros(dim, dim, device=device, dtype=torch.float32)
                domain_means.append(mu_d)
                domain_covs.append(cov_d)
                omega_values.append(float(n_d) / max(total_count, 1.0))
                n_values.append(n_d)

            omega_t = torch.tensor(omega_values, device=device, dtype=torch.float32)
            mu_stack = torch.stack(domain_means, dim=0)
            mu_global = (omega_t.unsqueeze(-1) * mu_stack).sum(dim=0)

            M = torch.zeros(dim, dim, device=device, dtype=torch.float32)
            C_dom = torch.zeros_like(M)
            C_noise = torch.zeros_like(M)
            for d_idx, cov_d in enumerate(domain_covs):
                omega = omega_t[d_idx]
                mu_d = mu_stack[d_idx]
                diff = mu_d - mu_global
                M = M + omega * torch.outer(mu_d, mu_d)
                C_dom = C_dom + omega * torch.outer(diff, diff)
                C_noise = C_noise + (omega.square() / max(n_values[d_idx], 1)) * cov_d

            B_shared = M - lambda_dom * C_dom - lambda_shared_noise * C_noise
            Q_shared, shared_selected_rank, shared_positive_eig_count = _select_eigen_basis(
                B_shared,
                rank=k_shared,
                positive_only=positive_only,
                eps=eps,
            )

            if Q_shared.shape[1] > 0:
                P_shared = Q_shared.matmul(Q_shared.transpose(0, 1))
                R_shared = torch.eye(dim, device=device, dtype=torch.float32) - P_shared
            else:
                R_shared = torch.eye(dim, device=device, dtype=torch.float32)

            private_bases: list[torch.Tensor] = []
            for d_idx, cov_d in enumerate(domain_covs):
                mu_d = mu_stack[d_idx]
                residual_mu_d = mu_d - _project_vector_basis(mu_d, Q_shared)
                cov_residual_d = R_shared.matmul(cov_d).matmul(R_shared)
                B_private = torch.outer(residual_mu_d, residual_mu_d) - lambda_private_noise * cov_residual_d
                Q_private_raw, _, positive_private = _select_eigen_basis(
                    B_private,
                    rank=k_private,
                    positive_only=positive_only,
                    eps=eps,
                )
                Q_private = _orthonormalize_against(Q_private_raw, Q_shared, eps=eps)
                if Q_private.shape[1] > k_private:
                    Q_private = Q_private[:, :k_private]
                private_bases.append(Q_private)
                private_rank_values.append(float(Q_private.shape[1]))
                private_positive_values.append(float(positive_private))

            any_selected = shared_selected_rank > 0 or any(Qp.shape[1] > 0 for Qp in private_bases)
            if not any_selected:
                fallback_used = bool(fallback_to_vanilla)
                if not fallback_to_vanilla:
                    seq_weights = torch.full_like(seq_weights, floor)
                    retained_ratio = torch.zeros_like(retained_ratio)
                    shared_retained_mu_ratio = torch.tensor(0.0, device=device)
                    sp_retained_mu_ratio = torch.tensor(0.0, device=device)
                    noise_kept_ratio = torch.tensor(0.0, device=device)
            else:
                shared_lost_vec = torch.zeros(dim, device=device, dtype=torch.float32)
                sp_lost_vec = torch.zeros(dim, device=device, dtype=torch.float32)
                weighted_shared_retained = torch.zeros((), device=device, dtype=torch.float32)
                weighted_sp_retained = torch.zeros((), device=device, dtype=torch.float32)

                trace_noise = torch.trace(C_noise).clamp_min(eps)
                shared_noise_trace = _trace_in_basis(C_noise, Q_shared)

                for d_idx, (name, idxs_list) in enumerate(usable_domains):
                    idx_t = torch.as_tensor(idxs_list, device=device, dtype=torch.long)
                    omega = omega_t[d_idx]
                    n_d = max(n_values[d_idx], 1)
                    mu_d = mu_stack[d_idx]
                    cov_d = domain_covs[d_idx]
                    Q_private = private_bases[d_idx]
                    if Q_shared.shape[1] > 0 and Q_private.shape[1] > 0:
                        Q_domain = torch.cat([Q_shared, Q_private], dim=1)
                    elif Q_shared.shape[1] > 0:
                        Q_domain = Q_shared
                    else:
                        Q_domain = Q_private

                    proj_u = _project_rows_basis(u[idx_t], Q_domain)
                    u_norm2 = u[idx_t].square().sum(dim=-1)
                    retained = proj_u.square().sum(dim=-1) / u_norm2.clamp_min(eps)
                    retained = torch.where(u_norm2 > eps, retained.clamp(0.0, 1.0), torch.ones_like(retained))
                    retained_ratio[idx_t] = retained

                    shaped = floor + (1.0 - floor) * retained.pow(power)
                    shaped = mix_with_vanilla + (1.0 - mix_with_vanilla) * shaped
                    seq_weights[idx_t] = shaped.clamp(0.0, 1.0)

                    shared_residual_mu_d = mu_d - _project_vector_basis(mu_d, Q_shared)
                    sp_residual_mu_d = mu_d - _project_vector_basis(mu_d, Q_domain)
                    shared_lost_vec = shared_lost_vec + omega * shared_residual_mu_d
                    sp_lost_vec = sp_lost_vec + omega * sp_residual_mu_d

                    mu_norm2 = mu_d.square().sum().clamp_min(eps)
                    shared_retained_d = (_project_vector_basis(mu_d, Q_shared).square().sum() / mu_norm2).clamp(0.0, 1.0)
                    sp_retained_d = (_project_vector_basis(mu_d, Q_domain).square().sum() / mu_norm2).clamp(0.0, 1.0)
                    final_residual_ratio_d = (sp_residual_mu_d.square().sum() / mu_norm2).clamp(min=0.0)

                    weighted_mu_norm2 = weighted_mu_norm2 + omega * mu_norm2
                    weighted_private_residual_norm2 = weighted_private_residual_norm2 + omega * sp_residual_mu_d.square().sum()
                    private_residual_max_ratio = torch.maximum(private_residual_max_ratio, final_residual_ratio_d)
                    weighted_shared_retained = weighted_shared_retained + omega * shared_retained_d
                    weighted_sp_retained = weighted_sp_retained + omega * sp_retained_d

                    private_noise_d = (omega.square() / n_d) * _trace_in_basis(cov_d, Q_private)
                    private_noise_trace = private_noise_trace + private_noise_d
                    sp_noise_trace = sp_noise_trace + (omega.square() / n_d) * _trace_in_basis(cov_d, Q_domain)

                    safe_name = _metric_safe_domain_name(name)
                    per_domain_metrics[f"sp_md_bos_grpo/domain/{safe_name}/count"] = float(n_d)
                    per_domain_metrics[f"sp_md_bos_grpo/domain/{safe_name}/private_rank"] = float(Q_private.shape[1])
                    per_domain_metrics[f"sp_md_bos_grpo/domain/{safe_name}/retained_mu_ratio"] = float(sp_retained_d.detach().item())
                    per_domain_metrics[f"sp_md_bos_grpo/domain/{safe_name}/final_residual_ratio"] = float(final_residual_ratio_d.detach().item())
                    per_domain_metrics[f"sp_md_bos_grpo/domain/{safe_name}/private_noise_trace"] = float(private_noise_d.detach().item())
                    per_domain_metrics[f"sp_md_bos_grpo/domain/{safe_name}/seq_weight_mean"] = float(seq_weights[idx_t].mean().detach().item())

                shared_lost_signal_norm2 = shared_lost_vec.square().sum()
                sp_lost_signal_norm2 = sp_lost_vec.square().sum()
                shared_retained_mu_ratio = weighted_shared_retained.clamp(0.0, 1.0)
                sp_retained_mu_ratio = weighted_sp_retained.clamp(0.0, 1.0)
                noise_kept_ratio = (sp_noise_trace / trace_noise).clamp(min=0.0)
        else:
            fallback_used = True

        seq_adv = scalar_adv * seq_weights
        advantages = seq_adv.unsqueeze(-1) * mask_f
        returns = advantages.clone()

        valid = mask_f.bool().any(dim=-1)
        private_rank_mean = float(sum(private_rank_values) / max(len(private_rank_values), 1))
        private_rank_min = float(min(private_rank_values) if private_rank_values else 0.0)
        private_rank_max = float(max(private_rank_values) if private_rank_values else 0.0)
        private_positive_mean = float(sum(private_positive_values) / max(len(private_positive_values), 1))
        signal_gain = shared_lost_signal_norm2 - sp_lost_signal_norm2
        shared_private_margin = signal_gain - private_noise_trace
        ideal_private_margin = shared_lost_signal_norm2 - private_noise_trace
        private_residual_ratio = weighted_private_residual_norm2 / weighted_mu_norm2.clamp_min(eps)

        metrics = {
            "sp_md_bos_grpo/domain_count": float(len(usable_domains)),
            "sp_md_bos_grpo/domain_count_min": float(min(domain_counts) if domain_counts else 0),
            "sp_md_bos_grpo/domain_count_max": float(max(domain_counts) if domain_counts else 0),
            "sp_md_bos_grpo/used_update_sketch": float(used_update_sketch),
            "sp_md_bos_grpo/feature_dim": float(dim),
            "sp_md_bos_grpo/shared_rank": float(shared_selected_rank),
            "sp_md_bos_grpo/shared_positive_eig_count": float(shared_positive_eig_count),
            "sp_md_bos_grpo/private_rank_mean": private_rank_mean,
            "sp_md_bos_grpo/private_rank_min": private_rank_min,
            "sp_md_bos_grpo/private_rank_max": private_rank_max,
            "sp_md_bos_grpo/private_positive_eig_count_mean": private_positive_mean,
            "sp_md_bos_grpo/fallback_used": float(fallback_used),
            "sp_md_bos_grpo/shared_retained_mu_ratio": float(shared_retained_mu_ratio.detach().item()),
            "sp_md_bos_grpo/sp_retained_mu_ratio": float(sp_retained_mu_ratio.detach().item()),
            "sp_md_bos_grpo/noise_kept_ratio": float(noise_kept_ratio.detach().item()),
            "sp_md_bos_grpo/seq_weight_mean": seq_weights[valid].mean().detach().item() if valid.any() else 1.0,
            "sp_md_bos_grpo/seq_weight_min": seq_weights[valid].min().detach().item() if valid.any() else 1.0,
            "sp_md_bos_grpo/seq_weight_max": seq_weights[valid].max().detach().item() if valid.any() else 1.0,
            "sp_md_bos_grpo/retained_update_ratio_mean": retained_ratio[valid].mean().detach().item() if valid.any() else 1.0,
            "sp_md_bos_grpo/scalar_adv_std": scalar_adv.std(unbiased=False).detach().item() if scalar_adv.numel() else 0.0,
            "sp_md_bos_grpo/token_adv_std": advantages[response_mask.bool()].std(unbiased=False).detach().item()
            if response_mask.bool().any()
            else 0.0,
            "sp_md_bos_grpo/proxy_shared_only_lost_signal_norm2": float(shared_lost_signal_norm2.detach().item()),
            "sp_md_bos_grpo/proxy_shared_private_lost_signal_norm2": float(sp_lost_signal_norm2.detach().item()),
            "sp_md_bos_grpo/proxy_private_signal_gain_norm2": float(signal_gain.detach().item()),
            "sp_md_bos_grpo/proxy_private_noise_trace": float(private_noise_trace.detach().item()),
            "sp_md_bos_grpo/proxy_shared_private_vs_shared_margin": float(shared_private_margin.detach().item()),
            "sp_md_bos_grpo/proxy_shared_private_vs_shared_condition_met": float((shared_private_margin > 0).detach().item()),
            "sp_md_bos_grpo/proxy_private_completeness_residual_norm2": float(weighted_private_residual_norm2.detach().item()),
            "sp_md_bos_grpo/proxy_private_completeness_residual_ratio": float(private_residual_ratio.detach().item()),
            "sp_md_bos_grpo/proxy_private_completeness_residual_max_ratio": float(private_residual_max_ratio.detach().item()),
            "sp_md_bos_grpo/proxy_ideal_private_margin": float(ideal_private_margin.detach().item()),
            "sp_md_bos_grpo/proxy_ideal_private_condition_met": float((ideal_private_margin > 0).detach().item()),
            "sp_md_bos_grpo/proxy_shared_noise_trace": float(shared_noise_trace.detach().item()),
            "sp_md_bos_grpo/proxy_shared_private_noise_trace": float(sp_noise_trace.detach().item()),
        }
        metrics.update(per_domain_metrics)

    return advantages.to(dtype=reward_dtype), returns.to(dtype=reward_dtype), metrics


@register_adv_est(AdvantageEstimator.SNR_MULTI_DOMAIN_GRPO)
@register_adv_est("snr_md_grpo")
def compute_snr_multi_domain_grpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: Optional[np.ndarray] = None,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    responses: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    non_tensor_batch: Optional[dict[str, Any]] = None,
    update_sketch: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor, dict[str, float]]:
    """Signal-to-noise multi-domain GRPO with sketch-based update proxies.

    The estimator builds proxy trajectory updates ``u_i = A_i phi_i``.  When
    available, ``phi_i`` is the response-mean of a Megatron categorical-score
    sketch returned by the actor; otherwise it falls back to the older rollout
    feature proxy for compatibility.  Each sample is weighted by an estimated
    signal-to-noise ratio and then rescaled per domain to preserve proxy update
    energy.
    """
    del kwargs
    device = token_level_rewards.device
    reward_dtype = token_level_rewards.dtype
    eps = max(float(_cfg_get(config, "snr_md_grpo_eps", epsilon)), 1e-12)

    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1).to(device=device, dtype=torch.float32)
        batch_size = int(scores.numel())
        if index is None:
            group_index = torch.arange(batch_size, device=device, dtype=torch.long)
        else:
            group_index = as_torch_index(index, device=device)

        mean_g, std_g, _ = group_mean_std(scores, group_index, eps=eps, device=device)
        if norm_adv_by_std_in_grpo:
            scalar_adv = (scores - mean_g[group_index]) / (std_g[group_index] + eps)
        else:
            scalar_adv = scores - mean_g[group_index]

        mask_f = response_mask.to(device=device, dtype=torch.float32)
        lengths = mask_f.sum(dim=-1, keepdim=True).clamp_min(1.0)
        include_std = _cfg_get_bool(config, "snr_md_grpo_include_feature_std", False)
        normalize_features = _cfg_get_bool(config, "snr_md_grpo_normalize_features", True)

        used_update_sketch = update_sketch is not None and update_sketch.dim() == 3
        if used_update_sketch:
            sketch = update_sketch.to(device=device, dtype=torch.float32)
            sketch = sketch * mask_f.unsqueeze(-1)
            mean_features = sketch.sum(dim=1) / lengths
            if include_std:
                centered = (sketch - mean_features.unsqueeze(1)) * mask_f.unsqueeze(-1)
                std_features = (centered.square().sum(dim=1) / lengths).sqrt()
                phi = torch.cat([mean_features, std_features], dim=-1)
            else:
                phi = mean_features
        else:
            phi = _multi_domain_bos_domain_features(
                response_mask=response_mask,
                responses=responses,
                old_log_probs=old_log_probs,
                epsilon=eps,
                include_std=include_std,
                normalize=False,
            ).to(device=device, dtype=torch.float32)

        if normalize_features and phi.shape[0] > 1:
            feat_mean = phi.mean(dim=0, keepdim=True)
            feat_std = phi.std(dim=0, unbiased=False, keepdim=True).clamp_min(eps)
            phi = (phi - feat_mean) / feat_std

        domain_key = str(_cfg_get(config, "snr_md_grpo_domain_key", "domain"))
        min_domain_count = max(int(_cfg_get(config, "snr_md_grpo_min_domain_count", 2)), 1)

        domains_np = None
        if non_tensor_batch is not None:
            domains_np = non_tensor_batch.get(domain_key)
            if domains_np is None and domain_key != "ability":
                domains_np = non_tensor_batch.get("ability")
            if domains_np is None and domain_key != "data_source":
                domains_np = non_tensor_batch.get("data_source")
        domains_np = _as_numpy_object_array(domains_np, batch_size)
        domain_names = [str(v) if v is not None else "unknown" for v in domains_np.tolist()]

        valid_seq = mask_f.bool().any(dim=-1)
        domain_to_indices: dict[str, list[int]] = defaultdict(list)
        for i, name in enumerate(domain_names):
            if bool(valid_seq[i].item()):
                domain_to_indices[name].append(i)

        u = scalar_adv.unsqueeze(-1) * phi
        dim = int(u.shape[-1])
        raw_weights = torch.ones(batch_size, device=device, dtype=torch.float32)
        seq_weights = torch.ones(batch_size, device=device, dtype=torch.float32)
        signal_values = torch.zeros(batch_size, device=device, dtype=torch.float32)
        conflict_values = torch.zeros(batch_size, device=device, dtype=torch.float32)
        stability_values = torch.zeros(batch_size, device=device, dtype=torch.float32)
        domain_scales = torch.ones(batch_size, device=device, dtype=torch.float32)

        usable_domains = [(name, idxs) for name, idxs in domain_to_indices.items() if idxs]
        total_count = float(sum(len(idxs) for _, idxs in usable_domains))
        per_domain_metrics: dict[str, float] = {}

        if dim > 0 and len(usable_domains) >= min_domain_count and total_count > 0:
            domain_stats: dict[str, dict[str, Any]] = {}
            for name, idxs_list in usable_domains:
                idx_t = torch.as_tensor(idxs_list, device=device, dtype=torch.long)
                u_d = u[idx_t]
                n_d = int(idx_t.numel())
                mu_d = u_d.mean(dim=0)
                if n_d > 1:
                    centered = u_d - mu_d.unsqueeze(0)
                    cov_d = centered.transpose(0, 1).matmul(centered) / max(n_d - 1, 1)
                else:
                    cov_d = torch.zeros(dim, dim, device=device, dtype=torch.float32)
                domain_stats[name] = {
                    "idx": idx_t,
                    "n": n_d,
                    "omega": float(n_d) / max(total_count, 1.0),
                    "sum": u_d.sum(dim=0),
                    "mu": mu_d,
                    "cov": cov_d,
                }

            for name, idxs_list in usable_domains:
                stats_d = domain_stats[name]
                idx_t = stats_d["idx"]
                n_d = int(stats_d["n"])
                u_d = u[idx_t]
                cov_d = stats_d["cov"]
                mu_sum_d = stats_d["sum"]

                for local_pos, sample_idx in enumerate(idx_t.tolist()):
                    u_i = u[sample_idx]
                    if n_d > 1:
                        mu_own = (mu_sum_d - u_i) / float(n_d - 1)
                    else:
                        mu_own = stats_d["mu"]
                    own_norm2 = mu_own.square().sum().clamp_min(eps)
                    signal = torch.relu(torch.dot(u_i, mu_own)).square() / own_norm2

                    conflict = torch.zeros((), device=device, dtype=torch.float32)
                    denom_weight = max(1.0 - float(stats_d["omega"]), eps)
                    for other_name, other_stats in domain_stats.items():
                        if other_name == name:
                            continue
                        mu_other = other_stats["mu"]
                        other_norm2 = mu_other.square().sum().clamp_min(eps)
                        conflict_weight = float(other_stats["omega"]) / denom_weight
                        conflict = conflict + conflict_weight * torch.relu(-torch.dot(u_i, mu_other)).square() / other_norm2

                    u_norm = u_i.norm()
                    if n_d > 1 and bool((u_norm > eps).item()):
                        q_i = u_i / u_norm.clamp_min(eps)
                        stability = q_i.matmul(cov_d).dot(q_i).clamp_min(0.0)
                    else:
                        stability = torch.zeros((), device=device, dtype=torch.float32)

                    denom = signal + conflict + stability + eps
                    raw_weight = signal / denom
                    signal_values[sample_idx] = signal
                    conflict_values[sample_idx] = conflict
                    stability_values[sample_idx] = stability
                    raw_weights[sample_idx] = raw_weight.clamp(0.0, 1.0)

                proxy_energy = u_d.square().sum(dim=-1)
                base_energy = proxy_energy.sum()
                weighted_energy = (raw_weights[idx_t].square() * proxy_energy).sum()
                if bool((base_energy > eps).item()) and bool((weighted_energy > eps).item()):
                    scale_d = torch.sqrt(base_energy / weighted_energy.clamp_min(eps))
                else:
                    scale_d = torch.ones((), device=device, dtype=torch.float32)
                seq_weights[idx_t] = raw_weights[idx_t] * scale_d
                domain_scales[idx_t] = scale_d

                safe_name = _metric_safe_domain_name(name)
                per_domain_metrics[f"snr_md_grpo/domain/{safe_name}/count"] = float(n_d)
                per_domain_metrics[f"snr_md_grpo/domain/{safe_name}/raw_weight_mean"] = float(raw_weights[idx_t].mean().detach().item())
                per_domain_metrics[f"snr_md_grpo/domain/{safe_name}/seq_weight_mean"] = float(seq_weights[idx_t].mean().detach().item())
                per_domain_metrics[f"snr_md_grpo/domain/{safe_name}/domain_scale"] = float(scale_d.detach().item())

        seq_adv = scalar_adv * seq_weights
        advantages = seq_adv.unsqueeze(-1) * mask_f
        returns = advantages.clone()

        valid = valid_seq
        metrics = {
            "snr_md_grpo/domain_count": float(len(usable_domains)),
            "snr_md_grpo/used_update_sketch": float(used_update_sketch),
            "snr_md_grpo/feature_dim": float(dim),
            "snr_md_grpo/raw_weight_mean": raw_weights[valid].mean().detach().item() if valid.any() else 1.0,
            "snr_md_grpo/raw_weight_min": raw_weights[valid].min().detach().item() if valid.any() else 1.0,
            "snr_md_grpo/raw_weight_max": raw_weights[valid].max().detach().item() if valid.any() else 1.0,
            "snr_md_grpo/seq_weight_mean": seq_weights[valid].mean().detach().item() if valid.any() else 1.0,
            "snr_md_grpo/seq_weight_min": seq_weights[valid].min().detach().item() if valid.any() else 1.0,
            "snr_md_grpo/seq_weight_max": seq_weights[valid].max().detach().item() if valid.any() else 1.0,
            "snr_md_grpo/domain_scale_mean": domain_scales[valid].mean().detach().item() if valid.any() else 1.0,
            "snr_md_grpo/signal_mean": signal_values[valid].mean().detach().item() if valid.any() else 0.0,
            "snr_md_grpo/conflict_mean": conflict_values[valid].mean().detach().item() if valid.any() else 0.0,
            "snr_md_grpo/stability_mean": stability_values[valid].mean().detach().item() if valid.any() else 0.0,
            "snr_md_grpo/scalar_adv_std": scalar_adv.std(unbiased=False).detach().item() if scalar_adv.numel() else 0.0,
            "snr_md_grpo/token_adv_std": advantages[response_mask.bool()].std(unbiased=False).detach().item()
            if response_mask.bool().any()
            else 0.0,
        }
        metrics.update(per_domain_metrics)

    return advantages.to(dtype=reward_dtype), returns.to(dtype=reward_dtype), metrics


@torch.no_grad()
def _lpo_group_targets(
    scores: torch.Tensor,
    group_index: Any,
    config: Optional[AlgoConfig] = None,
    adaptive: bool = False,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Build per-response LPO targets on each prompt group.

    The returned target is on-policy/listwise: P_t is uniform inside each group.
    For adaptive mode, choose a = 1 / tau by maximizing
        (E_w[R] - E_{P_t}[R]) * relative_ESS(w || P_t)
    over a data-dependent one-dimensional grid.
    """

    device = scores.device
    dtype = scores.dtype
    scores_f = scores.detach().reshape(-1).to(device=device, dtype=torch.float32)
    if group_index is None:
        gidx = torch.arange(scores_f.numel(), device=device, dtype=torch.long)
    else:
        gidx = as_torch_index(group_index, device=device)
    if scores_f.numel() != gidx.numel():
        raise ValueError(f"LPO scores and group index length mismatch: {scores_f.numel()} vs {gidx.numel()}")

    target = torch.zeros_like(scores_f)
    chosen_a = torch.zeros_like(scores_f)
    ess = torch.ones_like(scores_f)
    reward_gain = torch.zeros_like(scores_f)
    group_count = int(torch.max(gidx).item()) + 1 if gidx.numel() > 0 else 0

    eps = float(_cfg_get(config, "lpo_eps", 1e-8))
    fixed_tau = float(_cfg_get(config, "lpo_tau", 1.0))
    fixed_tau = max(fixed_tau, eps)
    grid_size = int(_cfg_get(config, "lpo_adaptive_grid_size", 64))
    grid_size = max(grid_size, 2)
    max_logit_gap = float(_cfg_get(config, "lpo_adaptive_max_logit_gap", 20.0))
    max_logit_gap = max(max_logit_gap, eps)

    for group_id in range(group_count):
        mask = gidx == group_id
        n = int(mask.sum().item())
        if n <= 0:
            continue

        r = scores_f[mask]
        p = torch.full((n,), 1.0 / n, device=device, dtype=torch.float32)

        if n == 1 or torch.max(r) - torch.min(r) <= eps:
            w = p
            best_a = torch.zeros((), device=device, dtype=torch.float32)
            best_eta = torch.ones((), device=device, dtype=torch.float32)
            best_gain = torch.zeros((), device=device, dtype=torch.float32)
        elif adaptive:
            span = (torch.max(r) - torch.min(r)).clamp_min(eps)
            a_max = max_logit_gap / span
            a_grid = torch.linspace(0.0, 1.0, grid_size, device=device, dtype=torch.float32).square() * a_max

            def eval_target_objective(a_value: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
                logits_1d = a_value * r
                log_z_1d = torch.logsumexp(torch.log(p) + logits_1d, dim=0)
                w_1d = torch.exp(torch.log(p) + logits_1d - log_z_1d)
                gain_1d = torch.clamp(torch.sum(w_1d * r) - torch.sum(p * r), min=0.0)
                log_z2_1d = torch.logsumexp(torch.log(p) + 2.0 * logits_1d, dim=0)
                eta_1d = torch.exp(2.0 * log_z_1d - log_z2_1d).clamp(min=eps, max=1.0)
                return gain_1d * eta_1d, gain_1d, eta_1d, w_1d

            # Shape: (G, n)
            logits = a_grid.unsqueeze(1) * r.unsqueeze(0)
            log_z = torch.logsumexp(torch.log(p).unsqueeze(0) + logits, dim=1)
            w_grid = torch.exp(torch.log(p).unsqueeze(0) + logits - log_z.unsqueeze(1))

            mu0 = torch.sum(p * r)
            mu = torch.sum(w_grid * r.unsqueeze(0), dim=1)
            gain = torch.clamp(mu - mu0, min=0.0)

            log_z2 = torch.logsumexp(torch.log(p).unsqueeze(0) + 2.0 * logits, dim=1)
            eta = torch.exp(2.0 * log_z - log_z2).clamp(min=eps, max=1.0)
            objective = gain * eta

            best_idx = int(torch.argmax(objective).item())
            best_a = a_grid[best_idx]
            w = w_grid[best_idx]
            best_eta = eta[best_idx]
            best_gain = gain[best_idx]

            # Refine inside the winning grid cell. This is a numerical
            # precision step, not an algorithmic hyperparameter.
            refine_steps = 16
            lo_idx = max(best_idx - 1, 0)
            hi_idx = min(best_idx + 1, grid_size - 1)
            lo = a_grid[lo_idx]
            hi = a_grid[hi_idx]
            if bool((hi > lo).item()):
                golden = (math.sqrt(5.0) - 1.0) / 2.0
                c = hi - golden * (hi - lo)
                d = lo + golden * (hi - lo)
                fc, _, _, _ = eval_target_objective(c)
                fd, _, _, _ = eval_target_objective(d)
                for _ in range(refine_steps):
                    if bool((fc < fd).item()):
                        lo = c
                        c = d
                        fc = fd
                        d = lo + golden * (hi - lo)
                        fd, _, _, _ = eval_target_objective(d)
                    else:
                        hi = d
                        d = c
                        fd = fc
                        c = hi - golden * (hi - lo)
                        fc, _, _, _ = eval_target_objective(c)

                candidate_as = torch.stack([a_grid[best_idx], lo, hi, c, d])
                candidate_vals = []
                candidate_payloads = []
                for a_candidate in candidate_as:
                    payload = eval_target_objective(a_candidate)
                    candidate_vals.append(payload[0])
                    candidate_payloads.append(payload)
                refined_idx = int(torch.argmax(torch.stack(candidate_vals)).item())
                best_a = candidate_as[refined_idx]
                _, best_gain, best_eta, w = candidate_payloads[refined_idx]
        else:
            best_a = torch.as_tensor(1.0 / fixed_tau, device=device, dtype=torch.float32)
            logits = best_a * r
            log_z = torch.logsumexp(torch.log(p) + logits, dim=0)
            w = torch.exp(torch.log(p) + logits - log_z)

            mu0 = torch.sum(p * r)
            best_gain = torch.clamp(torch.sum(w * r) - mu0, min=0.0)
            log_z2 = torch.logsumexp(torch.log(p) + 2.0 * logits, dim=0)
            best_eta = torch.exp(2.0 * log_z - log_z2).clamp(min=eps, max=1.0)

        target[mask] = w
        chosen_a[mask] = best_a
        ess[mask] = best_eta
        reward_gain[mask] = best_gain

    metrics = {
        "lpo/a_mean": chosen_a.mean().detach().item() if chosen_a.numel() else 0.0,
        "lpo/tau_mean": (1.0 / chosen_a[chosen_a > eps]).mean().detach().item() if torch.any(chosen_a > eps) else 0.0,
        "lpo/relative_ess_mean": ess.mean().detach().item() if ess.numel() else 1.0,
        "lpo/reward_gain_mean": reward_gain.mean().detach().item() if reward_gain.numel() else 0.0,
        "lpo/group_count": float(group_count),
    }
    return target.to(dtype=dtype), metrics


def _lpo_advantage_from_target(
    scores: torch.Tensor,
    target: torch.Tensor,
    group_index: Any,
    projection: str,
    config: Optional[AlgoConfig] = None,
) -> torch.Tensor:
    """Convert a listwise target into a sequence-scalar policy-gradient coefficient.

    For forward KL, the on-policy ascent coefficient is w* - P_t. To match
    g = (1/K) sum_k A_k grad log pi_k under uniform P_t, use
    A_k = K * (w*_k - 1/K).

    For reverse KL, the on-policy ascent coefficient is P_t * (phi_k - E_P[phi]);
    with uniform P_t and phi_k = a R_k this gives A_k = a(R_k - mean R).
    """

    device = scores.device
    dtype = scores.dtype
    scores_f = scores.detach().reshape(-1).to(device=device, dtype=torch.float32)
    target_f = target.detach().reshape(-1).to(device=device, dtype=torch.float32)
    if group_index is None:
        gidx = torch.arange(scores_f.numel(), device=device, dtype=torch.long)
    else:
        gidx = as_torch_index(group_index, device=device)
    projection = projection.lower()
    eps = float(_cfg_get(config, "lpo_eps", 1e-8))
    fixed_tau = max(float(_cfg_get(config, "lpo_tau", 1.0)), eps)

    advantages = torch.zeros_like(scores_f)
    group_count = int(torch.max(gidx).item()) + 1 if gidx.numel() > 0 else 0
    for group_id in range(group_count):
        mask = gidx == group_id
        n = int(mask.sum().item())
        if n <= 0:
            continue
        p = 1.0 / n
        if projection in {"forward", "fwd", "kl_fwd"}:
            advantages[mask] = n * (target_f[mask] - p)
        elif projection in {"reverse", "rev", "kl_rev"}:
            r = scores_f[mask]
            tau = fixed_tau
            # If adaptive mode was used, infer a local slope from the target
            # logits when possible; otherwise fall back to 1 / lpo_tau.
            centered = r - r.mean()
            denom = centered.square().sum()
            if denom > eps and torch.all(target_f[mask] > 0):
                logw = torch.log(target_f[mask].clamp_min(eps))
                a = ((logw - logw.mean()) * centered).sum() / denom
                tau = 1.0 / a.clamp_min(eps)
            advantages[mask] = (r - r.mean()) / tau
        else:
            raise ValueError(f"Unsupported LPO projection: {projection}")
    return advantages.to(dtype=dtype)


@register_adv_est(AdvantageEstimator.LPO)
@register_adv_est("lpo_forward")
def compute_lpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: Optional[np.ndarray] = None,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    responses: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Fixed-temperature first-order LPO target projection.

    This estimator is microbatch-safe: it constructs w* before the actor update
    and broadcasts the resulting sequence coefficient over response tokens.
    Use algorithm.lpo_projection=forward (default) or reverse.
    """

    del epsilon, norm_adv_by_std_in_grpo, responses, old_log_probs, kwargs
    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1)
        target, metrics = _lpo_group_targets(scores=scores, group_index=index, config=config, adaptive=False)
        projection = str(_cfg_get(config, "lpo_projection", "forward"))
        seq_adv = _lpo_advantage_from_target(scores=scores, target=target, group_index=index, projection=projection, config=config)
        advantages = seq_adv.unsqueeze(-1) * response_mask
        if config is not None:
            # Returned via ray_trainer for estimators that emit a metrics dict.
            return advantages, advantages, metrics
        return advantages, advantages


@register_adv_est(AdvantageEstimator.LPO_ADAPTIVE)
@register_adv_est("lpo_adaptive_forward")
def compute_lpo_adaptive_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: Optional[np.ndarray] = None,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    responses: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Adaptive-temperature first-order LPO target projection.

    Chooses a = 1/tau per prompt by maximizing reward gain times relative ESS,
    then converts the target into a forward/reverse LPO policy coefficient.
    """

    del epsilon, norm_adv_by_std_in_grpo, responses, old_log_probs, kwargs
    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1)
        target, metrics = _lpo_group_targets(scores=scores, group_index=index, config=config, adaptive=True)
        projection = str(_cfg_get(config, "lpo_projection", "forward"))
        seq_adv = _lpo_advantage_from_target(scores=scores, target=target, group_index=index, projection=projection, config=config)
        advantages = seq_adv.unsqueeze(-1) * response_mask
        if config is not None:
            return advantages, advantages, metrics
        return advantages, advantages


@register_adv_est(AdvantageEstimator.GDPO)  # or simply: @register_adv_est("gdpo")
def compute_gdpo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    non_tensor_batch: Optional[dict] = None,
    batch: Optional[dict] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    GDPO: Group reward-Decoupled Normalization Policy Optimization.

    Instead of summing all reward dimensions first (like GRPO), GDPO normalizes
    each reward dimension independently within each group before aggregation.
    This prevents a dominant reward signal from drowning out weaker ones.

    Mathematical formulation:
        Step 1 – Group-wise decoupled normalization (via GRPO per dimension):
            For each reward dimension k, within each group g:
            A_k = (r_k - μ_group(r_k)) / (σ_group(r_k) + ε)

        Step 2 – Weighted aggregation:
            A_sum = Σ_k w_k · A_k

        Step 3 – Batch-level normalization (via masked_whiten):
            A_final = whiten(A_sum, response_mask)

    Args:
        token_level_rewards: (bs, response_length) – standard token-level rewards.
            Used as fallback when per-dimension rewards are not provided.
        response_mask: (bs, response_length)
        index: (bs,) – group id per sample (from ``uid``).
        epsilon: Numerical stability constant.
        norm_adv_by_std_in_grpo: Whether to normalize by std in GRPO.
        config: Algorithm configuration (optional).
        non_tensor_batch: Non-tensor batch data containing per-dimension reward scores.
        batch: Batch data containing prompts, attention_mask, etc.

    Note:
        Ref GDPO (https://arxiv.org/abs/2601.05242).

    Returns:
        advantages: (bs, response_length)
        returns: (bs, response_length) – same as advantages (outcome-only).
    """
    score_list = None
    reward_weights = None

    if config is not None and non_tensor_batch is not None and batch is not None:
        gdpo_reward_keys = config.get("gdpo_reward_keys", None)
        assert gdpo_reward_keys, (
            "GDPO requires 'algorithm.gdpo_reward_keys' listing the individual reward "
            "component keys returned by compute_score (e.g. ['format_reward', 'accuracy_reward'])."
        )
        device = token_level_rewards.device
        prompt_length = batch["prompts"].size(1)
        valid_response_length = batch["attention_mask"][:, prompt_length:].sum(dim=1) - 1

        score_list = []
        for key in gdpo_reward_keys:
            assert key in non_tensor_batch, (
                f"GDPO reward key '{key}' not found in non_tensor_batch. "
                f"Available keys: {list(non_tensor_batch.keys())}. "
                f"Make sure your compute_score returns a dict containing '{key}'."
            )
            comp = non_tensor_batch[key]
            rm_score = torch.tensor(np.asarray(comp, dtype=np.float32), device=device)
            rm_scores = torch.zeros_like(response_mask, dtype=torch.float32)
            rm_scores[torch.arange(rm_scores.size(0), device=device), valid_response_length] = rm_score
            score_list.append(rm_scores)

        gdpo_weights = config.get("gdpo_reward_weights", None)
        if gdpo_weights is not None:
            reward_weights = list(gdpo_weights)

    if score_list is None:
        score_list = [token_level_rewards]

    num_scores = len(score_list)

    if reward_weights is not None:
        weights = torch.tensor(reward_weights, dtype=torch.float32, device=token_level_rewards.device)
    else:
        weights = torch.ones(num_scores, dtype=torch.float32, device=token_level_rewards.device)

    new_advantage = None

    for i in range(num_scores):
        normalized_score, _ = compute_grpo_outcome_advantage(
            token_level_rewards=score_list[i],
            response_mask=response_mask,
            index=index,
            epsilon=epsilon,
            norm_adv_by_std_in_grpo=norm_adv_by_std_in_grpo,
            config=config,
        )

        if new_advantage is None:
            new_advantage = weights[i] * normalized_score
        else:
            new_advantage += weights[i] * normalized_score

    advantages = verl_F.masked_whiten(new_advantage, response_mask) * response_mask

    return advantages, advantages


@register_adv_est(AdvantageEstimator.GRPO_PASSK)  # or simply: @register_adv_est("grpo_passk")
def compute_grpo_passk_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for Pass@k using a GRPO-style outcome reward formulation.
    Only the best response per group gets a non-zero advantage: r_max - r_second_max.

    Implemented as described in https://arxiv.org/abs/2503.19595.

    Args:
        token_level_rewards: (bs, response_length)
        response_mask: (bs, response_length)
        index: (bs,) → group ID per sample
        epsilon: float for numerical stability
        config: (AlgoConfig) algorithm settings, which contains "norm_adv_by_std_in_grpo"

    Returns:
        advantages: (bs, response_length)
        returns: (bs, response_length)
    """
    assert config is not None
    # if True, normalize advantage by std within group
    norm_adv_by_std_in_grpo = config.get("norm_adv_by_std_in_grpo", True)
    scores = token_level_rewards.sum(dim=-1)  # (bs,)
    advantages = torch.zeros_like(scores)

    id2scores = defaultdict(list)
    id2indices = defaultdict(list)

    with torch.no_grad():
        bsz = scores.shape[0]
        for i in range(bsz):
            idx = index[i]
            id2scores[idx].append(scores[i])
            id2indices[idx].append(i)

        for idx in id2scores:
            rewards = torch.stack(id2scores[idx])  # (k,)
            if rewards.numel() < 2:
                raise ValueError(
                    f"Pass@k requires at least 2 samples per group. Got {rewards.numel()} for group {idx}."
                )
            topk, topk_idx = torch.topk(rewards, 2)
            r_max, r_second_max = topk[0], topk[1]
            i_max = id2indices[idx][topk_idx[0].item()]
            advantage = r_max - r_second_max
            if norm_adv_by_std_in_grpo:
                std = torch.std(rewards)
                advantage = advantage / (std + epsilon)
            advantages[i_max] = advantage

    advantages = advantages.unsqueeze(-1) * response_mask
    return advantages, advantages


@register_adv_est(
    AdvantageEstimator.REINFORCE_PLUS_PLUS_BASELINE
)  # or simply: @register_adv_est("reinforce_plus_plus_baseline")
def compute_reinforce_plus_plus_baseline_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: torch.Tensor,
    epsilon: float = 1e-6,
    config: Optional[AlgoConfig] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for RF++-baseline (https://arxiv.org/abs/2501.03262), operating only on Outcome reward
    (with only one scalar reward for each response).

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape: (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
        config: (AlgoConfig) algorithm config

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)
    """
    response_length = token_level_rewards.shape[-1]
    scores = token_level_rewards.sum(dim=-1)

    id2score = defaultdict(list)
    id2mean = {}

    with torch.no_grad():
        bsz = scores.shape[0]
        for i in range(bsz):
            id2score[index[i]].append(scores[i])
        for idx in id2score:
            if len(id2score[idx]) == 1:
                id2mean[idx] = torch.tensor(0.0)
            elif len(id2score[idx]) > 1:
                id2mean[idx] = torch.mean(torch.stack(id2score[idx]))
            else:
                raise ValueError(f"no score in prompt index: {idx}")
        for i in range(bsz):
            scores[i] = scores[i] - id2mean[index[i]]

        scores = scores.unsqueeze(-1).tile([1, response_length]) * response_mask
        scores = verl_F.masked_whiten(scores, response_mask) * response_mask

    return scores, scores


@register_adv_est(AdvantageEstimator.RLOO)  # or simply: @register_adv_est("rloo")
def compute_rloo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    config: Optional[AlgoConfig] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for RLOO based on https://arxiv.org/abs/2402.14740

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape: (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
        config: (AlgoConfig) algorithm config

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)
    """
    scores = token_level_rewards.sum(dim=-1)

    id2score = defaultdict(list)
    id2mean = {}

    with torch.no_grad():
        bsz = scores.shape[0]
        for i in range(bsz):
            id2score[index[i]].append(scores[i])
        for idx in id2score:
            if len(id2score[idx]) == 1:
                id2mean[idx] = torch.tensor(0.0)
            elif len(id2score[idx]) > 1:
                id2mean[idx] = torch.mean(torch.stack(id2score[idx]))
            else:
                raise ValueError(f"no score in prompt index: {idx}")
        for i in range(bsz):
            response_num = len(id2score[index[i]])
            if response_num > 1:
                scores[i] = scores[i] * response_num / (response_num - 1) - id2mean[index[i]] * response_num / (
                    response_num - 1
                )
        scores = scores.unsqueeze(-1) * response_mask

    return scores, scores


@register_adv_est(AdvantageEstimator.OPO)  # or simply: @register_adv_est("opo")
def compute_opo_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    config: Optional[AlgoConfig] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for OPO based on https://arxiv.org/pdf/2505.23585

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape: (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
        config: (AlgoConfig) algorithm config

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)
    """
    response_length = response_mask.sum(dim=-1)
    scores = token_level_rewards.sum(dim=-1)

    id2score = defaultdict(list)
    id2len = defaultdict(list)
    id2bsl = {}

    with torch.no_grad():
        bsz = scores.shape[0]
        for i in range(bsz):
            id2score[index[i]].append(scores[i])
            id2len[index[i]].append(response_length[i])

        for idx in id2score:
            if len(id2score[idx]) == 1:
                id2bsl[idx] = torch.tensor(0.0)
            elif len(id2score[idx]) > 1:
                score_tensor = torch.stack(id2score[idx])
                len_tensor = torch.stack(id2len[idx])
                id2bsl[idx] = (len_tensor * score_tensor).sum() / len_tensor.sum()
            else:
                raise ValueError(f"no score in prompt index: {idx}")
        for i in range(bsz):
            scores[i] = scores[i] - id2bsl[index[i]]
        scores = scores.unsqueeze(-1) * response_mask

    return scores, scores


@register_adv_est(AdvantageEstimator.REINFORCE_PLUS_PLUS)  # or simply: @register_adv_est("reinforce_plus_plus")
def compute_reinforce_plus_plus_outcome_advantage(
    token_level_rewards: torch.Tensor, response_mask: torch.Tensor, config: Optional[AlgoConfig] = None, **kwargs
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for REINFORCE++.
    This implementation is based on the paper: https://arxiv.org/abs/2501.03262

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape: (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
        config: (AlgoConfig) algorithm config

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)
    """
    assert config is not None
    gamma = config.gamma
    with torch.no_grad():
        returns = torch.zeros_like(token_level_rewards)
        running_return = 0

        for t in reversed(range(token_level_rewards.shape[1])):
            running_return = token_level_rewards[:, t] + gamma * running_return
            returns[:, t] = running_return
            # Reset after EOS
            running_return = running_return * response_mask[:, t]

        advantages = verl_F.masked_whiten(returns, response_mask)
        advantages = advantages * response_mask

    return advantages, returns


@register_adv_est(AdvantageEstimator.REMAX)  # or simply: @register_adv_est("remax")
def compute_remax_outcome_advantage(
    token_level_rewards: torch.Tensor,
    reward_baselines: torch.Tensor,
    response_mask: torch.Tensor,
    config: Optional[AlgoConfig] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for ReMax, operating only on Outcome reward
    This implementation is based on the paper: https://arxiv.org/abs/2310.10505
    (with only one scalar reward for each response).

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape: (bs, response_length)
        reward_baselines: `(torch.Tensor)`
            shape: (bs,)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
        config: (AlgoConfig) algorithm config

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)
    """

    with torch.no_grad():
        returns = (token_level_rewards * response_mask).flip(dims=[-1]).cumsum(dim=-1).flip(dims=[-1])
        advantages = returns - reward_baselines.unsqueeze(-1) * response_mask

    return advantages, returns


@register_adv_est(AdvantageEstimator.GPG)  # or simply: @register_adv_est("gpg")
def compute_gpg_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    f_norm: float = 1.0,
    alpha: float = 1.0,
    config=None,
    **kwargs,
):
    """
    Compute advantage for GPG, operating only on Outcome reward
    (with only one scalar reward for each response).
    Args:
        token_level_rewards: `(torch.Tensor)`
            shape: (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
        index: `(np.ndarray)`
            shape: (bs,)
        epsilon: (float)
        f_norm: (float)
        alpha: (float)
        config: (dict) algorithm config

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)
    """
    scores = token_level_rewards.sum(dim=-1)

    id2score = defaultdict(list)
    id2mean = {}
    id2std = {}

    with torch.no_grad():
        bsz = scores.shape[0]
        m = torch.count_nonzero(scores)
        alpha = bsz / m.clamp(min=1)

        for i in range(bsz):
            id2score[index[i]].append(scores[i])

        for idx in id2score:
            if len(id2score[idx]) == 1:
                id2mean[idx] = torch.tensor(0.0)
                id2std[idx] = torch.tensor(1.0)
            elif len(id2score[idx]) > 1:
                scores_tensor = torch.stack(id2score[idx])
                id2mean[idx] = torch.mean(scores_tensor)
                id2std[idx] = torch.std(scores_tensor)
            else:
                raise ValueError(f"no score in prompt index: {idx}")
        for i in range(bsz):
            scores[i] = alpha * (scores[i] - id2mean[index[i]]) / (f_norm)
        scores = scores.unsqueeze(-1) * response_mask

    return scores, scores


@register_adv_est(AdvantageEstimator.RLOO_VECTORIZED)  # or simply: @register_adv_est("rloo_vectorized")
def compute_rloo_vectorized_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    config: Optional[AlgoConfig] = None,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantage for RLOO based on https://arxiv.org/abs/2402.14740

    Args:
        token_level_rewards: `(torch.Tensor)`
            shape: (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
        config: (AlgoConfig) algorithm config

    Returns:
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        Returns: `(torch.Tensor)`
            shape: (bs, response_length)
    """
    scores = token_level_rewards.sum(dim=-1)

    with torch.no_grad():
        inv = torch.from_numpy(np.unique(index, return_inverse=True)[1]).to(scores.device)

        c = torch.bincount(inv)[inv].to(scores.dtype)
        adv = ((c * scores - torch.bincount(inv, weights=scores)[inv]) / (c - 1).clamp_min(1)) * (c > 1)

        adv = adv.unsqueeze(-1) * response_mask

    return adv, adv


@register_adv_est(AdvantageEstimator.OPTIMAL_TOKEN_BASELINE)
def compute_optimal_token_baseline_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    old_log_probs: torch.Tensor,
    sum_pi_squared: torch.Tensor,
    rollout_is_weights: torch.Tensor = None,
    handle_zero_tail: bool = True,
    epsilon: float = 1e-8,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantages using Optimal Token Baseline (OTB).

    Unlike the group mean based baseline which uses a single baseline per trajectory,
    this computes a unique baseline for each timestep using cumulative path variance.

    Theory:
        For each timestep t in each prompt group:
            B_t* = E[G_t × W_t] / E[W_t]
        where W_t = Σ_{j=1}^t ||s_j||² (cumulative path-variance proxy)
        and ||s_j||² = 1 - 2π_j + Σπ²

    The cumulative sum W_t captures the "realized energy" of trajectory has been up to timestep t,
    giving higher weight to predicting rewards on high-variance paths.

    Args:
        token_level_rewards: Rewards at each token position [shape: (bs, response_length)]
        response_mask: Binary mask for valid tokens (1) vs padding (0) [shape: (bs, response_length)]
        index: Prompt indices for grouping trajectories from same prompt [shape: (bs,)]
        old_log_probs: Log probabilities from training policy during generation [shape: (bs, response_length)]
        sum_pi_squared: Sum of squared probabilities over vocabulary Σπ² [shape: (bs, response_length)]
        rollout_is_weights: Pre-computed IS weights for W correction [shape: (bs, response_length)],
            None if not using IS
        handle_zero_tail: If True, zero baselines will be set in the portion of the longest trajectory
            that extends beyond the second-longest trajectory in the prompt group.
            Default: True
        epsilon: Small constant for numerical stability (default: 1e-8)

    Returns:
        advantages: OTB advantage estimates [shape: (bs, response_length)]
        returns: Cumulative rewards (returns) from each position [shape: (bs, response_length)]

    Note on Rollout Importance Sampling:
        When rollout_is_weights is provided, W_t is scaled by ρ̄²(t) to minimize MSE under truncated IS:
            B_t* = Σ[G_t × ρ̄²(t) × W_t] / Σ[ρ̄²(t) × W_t]
    """
    with torch.no_grad():
        batch_size, seq_len = token_level_rewards.shape
        device = token_level_rewards.device

        # Compute returns (reward-to-go) for each timestep
        returns = (token_level_rewards * response_mask).flip(dims=[-1]).cumsum(dim=-1).flip(dims=[-1])

        # Step 1: Compute w_per_timestep = 1 - 2π_t + Σπ²)
        pi_t = torch.exp(old_log_probs)
        w_per_timestep = 1 - 2 * pi_t + sum_pi_squared

        # Step 2: Apply rollout importance sampling correction (if enabled)
        if rollout_is_weights is not None:
            # Scale W by ρ̄² to minimize MSE under truncated IS
            w_per_timestep = w_per_timestep * (rollout_is_weights**2)

        # Step 3: Compute cumulative path-variance proxy: W_t = Σ_{j=1}^t w_j
        # This measures accumulated variance from the start of the trajectory up to timestep t
        w_cumulative = (w_per_timestep * response_mask).cumsum(dim=-1)

        # Group trajectories by prompt
        prompt_groups = defaultdict(list)
        for i in range(batch_size):
            prompt_groups[index[i]].append(i)

        # Initialize baselines tensor [batch_size, seq_len]
        baselines = torch.zeros_like(returns)

        # Compute per-step baseline for each prompt group
        for _, trajectory_indices in prompt_groups.items():
            N = len(trajectory_indices)
            if N == 1:
                # Single trajectory - no baseline (advantage = return)
                continue

            traj_idx = torch.tensor(trajectory_indices, device=device)

            # Extract group data [N, seq_len]
            returns_group = returns[traj_idx]
            w_cumulative_group = w_cumulative[traj_idx]
            mask_group = response_mask[traj_idx]

            # Compute per-timestep baseline: B_t = Σ[G_t × W_t] / Σ[W_t]
            # where W_t = Σ_{j=1}^t ||s_j||² (cumulative path variance)
            # Shape: [seq_len]
            numerator = (returns_group * w_cumulative_group * mask_group).sum(dim=0)  # Sum over trajectories
            denominator = (w_cumulative_group * mask_group).sum(dim=0) + epsilon

            baseline_per_step = numerator / denominator  # [seq_len]

            # Assign to all trajectories in this group
            baselines[traj_idx] = baseline_per_step.unsqueeze(0).expand(N, -1)

            if handle_zero_tail:
                # Optionally zero out the portion of the longest trajectory that extends
                # beyond the second-longest trajectory in the prompt group.
                response_lengths = mask_group.sum(dim=-1)
                sorted_lengths, _ = torch.sort(response_lengths)
                max_length = int(sorted_lengths[-1].item())
                second_max_length = int(sorted_lengths[-2].item())
                max_length_idx = (response_lengths == max_length).nonzero(as_tuple=True)[0]
                if max_length_idx.numel() == 1 and max_length > second_max_length:
                    max_length_traj_idx = trajectory_indices[int(max_length_idx[0])]
                    baselines[max_length_traj_idx, second_max_length:] = 0.0

        # Compute advantages: A_t = G_t - B_t
        advantages = (returns - baselines) * response_mask

    return advantages, returns


@register_adv_est(AdvantageEstimator.TIR_OPTIMAL_TOKEN_BASELINE)
def compute_multi_turn_optimal_token_baseline_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    old_log_probs: torch.Tensor,
    sum_pi_squared: torch.Tensor,
    rollout_is_weights: torch.Tensor = None,
    handle_zero_tail: bool = True,
    epsilon: float = 1e-8,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute advantages using Optimal Token Baseline (OTB).

    Unlike the group mean based baseline which uses a single baseline per trajectory,
    this computes a unique baseline for each timestep using cumulative path variance.

    Theory:
        For each timestep t in each prompt group:
            B_t* = E[G_t × W_t] / E[W_t]
        where W_t = Σ_{j=1}^t ||s_j||² (cumulative path-variance proxy)
        and ||s_j||² = 1 - 2π_j + Σπ²

    The cumulative sum W_t captures the "realized energy" of trajectory has been up to timestep t,
    giving higher weight to predicting rewards on high-variance paths.

    Args:
        token_level_rewards: Rewards at each token position [shape: (bs, response_length)]
        response_mask: Binary mask for valid tokens (1) vs padding (0) [shape: (bs, response_length)]
        index: Prompt indices for grouping trajectories from same prompt [shape: (bs,)]
        old_log_probs: Log probabilities from training policy during generation [shape: (bs, response_length)]
        sum_pi_squared: Sum of squared probabilities over vocabulary Σπ² [shape: (bs, response_length)]
        rollout_is_weights: Pre-computed IS weights for W correction [shape: (bs, response_length)],
            None if not using IS
        handle_zero_tail: If True, zero baselines will be set in the portion of the longest trajectory
            that extends beyond the second-longest trajectory in the prompt group.
            Default: False
        epsilon: Small constant for numerical stability (default: 1e-8)

    Returns:
        advantages: OTB advantage estimates [shape: (bs, response_length)]
        returns: Cumulative rewards (returns) from each position [shape: (bs, response_length)]

    Note on Rollout Importance Sampling:
        When rollout_is_weights is provided, W_t is scaled by ρ̄²(t) to minimize MSE under truncated IS:
            B_t* = Σ[G_t × ρ̄²(t) × W_t] / Σ[ρ̄²(t) × W_t]
    """
    with torch.no_grad():
        # Compute returns (reward-to-go) for each timestep
        token_returns = (token_level_rewards * response_mask).flip(dims=[-1]).cumsum(dim=-1).flip(dims=[-1])

        # Step 1: Compute w_per_timestep = 1 - 2π_t + Σπ²)
        pi_t = torch.exp(old_log_probs)
        w_per_timestep = 1 - 2 * pi_t + sum_pi_squared

        # Step 2: Apply rollout importance sampling correction (if enabled)
        if rollout_is_weights is not None:
            # Scale W by ρ̄² to minimize MSE under truncated IS
            w_per_timestep = w_per_timestep * (rollout_is_weights**2)

        # Step 3: Compute cumulative path-variance proxy: W_t = Σ_{j=1}^t w_j
        # This measures accumulated variance from the start of the trajectory up to timestep t
        w_cumulative = (w_per_timestep * response_mask).cumsum(dim=-1)

        # Step 4: Concatenate returns and w_cumulative for each trajectory
        # This allows us to compute baseline per timestep for each trajectory
        response_lengths = response_mask.sum(dim=-1).to(dtype=torch.long)  # [shape: (bs * n, )]
        max_response_length = int(response_lengths.max().item()) if response_lengths.numel() > 0 else 0
        all_w_values = w_cumulative.new_zeros(
            (len(response_lengths), max_response_length)
        )  # [shape: (bs * n, max_response_length)]
        all_returns = torch.zeros_like(all_w_values)
        for i in range(len(response_lengths)):
            length = int(response_lengths[i].item())
            if length == 0:
                continue
            mask = response_mask[i].bool()
            all_w_values[i, :length] = w_cumulative[i, mask]
            all_returns[i, :length] = token_returns[i, mask]

        # Group trajectories by prompt
        prompt_groups = defaultdict(list)
        for i in range(len(response_lengths)):
            if response_lengths[i] == 0:
                continue
            prompt_groups[index[i]].append(i)

        # Compute optimal baseline for each prompt group
        baselines = torch.zeros_like(all_returns)

        for _, trajectory_indices in prompt_groups.items():
            N = len(trajectory_indices)
            traj_idx = torch.tensor(trajectory_indices, device=all_returns.device)

            if N == 1:
                # Single trajectory - no baseline (keep original reward as advantage)
                baselines[traj_idx[0]] = 0.0
                continue

            # Extract group data
            w_group = all_w_values[traj_idx]  # [shape: (N, max_response_length)]
            R_group = all_returns[traj_idx]  # [shape: (N, max_response_length)]
            # Direct optimal baseline - single value for all in group
            b_star = (R_group * w_group).sum(dim=0) / (w_group.sum(dim=0) + epsilon)
            # Convert to match baselines dtype (epsilon can cause float64 promotion)
            baselines[traj_idx] = b_star.to(baselines.dtype)

            if handle_zero_tail:
                # Optionally zero out the portion of the longest trajectory that extends
                # beyond the second-longest trajectory in the prompt group.
                response_lengths_group = response_lengths[traj_idx]
                sorted_lengths, _ = torch.sort(response_lengths_group)
                max_length = int(sorted_lengths[-1].item())
                second_max_length = int(sorted_lengths[-2].item())
                max_length_idx = (response_lengths_group == max_length).nonzero(as_tuple=True)[0]
                if max_length_idx.numel() == 1 and max_length > second_max_length:
                    max_length_traj_idx = trajectory_indices[int(max_length_idx[0])]
                    baselines[max_length_traj_idx, second_max_length:] = 0.0

        # Compute advantages
        all_advantages = all_returns - baselines  # [shape: (bs * n, max_response_length)]

        advantages = torch.zeros_like(token_returns)  # [shape: (bs * n, turn * response_length)]
        for i in range(len(response_lengths)):
            if response_lengths[i] == 0:
                continue
            advantages[i, response_mask[i].bool()] = all_advantages[i, : response_lengths[i]]

        advantages = advantages * response_mask  # [shape: (bs * n * turn, response_length)]

    return advantages, token_returns


def compute_rewards(token_level_scores, old_log_prob, ref_log_prob, kl_ratio):
    """Compute token-level rewards with KL penalty.

    Args:
        token_level_scores (torch.Tensor): Token-level reward scores.
        old_log_prob (torch.Tensor): Log probabilities from current policy.
        ref_log_prob (torch.Tensor): Log probabilities from reference policy.
        kl_ratio (float): KL penalty coefficient.

    Returns:
        torch.Tensor: Token-level rewards with KL penalty applied.
    """
    kl = old_log_prob - ref_log_prob
    return token_level_scores - kl * kl_ratio


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

def agg_loss(
    loss_mat: torch.Tensor,
    loss_mask: torch.Tensor,
    loss_agg_mode: str,
    dp_size: int = 1,
    batch_num_tokens: Optional[int] = None,
    global_batch_size: Optional[int] = None,
    loss_scale_factor: Optional[int] = None,
):
    """
    Aggregate the loss across global batch to ensure the loss is invariant to fsdp/megatron parallelism.

    NOTE: The returned loss has different behaviors for different backend:
    - FSDP: the loss is directly used for backward.
    - Megatron: the loss should be scaled by `num_microbatches` and `cp_size` for pp schedule.

    Args:
        loss_mat: micro batch loss matrix, (bs, response_length)
        loss_mask: micro batch loss mask, (bs, response_length)
        loss_agg_mode: method to aggregate the loss matrix into a scalar
        dp_size: data parallel size
        batch_num_tokens: number of valid tokens in global batch
        global_batch_size: global batch size
        loss_scale_factor: scale factor for "seq-mean-token-sum-norm" mode. If None, uses loss_mask.shape[-1].
            Set this to a constant value to ensure consistent normalization throughout training.

    Returns:
        loss: `a scalar torch.Tensor`
            aggregated loss
    """
    if loss_agg_mode == "token-mean":
        if batch_num_tokens is None:
            if dp_size > 1:
                raise ValueError("(global) batch_num_tokens is required when dp_size > 1")
            batch_num_tokens = loss_mask.sum()
        loss = verl_F.masked_sum(loss_mat, loss_mask) / batch_num_tokens * dp_size
    elif loss_agg_mode in ["seq-mean-token-sum", "seq-mean-token-sum-norm"]:
        seq_losses = torch.sum(loss_mat * loss_mask, dim=-1)  # token-sum
        seq_mask = (torch.sum(loss_mask, dim=-1) > 0).float()  # exclude fully masked sequences
        if global_batch_size is None:
            if dp_size > 1:
                raise ValueError("global_batch_size is required when dp_size > 1")
            global_batch_size = seq_mask.sum()
        loss = verl_F.masked_sum(seq_losses, seq_mask) / global_batch_size * dp_size  # seq-mean
        if loss_agg_mode == "seq-mean-token-sum-norm":
            if loss_scale_factor is None:
                horizon = loss_mask.shape[-1]
                loss_scale_factor = horizon
            loss /= loss_scale_factor
    elif loss_agg_mode == "seq-mean-token-mean":
        seq_mask = torch.sum(loss_mask, dim=-1)  # per-sequence token count
        seq_losses = torch.sum(loss_mat * loss_mask, dim=-1) / (seq_mask + 1e-8)  # token-mean
        seq_mask = (seq_mask > 0).float()  # exclude fully masked sequences
        if global_batch_size is None:
            if dp_size > 1:
                raise ValueError("global_batch_size is required when dp_size > 1")
            global_batch_size = seq_mask.sum()
        loss = verl_F.masked_sum(seq_losses, seq_mask) / global_batch_size * dp_size  # seq-mean
    else:
        raise ValueError(f"Invalid loss_agg_mode: {loss_agg_mode}")

    return loss


def _policy_loss_cfg_get(config: Optional[ActorConfig], key: str, default: Any) -> Any:
    if config is None:
        return default
    policy_loss_cfg = getattr(config, "policy_loss", None)
    if policy_loss_cfg is None:
        return default
    if hasattr(policy_loss_cfg, "get"):
        return policy_loss_cfg.get(key, default)
    return getattr(policy_loss_cfg, key, default)


def _compute_score_norm_proxy(
    old_log_prob: torch.Tensor,
    response_mask: torch.Tensor,
    config: Optional[ActorConfig],
    sum_pi_squared: torch.Tensor | None = None,
) -> tuple[torch.Tensor, bool]:
    """Proxy ||grad log pi(a|s)||^2 using categorical-logit score norm."""
    eps = float(_policy_loss_cfg_get(config, "intentional_score_norm_eps", 1e-8))
    require_sum_pi_squared = bool(_policy_loss_cfg_get(config, "intentional_require_sum_pi_squared", True))
    pi_taken = torch.exp(torch.clamp(old_log_prob.detach(), min=-20.0, max=20.0))

    if sum_pi_squared is None:
        if require_sum_pi_squared:
            raise ValueError(
                "Intentional GRPO requires sum_pi_squared. Set "
                "actor_rollout_ref.actor.calculate_sum_pi_squared=True or set "
                "actor_rollout_ref.actor.policy_loss.intentional_require_sum_pi_squared=False."
            )
        score_norm = 1.0 - pi_taken
        used_sum_pi_squared = False
    else:
        sum_pi_squared = sum_pi_squared.to(device=old_log_prob.device, dtype=old_log_prob.dtype).detach()
        score_norm = 1.0 - 2.0 * pi_taken + sum_pi_squared
        used_sum_pi_squared = True

    mask = response_mask.to(device=old_log_prob.device, dtype=old_log_prob.dtype)
    score_norm = torch.clamp(score_norm, min=eps) * mask
    return score_norm, used_sum_pi_squared


def _compute_intentional_logprob_target(
    negative_approx_kl: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    config: Optional[ActorConfig],
) -> tuple[torch.Tensor, torch.Tensor]:
    eta = float(_policy_loss_cfg_get(config, "intentional_eta", 1.0))
    clip_target = bool(_policy_loss_cfg_get(config, "intentional_clip_target", True))
    raw_target = eta * advantages.detach()

    if not clip_target:
        target = raw_target
        dual_clipped = torch.zeros_like(raw_target, dtype=torch.bool)
    else:
        assert config is not None
        clip_ratio = config.clip_ratio
        clip_ratio_low = config.clip_ratio_low if config.clip_ratio_low is not None else clip_ratio
        clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_ratio
        clip_ratio_c = config.get("clip_ratio_c", 3.0)
        assert clip_ratio_c > 1.0, (
            "The lower bound of the clip_ratio_c for dual-clip PPO should be greater than 1.0,"
            + f" but get the value: {clip_ratio_c}."
        )
        log_lo = math.log(max(1.0 - float(clip_ratio_low), 1e-8))
        log_hi = math.log(1.0 + float(clip_ratio_high))
        log_c = math.log(float(clip_ratio_c))
        log_ratio = negative_approx_kl.detach()
        log_lo_t = torch.as_tensor(log_lo, device=raw_target.device, dtype=raw_target.dtype)
        log_hi_t = torch.as_tensor(log_hi, device=raw_target.device, dtype=raw_target.dtype)
        log_c_t = torch.as_tensor(log_c, device=raw_target.device, dtype=raw_target.dtype)
        pos_target = torch.minimum(raw_target, log_hi_t - log_ratio).clamp(min=0.0)
        neg_target = torch.maximum(raw_target, log_lo_t - log_ratio).clamp(max=0.0)
        target = torch.where(advantages > 0, pos_target, torch.where(advantages < 0, neg_target, torch.zeros_like(raw_target)))
        dual_clipped = (advantages < 0) & (log_ratio > log_c_t)
        target = torch.where(dual_clipped, torch.zeros_like(target), target)

    mask = response_mask.to(device=advantages.device, dtype=advantages.dtype)
    target = target * mask
    clipped = (
        (((target.abs() + 1e-12) < (raw_target.abs() - 1e-12)) | dual_clipped).to(dtype=advantages.dtype) * mask
    )
    return target.detach(), clipped


def _assert_intentional_target_clipping_enabled(config: Optional[ActorConfig], loss_name: str) -> None:
    if not bool(_policy_loss_cfg_get(config, "intentional_clip_target", True)):
        raise ValueError(
            f"{loss_name} does not have an independent PPO clipped objective. "
            "Set actor_rollout_ref.actor.policy_loss.intentional_clip_target=True, "
            "or use loss_mode=vanilla / vanilla_adaptive_alpha_grpo for clipped vanilla behavior."
        )


def _compute_vanilla_grpo_terms(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    config: ActorConfig,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    clip_ratio = config.clip_ratio
    clip_ratio_low = config.clip_ratio_low if config.clip_ratio_low is not None else clip_ratio
    clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_ratio
    clip_ratio_c = config.get("clip_ratio_c", 3.0)
    assert clip_ratio_c > 1.0, (
        "The lower bound of the clip_ratio_c for dual-clip PPO should be greater than 1.0,"
        + f" but get the value: {clip_ratio_c}."
    )

    raw_negative_approx_kl = log_prob - old_log_prob
    negative_approx_kl = torch.clamp(raw_negative_approx_kl, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    pg_losses1 = -advantages * ratio
    pg_losses2 = -advantages * torch.clamp(ratio, 1 - clip_ratio_low, 1 + clip_ratio_high)
    clip_pg_losses1 = torch.maximum(pg_losses1, pg_losses2)
    pg_clipfrac = verl_F.masked_mean(torch.gt(pg_losses2, pg_losses1).float(), response_mask)

    pg_losses3 = -advantages * clip_ratio_c
    clip_pg_losses2 = torch.min(pg_losses3, clip_pg_losses1)
    pg_clipfrac_lower = verl_F.masked_mean(
        torch.gt(clip_pg_losses1, pg_losses3) * (advantages < 0).float(), response_mask
    )
    pg_losses = torch.where(advantages < 0, clip_pg_losses2, clip_pg_losses1)

    active_std = torch.le(pg_losses2, pg_losses1)
    active_dual = torch.logical_or(advantages >= 0, torch.ge(pg_losses3, clip_pg_losses1))
    active_kl = torch.logical_and(raw_negative_approx_kl >= -20.0, raw_negative_approx_kl <= 20.0)
    active = (active_std & active_dual & active_kl & (response_mask > 0)).to(dtype=advantages.dtype)
    active_coeff = active * advantages * ratio

    if rollout_is_weights is not None:
        rollout_is_weights = rollout_is_weights.to(device=pg_losses.device, dtype=pg_losses.dtype)
        pg_losses = pg_losses * rollout_is_weights
        active_coeff = active_coeff * rollout_is_weights.detach()

    return pg_losses, active_coeff.detach(), negative_approx_kl, ratio.detach(), ppo_kl, pg_clipfrac, pg_clipfrac_lower


@deprecated("verl.trainer.ppo.core_algos.compute_policy_loss_vanilla")
def compute_policy_loss(
    old_log_prob,
    log_prob,
    advantages,
    response_mask,
    cliprange=None,
    cliprange_low=None,
    cliprange_high=None,
    clip_ratio_c=3.0,
    loss_agg_mode: str = "token-mean",
):
    """
    Compute the clipped policy objective and related metrics for PPO.

    Adapted from
    https://github.com/huggingface/trl/blob/main/trl/trainer/ppo_trainer.py#L1122

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        cliprange (float, optional):
            Clipping parameter ε for standard PPO. See https://arxiv.org/abs/1707.06347.
            Defaults to None (must be provided).
        cliprange_low (float, optional):
            Lower clip range for dual-clip PPO. Defaults to same as `cliprange`.
        cliprange_high (float, optional):
            Upper clip range for dual-clip PPO. Defaults to same as `cliprange`.
        clip_ratio_c (float, optional):
            Lower bound of the ratio for dual-clip PPO. See https://arxiv.org/pdf/1912.09729.
            Defaults to 3.0.
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. Defaults to "token-mean".
    """
    assert clip_ratio_c > 1.0, (
        "The lower bound of the clip_ratio_c for dual-clip PPO should be greater than 1.0,"
        + f" but get the value: {clip_ratio_c}."
    )

    negative_approx_kl = log_prob - old_log_prob
    # Clamp negative_approx_kl for stability
    negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    pg_losses1 = -advantages * ratio
    if cliprange_low is None:
        cliprange_low = cliprange
    if cliprange_high is None:
        cliprange_high = cliprange
    pg_losses2 = -advantages * torch.clamp(
        ratio, 1 - cliprange_low, 1 + cliprange_high
    )  # - clip(ratio, 1-cliprange, 1+cliprange) * A
    clip_pg_losses1 = torch.maximum(
        pg_losses1, pg_losses2
    )  # max(-ratio * A, -clip(ratio, 1-cliprange, 1+cliprange) * A)
    pg_clipfrac = verl_F.masked_mean(torch.gt(pg_losses2, pg_losses1).float(), response_mask)

    pg_losses3 = -advantages * clip_ratio_c
    clip_pg_losses2 = torch.min(pg_losses3, clip_pg_losses1)
    pg_clipfrac_lower = verl_F.masked_mean(
        torch.gt(clip_pg_losses1, pg_losses3) * (advantages < 0).float(), response_mask
    )

    pg_losses = torch.where(advantages < 0, clip_pg_losses2, clip_pg_losses1)
    pg_loss = agg_loss(loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode)

    return pg_loss, pg_clipfrac, ppo_kl, pg_clipfrac_lower


@register_policy_loss("vanilla")  # type: ignore[arg-type]
def compute_policy_loss_vanilla(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for PPO.

    Adapted from
    https://github.com/huggingface/trl/blob/main/trl/trainer/ppo_trainer.py#L1122

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. Defaults to "token-mean".
        config: `(verl.trainer.config.ActorConfig)`:
            config for the actor.
        rollout_log_probs: `(torch.Tensor)`:
            log probabilities of actions under the rollout policy, shape (batch_size, response_length).
    """

    assert config is not None
    assert not isinstance(config, AlgoConfig)
    clip_ratio = config.clip_ratio  # Clipping parameter ε for standard PPO. See https://arxiv.org/abs/1707.06347.
    clip_ratio_low = config.clip_ratio_low if config.clip_ratio_low is not None else clip_ratio
    clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_ratio
    clip_ratio_c = config.get(  # Lower bound of the ratio for dual-clip PPO. See https://arxiv.org/pdf/1912.09729.
        "clip_ratio_c", 3.0
    )

    cliprange = clip_ratio
    cliprange_low = clip_ratio_low
    cliprange_high = clip_ratio_high

    assert clip_ratio_c > 1.0, (
        "The lower bound of the clip_ratio_c for dual-clip PPO should be greater than 1.0,"
        + f" but get the value: {clip_ratio_c}."
    )

    negative_approx_kl = log_prob - old_log_prob
    # Clamp negative_approx_kl for stability
    negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    pg_losses1 = -advantages * ratio
    if cliprange_low is None:
        cliprange_low = cliprange
    if cliprange_high is None:
        cliprange_high = cliprange
    pg_losses2 = -advantages * torch.clamp(
        ratio, 1 - cliprange_low, 1 + cliprange_high
    )  # - clip(ratio, 1-cliprange, 1+cliprange) * A
    clip_pg_losses1 = torch.maximum(
        pg_losses1, pg_losses2
    )  # max(-ratio * A, -clip(ratio, 1-cliprange, 1+cliprange) * A)
    pg_clipfrac = verl_F.masked_mean(torch.gt(pg_losses2, pg_losses1).float(), response_mask)

    pg_losses3 = -advantages * clip_ratio_c
    clip_pg_losses2 = torch.min(pg_losses3, clip_pg_losses1)
    pg_clipfrac_lower = verl_F.masked_mean(
        torch.gt(clip_pg_losses1, pg_losses3) * (advantages < 0).float(), response_mask
    )

    pg_losses = torch.where(advantages < 0, clip_pg_losses2, clip_pg_losses1)

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **get_agg_loss_kwargs(config.global_batch_info),   # ← was **config.global_batch_info
    )

    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("simple_intentional_grpo")
@register_policy_loss("intentional_grpo")
def compute_policy_loss_simple_intentional_grpo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
    sum_pi_squared: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Token-level simple Intentional GRPO.

    It uses the diagonal Fisher proxy ||g_i||^2 ~= 1 - 2*pi_i + sum_a pi_a^2
    and ignores cross-token terms g_i^T g_j.
    """
    assert config is not None
    assert not isinstance(config, AlgoConfig)
    _assert_intentional_target_clipping_enabled(config, "simple_intentional_grpo")

    negative_approx_kl = torch.clamp(log_prob - old_log_prob, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    score_norm, used_sum_pi_squared = _compute_score_norm_proxy(
        old_log_prob=old_log_prob,
        response_mask=response_mask,
        config=config,
        sum_pi_squared=sum_pi_squared,
    )
    target_delta, target_clipped = _compute_intentional_logprob_target(
        negative_approx_kl=negative_approx_kl,
        advantages=advantages,
        response_mask=response_mask,
        config=config,
    )

    coeff = target_delta / score_norm.clamp(min=float(_policy_loss_cfg_get(config, "intentional_score_norm_eps", 1e-8)))
    if rollout_is_weights is not None:
        coeff = coeff * rollout_is_weights.to(device=coeff.device, dtype=coeff.dtype)

    pg_losses = -coeff.detach() * log_prob
    pg_loss = agg_loss(
        loss_mat=pg_losses,
        loss_mask=response_mask,
        loss_agg_mode=loss_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    valid_target = ((advantages != 0).float() * response_mask.to(dtype=advantages.dtype)).to(device=advantages.device)
    pg_clipfrac = verl_F.masked_mean(target_clipped.float(), response_mask)
    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": torch.tensor(0.0, device=pg_loss.device).item(),
        "actor/intentional_eta": float(_policy_loss_cfg_get(config, "intentional_eta", 1.0)),
        "actor/intentional_score_norm_mean": verl_F.masked_mean(score_norm.detach(), response_mask).detach().item(),
        "actor/intentional_coeff_abs_mean": verl_F.masked_mean(coeff.detach().abs(), response_mask).detach().item(),
        "actor/intentional_target_abs_mean": verl_F.masked_mean(target_delta.detach().abs(), response_mask).detach().item(),
        "actor/intentional_target_active_frac": verl_F.masked_mean((target_delta != 0).float(), response_mask).detach().item(),
        "actor/intentional_target_clipped_frac": verl_F.masked_mean(target_clipped.float(), valid_target).detach().item(),
        "actor/intentional_used_sum_pi_squared": float(used_sum_pi_squared),
        "actor/intentional_ratio_mean": verl_F.masked_mean(ratio.detach(), response_mask).detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("vanilla_adaptive_alpha_grpo")
@register_policy_loss("adaptive_alpha_grpo")
def compute_policy_loss_vanilla_adaptive_alpha_grpo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
    sum_pi_squared: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Vanilla clipped GRPO direction with an intentional scalar loss multiplier."""
    assert config is not None
    assert not isinstance(config, AlgoConfig)

    pg_losses, active_coeff, negative_approx_kl, ratio, ppo_kl, pg_clipfrac, pg_clipfrac_lower = _compute_vanilla_grpo_terms(
        old_log_prob=old_log_prob,
        log_prob=log_prob,
        advantages=advantages,
        response_mask=response_mask,
        config=config,
        rollout_is_weights=rollout_is_weights,
    )
    score_norm, used_sum_pi_squared = _compute_score_norm_proxy(
        old_log_prob=old_log_prob,
        response_mask=response_mask,
        config=config,
        sum_pi_squared=sum_pi_squared,
    )
    target_delta, target_clipped = _compute_intentional_logprob_target(
        negative_approx_kl=negative_approx_kl,
        advantages=advantages,
        response_mask=response_mask,
        config=config,
    )

    mask = response_mask.to(device=advantages.device, dtype=advantages.dtype)
    h = (active_coeff * score_norm).detach() * mask
    target_delta = target_delta.detach() * mask
    denom = (h.square() * mask).sum().clamp(min=float(_policy_loss_cfg_get(config, "intentional_score_norm_eps", 1e-8)))
    numer = (target_delta * h * mask).sum()
    alpha_scale = numer / denom
    alpha_min = _policy_loss_cfg_get(config, "intentional_alpha_min", 0.0)
    alpha_max = _policy_loss_cfg_get(config, "intentional_alpha_max", None)
    if alpha_min is not None:
        alpha_scale = torch.clamp(alpha_scale, min=float(alpha_min))
    if alpha_max is not None:
        alpha_scale = torch.clamp(alpha_scale, max=float(alpha_max))

    pg_loss = agg_loss(
        loss_mat=alpha_scale.detach() * pg_losses,
        loss_mask=response_mask,
        loss_agg_mode=loss_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    pred_delta = alpha_scale.detach() * h
    target_error = (pred_delta - target_delta).square()
    target_norm = target_delta.square().sum().clamp(min=float(_policy_loss_cfg_get(config, "intentional_score_norm_eps", 1e-8)))
    normalized_error = (target_error * mask).sum() / target_norm
    valid_target = ((advantages != 0).float() * response_mask.to(dtype=advantages.dtype)).to(device=advantages.device)
    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
        "actor/intentional_eta": float(_policy_loss_cfg_get(config, "intentional_eta", 1.0)),
        "actor/intentional_alpha_scale": alpha_scale.detach().item(),
        "actor/intentional_score_norm_mean": verl_F.masked_mean(score_norm.detach(), response_mask).detach().item(),
        "actor/intentional_target_abs_mean": verl_F.masked_mean(target_delta.detach().abs(), response_mask).detach().item(),
        "actor/intentional_pred_abs_mean": verl_F.masked_mean(pred_delta.detach().abs(), response_mask).detach().item(),
        "actor/intentional_target_error": normalized_error.detach().item(),
        "actor/intentional_target_clipped_frac": verl_F.masked_mean(target_clipped.float(), valid_target).detach().item(),
        "actor/intentional_used_sum_pi_squared": float(used_sum_pi_squared),
        "actor/intentional_ratio_mean": verl_F.masked_mean(ratio.detach(), response_mask).detach().item(),
    }
    return pg_loss, pg_metrics


def _compute_entropy_safe_token_weights(
    entropy_drift_score: torch.Tensor,
    response_mask: torch.Tensor,
    max_iter: int = 32,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Project token weights onto the entropy-safe half-space.

    Solves:
        min_w 0.5 * ||w - 1||^2
        s.t.  sum_j w_j d_j <= 0, 0 <= w_j <= 1

    where positive d_j predicts entropy decrease (entropy drift).

    The solve is deliberately outside autograd: the weights only gate/attenuate
    PPO token terms, while gradients still come only from the policy objective.
    """

    with torch.no_grad():
        mask = response_mask.bool()
        d = torch.where(
            mask,
            entropy_drift_score.detach().float(),
            torch.zeros_like(entropy_drift_score).float(),
        )
        valid_count = mask.float().sum().clamp_min(1.0)
        drift_before = d.sum()

        if drift_before.item() <= 0:
            weights = torch.ones_like(d)
            lagrange = torch.zeros((), device=d.device, dtype=d.dtype)
        else:

            def projected_drift(lam: torch.Tensor) -> torch.Tensor:
                return (torch.clamp(1.0 - lam * d, min=0.0, max=1.0) * d).sum()

            low = torch.zeros((), device=d.device, dtype=d.dtype)
            high = torch.ones((), device=d.device, dtype=d.dtype)
            for _ in range(max_iter):
                if projected_drift(high).item() <= 0:
                    break
                high = high * 2.0

            for _ in range(max_iter):
                mid = (low + high) * 0.5
                if projected_drift(mid).item() > 0:
                    low = mid
                else:
                    high = mid

            lagrange = high
            weights = torch.clamp(1.0 - lagrange * d, min=0.0, max=1.0)
            weights = torch.where(mask, weights, torch.ones_like(weights))

        drift_after = (weights * d).sum()
        clipped = ((weights < 1.0 - 1e-6) & mask).float()
        metrics = {
            "actor/entropy_safe_drift_before": (drift_before / valid_count).detach().item(),
            "actor/entropy_safe_drift_after": (drift_after / valid_count).detach().item(),
            "actor/entropy_safe_weight_mean": verl_F.masked_mean(weights, mask).detach().item(),
            "actor/entropy_safe_weight_min": torch.where(mask, weights, torch.ones_like(weights)).min().detach().item(),
            "actor/entropy_safe_clipped_frac": verl_F.masked_mean(clipped, mask).detach().item(),
            "actor/entropy_safe_lambda": lagrange.detach().item(),
        }

    return weights.to(dtype=entropy_drift_score.dtype), metrics


def _compute_entropy_confidence_signal(
    log_prob: torch.Tensor,
    response_mask: torch.Tensor,
    entropy: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Return q_j, where A_j * q_j > 0 predicts entropy decrease.

    Preferred signal:
        q_j = log p(y_j | s_j) + H(pi(. | s_j))

    This is positive when the sampled token is more likely than a typical token
    under the current next-token distribution. If full token entropy is not
    available, fall back to centering selected-token log-probabilities within
    the valid tokens of this micro-batch.
    """

    with torch.no_grad():
        mask = response_mask.bool()
        selected_log_prob = log_prob.detach()
        if entropy is not None:
            confidence = selected_log_prob + entropy.detach().to(
                dtype=selected_log_prob.dtype,
                device=selected_log_prob.device,
            )
            source_is_entropy = 1.0
        else:
            baseline = verl_F.masked_mean(selected_log_prob, mask).detach()
            confidence = selected_log_prob - baseline
            source_is_entropy = 0.0

        confidence = torch.where(mask, confidence, torch.zeros_like(confidence))
        metrics = {
            "actor/entropy_safe_conf_mean": verl_F.masked_mean(confidence, mask).detach().item(),
            "actor/entropy_safe_conf_pos_frac": verl_F.masked_mean((confidence > 0).float(), mask).detach().item(),
            "actor/entropy_safe_used_token_entropy": source_is_entropy,
        }

    return confidence, metrics


def _compute_reward_first_entropy_advantages(
    advantages: torch.Tensor,
    confidence: torch.Tensor,
    response_mask: torch.Tensor,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Return reward-first entropy-corrected advantages.

    Let A be vanilla advantages and e be the entropy score. We use

        g = A + lambda h

    where h is the smallest first-order entropy-canceling correction that is
    orthogonal to A, and lambda = 1 - rho^2 automatically backs off when A and e
    are nearly parallel. Algebraically this simplifies to

        g = A - <A,e>/<e,e> * e_A_perp

    so the risky division by ||e_A_perp|| is avoided.
    """

    with torch.no_grad():
        mask = response_mask.bool()
        a = torch.where(mask, advantages.detach().float(), torch.zeros_like(advantages).float())
        e = torch.where(mask, confidence.detach().float(), torch.zeros_like(confidence).float())
        mask_f = mask.float()
        valid_count = mask_f.sum().clamp_min(1.0)
        eps = torch.finfo(a.dtype).eps

        aa_sum = (a.square() * mask_f).sum()
        ee_sum = (e.square() * mask_f).sum()
        ae_sum = (a * e * mask_f).sum()

        aa_safe = aa_sum.clamp_min(eps)
        ee_safe = ee_sum.clamp_min(eps)

        rho = ae_sum / torch.sqrt(aa_safe * ee_safe)
        rho = torch.clamp(rho, min=-1.0, max=1.0)
        lambda_auto = torch.clamp(1.0 - rho.square(), min=0.0, max=1.0)

        e_parallel_to_a = (ae_sum / aa_safe) * a
        e_perp = e - e_parallel_to_a
        correction = -(ae_sum / ee_safe) * e_perp
        corrected = a + correction
        corrected = torch.where(mask, corrected, advantages.detach().float())

        drift_before = ae_sum / valid_count
        drift_after = (corrected * e * mask_f).sum() / valid_count
        reward_gain_before = aa_sum / valid_count
        reward_gain_after = (a * corrected * mask_f).sum() / valid_count
        correction_norm = torch.sqrt((correction.square() * mask_f).sum() / valid_count)
        advantage_norm = torch.sqrt(aa_sum / valid_count)
        relative_correction_norm = correction_norm / advantage_norm.clamp_min(eps)

        metrics = {
            "actor/erf_lambda": lambda_auto.detach().item(),
            "actor/erf_rho": rho.detach().item(),
            "actor/erf_rho_sq": rho.square().detach().item(),
            "actor/erf_drift_before": drift_before.detach().item(),
            "actor/erf_drift_after": drift_after.detach().item(),
            "actor/erf_reward_gain_before": reward_gain_before.detach().item(),
            "actor/erf_reward_gain_after": reward_gain_after.detach().item(),
            "actor/erf_relative_correction_norm": relative_correction_norm.detach().item(),
        }

    return corrected.to(dtype=advantages.dtype), metrics


@register_policy_loss("entropy_safe_token")
@register_policy_loss("tespo")
def compute_policy_loss_entropy_safe_token(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
    entropy: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """PPO with target-free token-level entropy-safe projection.

    The loss first builds the standard clipped PPO token losses, then computes
    token weights w in [0, 1] that minimally alter vanilla PPO while enforcing
    a first-order no-entropy-collapse constraint:

        sum_j w_j * A_j * q_j <= 0

    where q_j is a confidence signal. Positive A_j * q_j predicts entropy
    decrease, so only those token terms are attenuated when the vanilla update
    is predicted to reduce entropy.
    """

    assert config is not None
    assert not isinstance(config, AlgoConfig)
    clip_ratio = config.clip_ratio
    clip_ratio_low = config.clip_ratio_low if config.clip_ratio_low is not None else clip_ratio
    clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_ratio
    clip_ratio_c = config.get("clip_ratio_c", 3.0)

    assert clip_ratio_c > 1.0, (
        "The lower bound of the clip_ratio_c for dual-clip PPO should be greater than 1.0,"
        + f" but get the value: {clip_ratio_c}."
    )

    negative_approx_kl = log_prob - old_log_prob
    negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    pg_losses1 = -advantages * ratio
    pg_losses2 = -advantages * torch.clamp(ratio, 1 - clip_ratio_low, 1 + clip_ratio_high)
    clip_pg_losses1 = torch.maximum(pg_losses1, pg_losses2)
    pg_clipfrac = verl_F.masked_mean(torch.gt(pg_losses2, pg_losses1).float(), response_mask)

    pg_losses3 = -advantages * clip_ratio_c
    clip_pg_losses2 = torch.min(pg_losses3, clip_pg_losses1)
    pg_clipfrac_lower = verl_F.masked_mean(
        torch.gt(clip_pg_losses1, pg_losses3) * (advantages < 0).float(), response_mask
    )
    pg_losses = torch.where(advantages < 0, clip_pg_losses2, clip_pg_losses1)

    confidence, confidence_metrics = _compute_entropy_confidence_signal(
        log_prob=log_prob,
        response_mask=response_mask,
        entropy=entropy,
    )
    drift_score = advantages.detach() * confidence
    entropy_safe_weights, entropy_safe_metrics = _compute_entropy_safe_token_weights(
        entropy_drift_score=drift_score,
        response_mask=response_mask,
    )
    pg_losses = pg_losses * entropy_safe_weights

    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses,
        loss_mask=response_mask,
        loss_agg_mode=loss_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    pg_metrics.update(confidence_metrics)
    pg_metrics.update(entropy_safe_metrics)
    return pg_loss, pg_metrics


@register_policy_loss("entropy_reward_first")
@register_policy_loss("erf")
def compute_policy_loss_entropy_reward_first(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
    entropy: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """PPO with reward-first entropy-neutral advantage correction.

    This keeps the vanilla first-order reward direction and adds the smallest
    reward-neutral entropy correction that is safe under the automatic
    lambda = 1 - rho^2 rule. When reward and entropy are nearly parallel, the
    simplified correction collapses toward vanilla PPO.
    """

    assert config is not None
    assert not isinstance(config, AlgoConfig)

    confidence, confidence_metrics = _compute_entropy_confidence_signal(
        log_prob=log_prob,
        response_mask=response_mask,
        entropy=entropy,
    )
    corrected_advantages, reward_first_metrics = _compute_reward_first_entropy_advantages(
        advantages=advantages,
        confidence=confidence,
        response_mask=response_mask,
    )

    pg_loss, pg_metrics = compute_policy_loss_vanilla(
        old_log_prob=old_log_prob,
        log_prob=log_prob,
        advantages=corrected_advantages,
        response_mask=response_mask,
        loss_agg_mode=loss_agg_mode,
        config=config,
        rollout_is_weights=rollout_is_weights,
    )
    pg_metrics.update(confidence_metrics)
    pg_metrics.update(reward_first_metrics)
    return pg_loss, pg_metrics


@register_policy_loss("dppo_tv")
def compute_policy_loss_dppo_tv(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for DPPO-Binary-TV.

    See https://arxiv.org/pdf/2602.04879 for more details.

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. Defaults to "token-mean".
        config: `(verl.trainer.config.ActorConfig)`:
            config for the actor.
        rollout_log_probs: `(torch.Tensor)`:
            log probabilities of actions under the rollout policy, shape (batch_size, response_length).
    """

    assert config is not None
    assert not isinstance(config, AlgoConfig)
    # Note: the clip_ratio is different from the standard PPO, it is the TV divergence threshold for DPPO.
    clip_divergence = config.clip_ratio
    clip_divergence_low = config.clip_ratio_low if config.clip_ratio_low is not None else clip_divergence
    clip_divergence_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_divergence

    negative_approx_kl = log_prob - old_log_prob
    # Clamp negative_approx_kl for stability
    negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    # Instead of dual-clip PPO, we use truncated importance sampling (TIS) to clip the policy loss.
    # However, a large threshold is recommended to avoid performance degradation due to the truncation bias.
    # See Section 5.4 in https://arxiv.org/pdf/2602.04879 for more details.
    clip_ratio_c = config.get("clip_ratio_c", 20.0)
    truncated_ratio = torch.clamp(ratio, max=clip_ratio_c)
    truncated_ratio = truncated_ratio.detach()

    # Compute valid mask for DPPO-Binary-TV
    prob = torch.exp(log_prob)
    old_prob = torch.exp(old_log_prob)
    valid_positive_mask = (prob - old_prob) <= clip_divergence_high
    valid_negative_mask = (prob - old_prob) >= -clip_divergence_low
    valid_mask = torch.where(advantages > 0, valid_positive_mask, valid_negative_mask)
    valid_mask = valid_mask.detach().float()

    pg_losses = -advantages * truncated_ratio * log_prob * valid_mask

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **config.global_batch_info
    )

    pg_clipfrac = verl_F.masked_mean((1.0 - valid_mask).float(), response_mask)
    pg_clipfrac_lower = verl_F.masked_mean((ratio > clip_ratio_c).float() * valid_mask, response_mask)

    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("dppo_kl")
def compute_policy_loss_dppo_kl(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for DPPO-Binary-KL.

    See https://arxiv.org/pdf/2602.04879 for more details.

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. Defaults to "token-mean".
        config: `(verl.trainer.config.ActorConfig)`:
            config for the actor.
        rollout_log_probs: `(torch.Tensor)`:
            log probabilities of actions under the rollout policy, shape (batch_size, response_length).
    """

    assert config is not None
    assert not isinstance(config, AlgoConfig)
    # Note: the clip_ratio is different from the standard PPO, it is the KL divergence threshold for DPPO.
    clip_divergence = config.clip_ratio
    clip_divergence_low = config.clip_ratio_low if config.clip_ratio_low is not None else clip_divergence
    clip_divergence_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_divergence

    negative_approx_kl = log_prob - old_log_prob
    # Clamp negative_approx_kl for stability
    negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    # Instead of dual-clip PPO, we use truncated importance sampling (TIS) to clip the policy loss.
    # However, a large threshold is recommended to avoid performance degradation due to the truncation bias.
    # See Section 5.4 in https://arxiv.org/pdf/2602.04879 for more details.
    clip_ratio_c = config.get("clip_ratio_c", 20.0)
    truncated_ratio = torch.clamp(ratio, max=clip_ratio_c)
    truncated_ratio = truncated_ratio.detach()

    # Compute valid mask for DPPO-Binary-KL
    prob = torch.exp(log_prob)
    old_prob = torch.exp(old_log_prob)
    binary_kl = old_prob * (old_log_prob - log_prob) + (1 - old_prob) * torch.log(
        (1.0 - old_prob + 1e-8) / (1.0 - prob + 1e-8)
    )
    valid_positive_mask = (binary_kl <= clip_divergence_high) | (prob <= old_prob)
    valid_negative_mask = (binary_kl <= clip_divergence_low) | (prob >= old_prob)
    valid_mask = torch.where(advantages > 0, valid_positive_mask, valid_negative_mask)
    valid_mask = valid_mask.detach().float()

    pg_losses = -advantages * truncated_ratio * log_prob * valid_mask

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **config.global_batch_info
    )

    # For compatibility, return zero for pg_clipfrac_lower (not used in standard DPPO)
    pg_clipfrac = verl_F.masked_mean((1.0 - valid_mask).float(), response_mask)
    pg_clipfrac_lower = verl_F.masked_mean((ratio > clip_ratio_c).float() * valid_mask, response_mask)

    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("gspo")
def compute_policy_loss_gspo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for GSPO.

    See https://arxiv.org/pdf/2507.18071 for more details.

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. For GSPO, it is recommended to use "seq-mean-token-mean".
    """

    assert config is not None
    assert isinstance(config, ActorConfig)
    clip_ratio_low = config.clip_ratio_low if config.clip_ratio_low is not None else config.clip_ratio
    clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else config.clip_ratio

    negative_approx_kl = log_prob - old_log_prob

    # compute sequence-level importance ratio:
    # si(θ) = (π_θ(yi|x)/π_θold(yi|x))^(1/|yi|) =
    # exp [(1/|y_i|) * Σ_t log(π_θ(y_i,t|x,y_i,<t)/π_θold(y_i,t|x,y_i,<t))]
    seq_lengths = torch.sum(response_mask, dim=-1).clamp(min=1)
    negative_approx_kl_seq = torch.sum(negative_approx_kl * response_mask, dim=-1) / seq_lengths

    # Combined ratio at token level:
    # s_i,t(θ) = sg[s_i(θ)] · π_θ(y_i,t|x, y_i,<t) / sg[π_θ(y_i,t|x, y_i,<t)]
    # In log space: log(s_i,t(θ)) = sg[log(s_i(θ))] + log_prob - sg[log_prob]
    log_seq_importance_ratio = log_prob - log_prob.detach() + negative_approx_kl_seq.detach().unsqueeze(-1)
    log_seq_importance_ratio = torch.clamp(log_seq_importance_ratio, max=10.0)  # clamp for numerical stability

    # finaly exp() to remove log
    seq_importance_ratio = torch.exp(log_seq_importance_ratio)

    pg_losses1 = -advantages * seq_importance_ratio
    pg_losses2 = -advantages * torch.clamp(seq_importance_ratio, 1 - clip_ratio_low, 1 + clip_ratio_high)
    pg_losses = torch.maximum(pg_losses1, pg_losses2)

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    # for GSPO, we need to aggregate the loss at the sequence level (seq-mean-token-mean)
    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode="seq-mean-token-mean", **config.global_batch_info
    )

    # For compatibility, return zero for pg_clipfrac_lower (not used in standard GSPO)
    pg_clipfrac = verl_F.masked_mean(torch.gt(pg_losses2, pg_losses1).float(), response_mask)
    pg_clipfrac_lower = torch.tensor(0.0, device=pg_loss.device)

    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)
    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("sapo")
def compute_policy_loss_sapo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the smoothed policy objective and related metrics for SAPO.

    See https://arxiv.org/pdf/2511.20347 for more details.

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. For SAPO, it is recommended to use "seq-mean-token-mean".
    """

    assert config is not None
    assert isinstance(config, ActorConfig)

    # temperature for positive and negative token updates
    tau_pos = torch.as_tensor(config.tau_pos, dtype=advantages.dtype, device=advantages.device)
    tau_neg = torch.as_tensor(config.tau_neg, dtype=advantages.dtype, device=advantages.device)

    def gate_function(x, tau):
        """The gating function used in SAPO"""
        return torch.sigmoid(tau * (x - 1.0)) * (4.0 / tau)

    # compute IS at token level:
    # r_{i,t}(θ) = π_θ(y_{i,t}|x, y_{i,<t}) / π_θold(y_{i,t}|x, y_{i,<t})]
    # In log space: log(r_{i,t}(θ)) = log_prob - ol_log_prob
    negative_approx_kl = log_prob - old_log_prob
    # Clamp negative_approx_kl for stability
    negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    # finally exp() to remove log and get r_{i,t}(θ)
    ratio = torch.exp(negative_approx_kl)

    # tau_{i,t} is tau_pos if adv > 0 else tau_neg
    taus = torch.where(
        condition=advantages > 0,
        input=tau_pos,  # if A_{i,t} > 0 we set to tau_pos
        other=tau_neg,  # if A_{i,t} <= 0 we set to tau_neg
    )

    # compute the gates f_{i,t}(r_{i,t}(θ)) at token level
    gates = gate_function(ratio, taus)

    # compute policy gradient loss
    pg_losses = -gates * advantages

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    # for SAPO, we need to aggregate the loss at the sequence level (seq-mean-token-mean)
    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **get_agg_loss_kwargs(config.global_batch_info),   # ← was **config.global_batch_info
    )

    # For compatibility, return zero for both pg_clipfrac and pg_clipfrac_lower (not used in SAPO)
    pg_clipfrac = torch.tensor(0.0, device=pg_loss.device)
    pg_clipfrac_lower = torch.tensor(0.0, device=pg_loss.device)
    # compute KL for metrics tracking
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)
    # return metrics dict
    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }

    return pg_loss, pg_metrics


@register_policy_loss("gpg")
def compute_policy_loss_gpg(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Adapted from
    https://github.com/AMAP-ML/GPG/blob/main/VisualThinker-R1-Zero/src/open-r1-multimodal/src/open_r1/trainer/grpo_trainer.py#L495
    Args:
        log_prob: `(torch.Tensor)`
            shape: (bs, response_length)
        advantages: `(torch.Tensor)`
            shape: (bs, response_length)
        response_mask: `(torch.Tensor)`
            shape: (bs, response_length)
    return:
        pg_loss: `a scalar torch.Tensor`
            policy gradient loss computed via GPG
    """
    assert config is not None
    pg_losses = -log_prob * advantages

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **config.global_batch_info
    )
    return pg_loss, {}


@register_policy_loss("clip_cov")
def compute_policy_loss_clip_cov(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for Clip-Cov.

    Adapted from
    https://github.com/PRIME-RL/Entropy-Mechanism-of-RL/blob/main/verl/trainer/ppo/core_algos.py

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        cliprange (float, optional):
            Clipping parameter ε for standard PPO. See https://arxiv.org/abs/1707.06347.
            Defaults to None (must be provided).
        cliprange_low (float, optional):
            Lower clip range for dual-clip PPO. Defaults to same as `cliprange`.
        cliprange_high (float, optional):
            Upper clip range for dual-clip PPO. Defaults to same as `cliprange`.
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. Defaults to "token-mean".
        clip_cvo_ratio (float, optional):
            Ratio for clipping the covariance. Defaults to 0.0002.
        clip_cov_lb (float, optional):
            Lower bound for clipping covariance. Defaults to 1.0.
        clip_cov_ub (float, optional):
            Upper bound for clipping covariance. Defaults to 5.0.
    """
    assert config is not None
    assert not isinstance(config, AlgoConfig), "passing AlgoConfig not supported yet"
    assert config.policy_loss is not None

    clip_cov_ratio = config.policy_loss.clip_cov_ratio if config.policy_loss.clip_cov_ratio is not None else 0.0002
    cliprange = config.clip_ratio
    cliprange_low = config.clip_ratio_low if config.clip_ratio_low is not None else cliprange
    cliprange_high = config.clip_ratio_high if config.clip_ratio_high is not None else cliprange
    clip_cov_ub = config.policy_loss.clip_cov_ub if config.policy_loss.clip_cov_ub is not None else 5.0
    clip_cov_lb = config.policy_loss.clip_cov_lb if config.policy_loss.clip_cov_lb is not None else 1.0

    assert clip_cov_ratio > 0, "clip_ratio should be larger than 0."

    negative_approx_kl = log_prob - old_log_prob
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    pg_losses1 = -advantages * ratio

    if cliprange_low is None:
        cliprange_low = cliprange
    if cliprange_high is None:
        cliprange_high = cliprange

    corr = torch.ones_like(advantages)
    pg_losses2 = -advantages * torch.clamp(ratio, 1 - cliprange_low, 1 + cliprange_high)
    clip_by_origin = (pg_losses2 > pg_losses1) & (response_mask > 0)

    cov_all = (advantages - verl_F.masked_mean(advantages, response_mask)) * (
        log_prob - verl_F.masked_mean(log_prob.detach(), response_mask)
    )
    cov_all[response_mask == 0] = -torch.inf
    cov_all[clip_by_origin] = -torch.inf

    clip_num = max(int(clip_cov_ratio * response_mask.sum().item()), 1)
    top_k_idx = (cov_all < clip_cov_ub) & (cov_all > clip_cov_lb) & (response_mask > 0)
    top_k_idx = torch.nonzero(top_k_idx)

    if len(top_k_idx) > 0:
        perm = torch.randperm(len(top_k_idx))
        top_k_idx = top_k_idx[perm[: min(clip_num, len(top_k_idx))]]
    else:
        top_k_idx = torch.empty((0, 2), device=cov_all.device, dtype=torch.long)

    corr[top_k_idx[:, 0], top_k_idx[:, 1]] = 0

    pg_clipfrac = verl_F.masked_mean((corr == 0).float(), response_mask)

    pg_losses = torch.maximum(pg_losses1, pg_losses2) * corr

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **config.global_batch_info
    )
    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("kl_cov")
def compute_policy_loss_kl_cov(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for Clip-Cov.

    Adapted from
    https://github.com/PRIME-RL/Entropy-Mechanism-of-RL/blob/main/verl/trainer/ppo/core_algos.py

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. Defaults to "token-mean".
        kl_cov_ratio (float, optional):
            Ratio for selecting the top-k covariance values. Defaults to 0.0002.
        ppo_kl_coef (float, optional):
            Coefficient for the KL penalty term in the loss. Defaults to 1.
    """
    assert config is not None
    assert not isinstance(config, AlgoConfig), "passing AlgoConfig not supported yet"
    assert config.policy_loss is not None

    kl_cov_ratio = config.policy_loss.kl_cov_ratio if config.policy_loss.kl_cov_ratio is not None else 0.0002
    ppo_kl_coef = config.policy_loss.ppo_kl_coef if config.policy_loss.ppo_kl_coef is not None else 1.0

    assert kl_cov_ratio > 0, "kl_cov_ratio should be larger than 0."

    negative_approx_kl = log_prob - old_log_prob
    abs_kl = negative_approx_kl.abs()
    ratio = torch.exp(negative_approx_kl)
    ppo_kl_abs = verl_F.masked_mean(negative_approx_kl.abs(), response_mask)
    pg_losses1 = -advantages * ratio
    pg_losses_kl = -advantages * ratio + ppo_kl_coef * abs_kl
    pg_losses = pg_losses1

    all_valid = response_mask > 0
    all_valid_idx = torch.nonzero(all_valid.reshape(-1), as_tuple=True)[0]
    all_valid_adv = advantages[all_valid].detach().reshape(-1).cpu()
    all_valid_logp = log_prob[all_valid].detach().reshape(-1).cpu()

    k = min(kl_cov_ratio, len(all_valid_adv))

    if k != 0:
        cov_lst_all = (all_valid_adv - all_valid_adv.mean()) * (all_valid_logp - all_valid_logp.mean())
        k_percent_nums = max(1, int(len(cov_lst_all) * kl_cov_ratio))
        large_cov_idxs = torch.topk(cov_lst_all, k_percent_nums, largest=True).indices

        if len(large_cov_idxs) != 0:
            large_cov_idxs = all_valid_idx[large_cov_idxs]
            pg_losses[large_cov_idxs // advantages.shape[1], large_cov_idxs % advantages.shape[1]] = pg_losses_kl[
                large_cov_idxs // advantages.shape[1], large_cov_idxs % advantages.shape[1]
            ]

    # Apply rollout correction weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **config.global_batch_info
    )
    pg_metrics = {
        "actor/ppo_kl": ppo_kl_abs.detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("geo_mean")
def compute_policy_loss_geo_mean(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for GMPO.

    Adapted from paper https://arxiv.org/abs/2507.20673
    https://github.com/callsys/GMPO/blob/main/train_zero_math_gmpo.py

    Args:
        old_log_prob (torch.Tensor):
            Log-probabilities of actions under the old policy, shape (batch_size, response_length).
        log_prob (torch.Tensor):
            Log-probabilities of actions under the current policy, shape (batch_size, response_length).
        advantages (torch.Tensor):
            Advantage estimates for each action, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the loss, shape (batch_size, response_length).
        loss_agg_mode (str, optional):
            not used
    """

    assert config is not None
    assert not isinstance(config, AlgoConfig)
    clip_ratio = config.clip_ratio  # Clipping parameter. See https://arxiv.org/abs/1707.06347.
    clip_ratio_low = config.clip_ratio_low if config.clip_ratio_low is not None else clip_ratio
    clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_ratio

    cliprange = clip_ratio
    cliprange_low = clip_ratio_low
    cliprange_high = clip_ratio_high
    if cliprange_low is None:
        cliprange_low = cliprange
    if cliprange_high is None:
        cliprange_high = cliprange

    negative_approx_kl = log_prob - old_log_prob
    # Clamp negative_approx_kl for stability (uncomment it if you like)
    # negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    # Clipping at token-level & Clipping wider
    sgn_advantage = torch.sign(advantages)
    negative_approx_kl_clamp = torch.clamp(negative_approx_kl, -cliprange_low, cliprange_high)
    negative_approx_kl_min = torch.min(sgn_advantage * negative_approx_kl, sgn_advantage * negative_approx_kl_clamp)
    negative_approx_kl_min = sgn_advantage * negative_approx_kl_min

    # Geometric-Mean Policy Optimization
    response_mask_sum = response_mask.sum(dim=-1)
    ratio = torch.exp((negative_approx_kl_min * response_mask).sum(dim=-1) / (response_mask_sum + 1e-8))
    # we only support sequence level advantage for now,
    # otherwise, below would be not consistent with the paper
    advantage = (advantages * response_mask).sum(dim=-1) / (response_mask_sum + 1e-8)
    pg_losses = -advantage * ratio

    # Apply rollout correction weights if provided
    # For geo_mean, IS weights are 2D (batch_size, seq_length) and need to be aggregated to sequence level
    if rollout_is_weights is not None:
        # Aggregate token-level weights to sequence level using geometric mean for consistency
        # Note: rollout_is_weights is always 2D regardless of aggregation mode
        seq_is_weights = torch.exp(
            (torch.log(rollout_is_weights + 1e-10) * response_mask).sum(dim=-1) / (response_mask_sum + 1e-8)
        )
        pg_losses = pg_losses * seq_is_weights

    pg_loss = torch.mean(pg_losses)

    # higher: ratio is too large that need clamp to clip_high (when adv > 0)
    clipped = torch.ne(negative_approx_kl, negative_approx_kl_clamp)
    pg_clipfrac = verl_F.masked_mean((clipped * (advantages > 0)).float(), response_mask)
    pg_clipfrac_lower = verl_F.masked_mean((clipped * (advantages < 0)).float(), response_mask)
    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    return pg_loss, pg_metrics


@register_policy_loss("cispo")
def compute_policy_loss_cispo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[DictConfig | ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Compute the clipped policy objective and related metrics for CISPO.

    See https://arxiv.org/pdf/2506.13585 for more details.
    """

    assert config is not None
    assert isinstance(config, ActorConfig)
    clip_ratio_low = config.clip_ratio_low if config.clip_ratio_low is not None else config.clip_ratio
    clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else config.clip_ratio

    # Compute importance sampling ratio: π_θ / π_θ_old
    negative_approx_kl = log_prob - old_log_prob
    # Clamp for numerical stability
    negative_approx_kl = torch.clamp(negative_approx_kl, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)
    ppo_kl = verl_F.masked_mean(-negative_approx_kl, response_mask)

    # CISPO: Clip the importance sampling weights
    # KEY: Apply stop gradient to the clipped ratio
    # This prevents gradients from flowing through the ratio computation and clipping
    # Gradients only flow through log_prob in the final loss term
    clipped_ratio = torch.clamp(ratio, 1 - clip_ratio_low, 1 + clip_ratio_high)
    clipped_ratio_sg = clipped_ratio.detach()

    # CISPO objective function (to maximize): J = sg(clip(ratio)) * A * log π_θ
    # Loss function (to minimize): L = -J = -sg(clip(ratio)) * A * log_prob
    pg_losses = -clipped_ratio_sg * advantages * log_prob

    # Track clipping statistics
    pg_clipfrac = verl_F.masked_mean((ratio != clipped_ratio).float(), response_mask)

    # Apply rollout importance sampling weights if provided
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **config.global_batch_info
    )

    # For compatibility, return zero for pg_clipfrac_lower (not used in CISPO)
    pg_clipfrac_lower = torch.tensor(0.0, device=pg_loss.device)

    pg_metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    return pg_loss, pg_metrics


def compute_entropy_loss(logits, response_mask, loss_agg_mode: str = "token-mean"):
    """Compute categorical entropy loss (For backward compatibility)

    Args:
        logits (torch.Tensor): shape is (bs, response_length, vocab_size)
        response_mask (torch.Tensor): shape is (bs, response_length)

    Returns:
        entropy: a scalar torch.Tensor

    """
    # compute entropy
    token_entropy = verl_F.entropy_from_logits(logits)  # (bs, response_len)
    entropy_loss = agg_loss(loss_mat=token_entropy, loss_mask=response_mask, loss_agg_mode=loss_agg_mode)
    return entropy_loss


def compute_value_loss(
    vpreds: torch.Tensor,
    returns: torch.Tensor,
    values: torch.Tensor,
    response_mask: torch.Tensor,
    cliprange_value: float,
    loss_agg_mode: str = "token-mean",
    value_loss_weights: Optional[torch.Tensor] = None,
):
    """
    Compute the clipped value-function loss for PPO.

    Copied from https://github.com/huggingface/trl/blob/main/trl/trainer/ppo_trainer.py#L1151

    Args:
        vpreds (torch.FloatTensor):
            Predicted values from the value head, shape (batch_size, response_length).
        values (torch.FloatTensor):
            Old (baseline) values from the value head, shape (batch_size, response_length).
        returns (torch.FloatTensor):
            Ground-truth returns, shape (batch_size, response_length).
        response_mask (torch.Tensor):
            Mask indicating which tokens to include in the value loss calculation.
        cliprange_value (float):
            Clip range for value prediction updates.
        loss_agg_mode (str, optional):
            Aggregation mode for `agg_loss`. Defaults to "token-mean".
        value_loss_weights (torch.Tensor, optional):
            Optional detached per-token MSE weights. Shape must match response_mask.

    Returns:
        vf_loss (torch.FloatTensor):
            A scalar tensor containing the aggregated value-function loss.
        vf_clipfrac (float):
            Fraction of elements where the clipped loss was used.
    """
    vpredclipped = verl_F.clip_by_value(vpreds, values - cliprange_value, values + cliprange_value)
    vf_losses1 = (vpreds - returns) ** 2
    vf_losses2 = (vpredclipped - returns) ** 2
    clipped_vf_losses = torch.max(vf_losses1, vf_losses2)
    if value_loss_weights is not None:
        clipped_vf_losses = clipped_vf_losses * value_loss_weights.to(
            device=clipped_vf_losses.device, dtype=clipped_vf_losses.dtype
        )
    vf_loss = 0.5 * agg_loss(loss_mat=clipped_vf_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode)
    vf_clipfrac = verl_F.masked_mean(torch.gt(vf_losses2, vf_losses1).float(), response_mask)
    return vf_loss, vf_clipfrac


def _build_turn_score_proxy(
    old_log_probs: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: Optional[torch.Tensor] = None,
    eps: float = 1e-8,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Build detached Fisher-score proxy weights.

    Token proxy:
        w_t = (1 - pi_old(a_t | h_t))^2.
    Turn proxy:
        w_i = sum_{t in turn i} w_t.
    """
    valid_mask = response_mask.bool()
    old_log_probs = old_log_probs.to(device=response_mask.device, dtype=torch.float32)
    mask_f = valid_mask.float()
    token_weights = (1.0 - old_log_probs.exp().clamp(max=1.0)).square() * mask_f
    token_weights = torch.where(valid_mask, token_weights.clamp(min=eps), torch.zeros_like(token_weights))

    if turn_index is None:
        sequence_weights = token_weights.sum(dim=-1, keepdim=True).clamp(min=eps)
        return torch.where(valid_mask, sequence_weights.expand_as(token_weights), torch.zeros_like(token_weights)), (
            valid_mask.float().sum(dim=-1).clamp(min=1.0)
        ), token_weights

    turn_index = turn_index.to(device=response_mask.device)
    valid_turn_mask = valid_mask & (turn_index >= 0)
    turn_weights = torch.zeros_like(token_weights)
    turn_counts = torch.zeros(response_mask.shape[0], device=response_mask.device, dtype=token_weights.dtype)
    if valid_turn_mask.any():
        for turn_id in torch.unique(turn_index[valid_turn_mask]):
            turn_mask = valid_turn_mask & (turn_index == turn_id)
            turn_counts += (turn_mask.float().sum(-1) > 0).float()
            weight = (token_weights * turn_mask.float()).sum(dim=-1, keepdim=True).clamp(min=eps)
            turn_weights = torch.where(turn_mask, weight.expand_as(turn_weights), turn_weights)

    return torch.where(valid_mask, turn_weights.clamp(min=eps), torch.zeros_like(turn_weights)), turn_counts, token_weights


def _normalize_value_loss_weights_by_group(
    weights: torch.Tensor,
    valid_mask: torch.Tensor,
    turn_index: Optional[torch.Tensor] = None,
    eps: float = 1e-8,
) -> torch.Tensor:
    """Normalize weights to mean 1 inside each turn, or inside each response if turn_index is unavailable."""
    weights = torch.where(valid_mask, weights, torch.zeros_like(weights))
    mask_f = valid_mask.to(dtype=weights.dtype)
    if turn_index is None:
        denom = (weights * mask_f).sum(dim=-1, keepdim=True)
        count = mask_f.sum(dim=-1, keepdim=True).clamp(min=1.0)
        mean = denom / count
        normalized = weights / mean.clamp(min=eps)
        return torch.where(valid_mask, normalized, torch.zeros_like(normalized))

    turn_index = turn_index.to(device=weights.device)
    valid_turn_mask = valid_mask & (turn_index >= 0)
    normalized = torch.zeros_like(weights)
    if valid_turn_mask.any():
        for turn_id in torch.unique(turn_index[valid_turn_mask]):
            turn_mask = valid_turn_mask & (turn_index == turn_id)
            turn_mask_f = turn_mask.to(dtype=weights.dtype)
            mean = (weights * turn_mask_f).sum(dim=-1, keepdim=True) / turn_mask_f.sum(dim=-1, keepdim=True).clamp(
                min=1.0
            )
            normalized = torch.where(turn_mask, weights / mean.clamp(min=eps), normalized)

    return torch.where(valid_mask, normalized, torch.zeros_like(normalized))


def _build_suffix_score_proxy(
    old_log_probs: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: Optional[torch.Tensor] = None,
    rho: float = 1.0,
    alpha: float = 1.0,
    eps: float = 1e-8,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Build token-level suffix-energy critic weights.

    Local proxy:
        q_t = (1 - pi_old(a_t | h_t))^2.
    Suffix proxy:
        C_t = sum_{k=t}^{T} rho^(k-t) q_k.

    The returned weights are normalized to mean 1 inside each turn (or response
    if turn_index is unavailable) and then optionally sharpened by `alpha`.
    """
    valid_mask = response_mask.bool()
    old_log_probs = old_log_probs.to(device=response_mask.device, dtype=torch.float32)
    mask_f = valid_mask.float()
    token_weights = (1.0 - old_log_probs.exp().clamp(max=1.0)).square() * mask_f
    token_weights = torch.where(valid_mask, token_weights.clamp(min=eps), torch.zeros_like(token_weights))

    rho = float(rho)
    alpha = float(alpha)
    if rho < 0.0 or rho > 1.0:
        raise ValueError(f"value_loss_weight_rho must be in [0, 1], got {rho}.")
    if alpha <= 0.0:
        raise ValueError(f"value_loss_weight_alpha must be > 0, got {alpha}.")

    suffix_weights = torch.zeros_like(token_weights)
    if turn_index is None:
        running = torch.zeros(token_weights.shape[0], device=token_weights.device, dtype=token_weights.dtype)
        for pos in range(token_weights.shape[1] - 1, -1, -1):
            running = token_weights[:, pos] + rho * running
            suffix_weights[:, pos] = torch.where(valid_mask[:, pos], running, torch.zeros_like(running))
    else:
        turn_index = turn_index.to(device=response_mask.device)
        valid_turn_mask = valid_mask & (turn_index >= 0)
        if valid_turn_mask.any():
            for turn_id in torch.unique(turn_index[valid_turn_mask]):
                turn_mask = valid_turn_mask & (turn_index == turn_id)
                running = torch.zeros(token_weights.shape[0], device=token_weights.device, dtype=token_weights.dtype)
                for pos in range(token_weights.shape[1] - 1, -1, -1):
                    running = torch.where(turn_mask[:, pos], token_weights[:, pos] + rho * running, running)
                    suffix_weights[:, pos] = torch.where(turn_mask[:, pos], running, suffix_weights[:, pos])

    suffix_weights = torch.where(valid_mask, suffix_weights.clamp(min=eps), torch.zeros_like(suffix_weights))
    suffix_weights = _normalize_value_loss_weights_by_group(suffix_weights, valid_mask, turn_index, eps=eps)
    if alpha != 1.0:
        suffix_weights = torch.where(valid_mask, suffix_weights.clamp(min=eps).pow(alpha), torch.zeros_like(suffix_weights))
        suffix_weights = _normalize_value_loss_weights_by_group(suffix_weights, valid_mask, turn_index, eps=eps)

    return suffix_weights, token_weights


def _build_turn_suffix_score_proxy(
    old_log_probs: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
    rho: float = 1.0,
    alpha: float = 1.0,
    eps: float = 1e-8,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Build turn-level suffix-energy critic weights.

    Token proxy:
        q_t = (1 - pi_old(a_t | h_t))^2.
    Turn proxy:
        Q_i = sum_{t in turn i} q_t.
    Turn suffix proxy:
        S_i = sum_{j=i}^{K} rho^(j-i) Q_j.

    The returned scalar is normalized over valid turns in each response and
    broadcast to all valid tokens inside the turn.
    """
    valid_mask = response_mask.bool()
    old_log_probs = old_log_probs.to(device=response_mask.device, dtype=torch.float32)
    turn_index = turn_index.to(device=response_mask.device)
    mask_f = valid_mask.float()
    token_weights = (1.0 - old_log_probs.exp().clamp(max=1.0)).square() * mask_f
    token_weights = torch.where(valid_mask, token_weights.clamp(min=eps), torch.zeros_like(token_weights))

    rho = float(rho)
    alpha = float(alpha)
    if rho < 0.0 or rho > 1.0:
        raise ValueError(f"value_loss_weight_rho must be in [0, 1], got {rho}.")
    if alpha <= 0.0:
        raise ValueError(f"value_loss_weight_alpha must be > 0, got {alpha}.")

    valid_turn_mask = valid_mask & (turn_index >= 0)
    turn_suffix_weights = torch.zeros_like(token_weights)
    if not valid_turn_mask.any():
        return turn_suffix_weights, token_weights

    turn_ids = torch.unique(turn_index[valid_turn_mask], sorted=True)
    batch_size = response_mask.shape[0]
    num_turns = turn_ids.numel()
    turn_energy = torch.zeros(batch_size, num_turns, device=response_mask.device, dtype=torch.float32)
    turn_valid = torch.zeros(batch_size, num_turns, device=response_mask.device, dtype=torch.bool)

    for idx, turn_id in enumerate(turn_ids):
        turn_mask = valid_turn_mask & (turn_index == turn_id)
        turn_energy[:, idx] = (token_weights * turn_mask.float()).sum(dim=-1)
        turn_valid[:, idx] = turn_mask.any(dim=-1)

    suffix_energy = torch.zeros_like(turn_energy)
    running = torch.zeros(batch_size, device=response_mask.device, dtype=torch.float32)
    for idx in range(num_turns - 1, -1, -1):
        running = torch.where(turn_valid[:, idx], turn_energy[:, idx] + rho * running, running)
        suffix_energy[:, idx] = torch.where(turn_valid[:, idx], running, torch.zeros_like(running))

    turn_count = turn_valid.float().sum(dim=-1, keepdim=True).clamp(min=1.0)
    mean_suffix = suffix_energy.sum(dim=-1, keepdim=True) / turn_count
    normalized_turn_weights = suffix_energy / mean_suffix.clamp(min=eps)
    normalized_turn_weights = torch.where(turn_valid, normalized_turn_weights, torch.zeros_like(normalized_turn_weights))

    if alpha != 1.0:
        normalized_turn_weights = torch.where(
            turn_valid, normalized_turn_weights.clamp(min=eps).pow(alpha), torch.zeros_like(normalized_turn_weights)
        )
        mean_after_alpha = normalized_turn_weights.sum(dim=-1, keepdim=True) / turn_count
        normalized_turn_weights = normalized_turn_weights / mean_after_alpha.clamp(min=eps)
        normalized_turn_weights = torch.where(
            turn_valid, normalized_turn_weights, torch.zeros_like(normalized_turn_weights)
        )

    for idx, turn_id in enumerate(turn_ids):
        turn_mask = valid_turn_mask & (turn_index == turn_id)
        turn_weight = normalized_turn_weights[:, idx].unsqueeze(-1).expand_as(turn_suffix_weights)
        turn_suffix_weights = torch.where(turn_mask, turn_weight, turn_suffix_weights)

    return torch.where(valid_mask, turn_suffix_weights.clamp(min=eps), torch.zeros_like(turn_suffix_weights)), token_weights


def build_value_loss_weights(
    response_mask: torch.Tensor,
    mode: str = "none",
    turn_index: Optional[torch.Tensor] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    normalize: bool = True,
    clip_min: Optional[float] = None,
    clip_max: Optional[float] = None,
    clip_renormalize: bool = True,
    rho: float = 1.0,
    alpha: float = 1.0,
) -> tuple[Optional[torch.Tensor], dict[str, torch.Tensor]]:
    """Build detached value-loss weights for baseline/critic training."""
    mode = (mode or "none").lower()
    if mode in {"none", "uniform", "token"}:
        return None, {}

    valid_mask = response_mask.bool()
    mask_f = valid_mask.float()
    metrics: dict[str, torch.Tensor] = {}

    if mode in {"response_length", "sequence_length", "active_token_count", "length"}:
        raw_weights = mask_f.sum(dim=-1, keepdim=True).clamp(min=1.0).expand_as(mask_f)
        mode_used = "response_length"
    elif mode in {"turn_length", "multi_turn_length"}:
        if turn_index is None:
            raw_weights = mask_f.sum(dim=-1, keepdim=True).clamp(min=1.0).expand_as(mask_f)
            mode_used = "response_length_fallback"
            metrics["fallback_to_response_length"] = torch.ones((), device=response_mask.device)
        else:
            turn_index = turn_index.to(device=response_mask.device)
            raw_weights = torch.zeros_like(mask_f)
            valid_turn_mask = valid_mask & (turn_index >= 0)
            if valid_turn_mask.any():
                for turn_id in torch.unique(turn_index[valid_turn_mask]):
                    turn_mask = valid_turn_mask & (turn_index == turn_id)
                    turn_len = turn_mask.float().sum(dim=-1, keepdim=True).clamp(min=1.0)
                    raw_weights = torch.where(turn_mask, turn_len.expand_as(raw_weights), raw_weights)
            raw_weights = torch.where(valid_mask, raw_weights.clamp(min=1.0), torch.zeros_like(raw_weights))
            mode_used = "turn_length"
    elif mode in {"token_score_norm", "token_fisher", "token_fisher_score", "token_grad_proxy"}:
        if old_log_probs is None:
            raw_weights = torch.ones_like(mask_f)
            mode_used = "uniform_fallback"
            metrics["fallback_to_uniform"] = torch.ones((), device=response_mask.device)
        else:
            old_log_probs = old_log_probs.to(device=response_mask.device, dtype=torch.float32)
            raw_weights = (1.0 - old_log_probs.exp().clamp(max=1.0)).square() * mask_f
            raw_weights = torch.where(valid_mask, raw_weights.clamp(min=1e-8), torch.zeros_like(raw_weights))
            mode_used = "token_score_norm"
    elif mode in {"turn_score_norm", "turn_fisher", "turn_fisher_score", "turn_grad_proxy", "wi"}:
        if old_log_probs is None:
            raw_weights = mask_f.sum(dim=-1, keepdim=True).clamp(min=1.0).expand_as(mask_f)
            mode_used = "response_length_fallback"
            metrics["fallback_to_response_length"] = torch.ones((), device=response_mask.device)
        else:
            raw_weights, _, token_weights = _build_turn_score_proxy(old_log_probs, response_mask, turn_index)
            mode_used = "turn_score_norm" if turn_index is not None else "response_score_norm"
            if turn_index is None:
                metrics["fallback_to_response_score_norm"] = torch.ones((), device=response_mask.device)
            metrics["token_score_norm_mean"] = verl_F.masked_mean(token_weights, valid_mask)
    elif mode in {"suffix_score_norm", "suffix_fisher", "suffix_fisher_score", "suffix_grad_proxy"}:
        if old_log_probs is None:
            raw_weights = torch.ones_like(mask_f)
            mode_used = "uniform_fallback"
            metrics["fallback_to_uniform"] = torch.ones((), device=response_mask.device)
        else:
            raw_weights, token_weights = _build_suffix_score_proxy(
                old_log_probs=old_log_probs,
                response_mask=response_mask,
                turn_index=turn_index,
                rho=rho,
                alpha=alpha,
            )
            mode_used = "suffix_score_norm"
            if turn_index is None:
                metrics["fallback_to_response_suffix_norm"] = torch.ones((), device=response_mask.device)
            metrics["token_score_norm_mean"] = verl_F.masked_mean(token_weights, valid_mask)
            metrics["suffix_rho"] = torch.tensor(float(rho), device=response_mask.device)
            metrics["suffix_alpha"] = torch.tensor(float(alpha), device=response_mask.device)
    elif mode in {
        "turn_suffix_score_norm",
        "turn_suffix_fisher",
        "turn_suffix_fisher_score",
        "turn_suffix_grad_proxy",
    }:
        if old_log_probs is None:
            raw_weights = torch.ones_like(mask_f)
            mode_used = "uniform_fallback"
            metrics["fallback_to_uniform"] = torch.ones((), device=response_mask.device)
        elif turn_index is None:
            raw_weights, token_weights = _build_suffix_score_proxy(
                old_log_probs=old_log_probs,
                response_mask=response_mask,
                turn_index=None,
                rho=rho,
                alpha=alpha,
            )
            mode_used = "suffix_score_norm"
            metrics["fallback_to_response_suffix_norm"] = torch.ones((), device=response_mask.device)
            metrics["token_score_norm_mean"] = verl_F.masked_mean(token_weights, valid_mask)
            metrics["suffix_rho"] = torch.tensor(float(rho), device=response_mask.device)
            metrics["suffix_alpha"] = torch.tensor(float(alpha), device=response_mask.device)
        else:
            raw_weights, token_weights = _build_turn_suffix_score_proxy(
                old_log_probs=old_log_probs,
                response_mask=response_mask,
                turn_index=turn_index,
                rho=rho,
                alpha=alpha,
            )
            mode_used = "turn_suffix_score_norm"
            metrics["token_score_norm_mean"] = verl_F.masked_mean(token_weights, valid_mask)
            metrics["suffix_rho"] = torch.tensor(float(rho), device=response_mask.device)
            metrics["suffix_alpha"] = torch.tensor(float(alpha), device=response_mask.device)
    else:
        raise ValueError(
            f"Invalid value_loss_weight_mode: {mode}. "
            "Expected one of: none, response_length, turn_length, token_score_norm, turn_score_norm, "
            "suffix_score_norm, turn_suffix_score_norm."
        )

    raw_weights = raw_weights.to(dtype=torch.float32)
    raw_mean = verl_F.masked_mean(raw_weights, valid_mask)
    weights = raw_weights
    if normalize:
        weights = weights / raw_mean.clamp(min=1e-8)

    if clip_min is not None or clip_max is not None:
        clamp_min = float(clip_min) if clip_min is not None else None
        clamp_max = float(clip_max) if clip_max is not None else None
        weights = torch.where(valid_mask, weights.clamp(min=clamp_min, max=clamp_max), torch.zeros_like(weights))
        clipped_mean = verl_F.masked_mean(weights, valid_mask)
        metrics["clipped_mean"] = clipped_mean
        if normalize and clip_renormalize:
            weights = weights / clipped_mean.clamp(min=1e-8)

    metrics.update(
        {
            "raw_mean": raw_mean,
            "mean": verl_F.masked_mean(weights, valid_mask),
            "max": torch.masked_select(weights, valid_mask).max() if valid_mask.any() else weights.new_tensor(0.0),
            "clip_min": torch.tensor(float(clip_min) if clip_min is not None else -1.0, device=response_mask.device),
            "clip_max": torch.tensor(float(clip_max) if clip_max is not None else -1.0, device=response_mask.device),
            "mode_is_token_score_norm": torch.tensor(
                float(mode_used == "token_score_norm"), device=response_mask.device
            ),
            "mode_is_turn_score_norm": torch.tensor(float(mode_used == "turn_score_norm"), device=response_mask.device),
            "mode_is_suffix_score_norm": torch.tensor(
                float(mode_used == "suffix_score_norm"), device=response_mask.device
            ),
            "mode_is_turn_suffix_score_norm": torch.tensor(
                float(mode_used == "turn_suffix_score_norm"), device=response_mask.device
            ),
        }
    )
    return weights.detach(), metrics


def kl_penalty(logprob: torch.FloatTensor, ref_logprob: torch.FloatTensor, kl_penalty) -> torch.FloatTensor:
    """Compute KL divergence given logprob and ref_logprob. Optionally using straight through to bind k2 on other
    kl penalty compute method for unbiased KL gradient estimation.
    See more description in http://joschu.net/blog/kl-approx.html

    Args:
        logprob:
        ref_logprob:

    Returns:
        kl_estimate
    """
    forward_score = kl_penalty_forward(logprob, ref_logprob, kl_penalty)
    if not kl_penalty.endswith("+") or kl_penalty in ("mse", "k2"):
        return forward_score

    """
    The expectation of k1 and k3 estimator is the expected value of KL, but the expected gradient of k1 and k3
    estimator is not the expected gradient of KL. On the other hand k2 estimator gives right gradient estimator, 
    so we use a straight through trick here if the kl_penalty method ends with '+', e.g., k3+. 
    """
    backward_score = 0.5 * (logprob - ref_logprob).square()

    return backward_score - backward_score.detach() + forward_score.detach()


def kl_penalty_forward(logprob: torch.FloatTensor, ref_logprob: torch.FloatTensor, kl_penalty) -> torch.FloatTensor:
    """Compute KL divergence given logprob and ref_logprob.
    Copied from https://github.com/huggingface/trl/blob/main/trl/trainer/ppo_trainer.py#L1104
    See more description in http://joschu.net/blog/kl-approx.html

    Args:
        logprob:
        ref_logprob:

    Returns:
        kl_estimate
    """
    if kl_penalty in ("kl", "k1"):
        return logprob - ref_logprob

    if kl_penalty == "abs":
        return (logprob - ref_logprob).abs()

    if kl_penalty in ("mse", "k2"):
        return 0.5 * (logprob - ref_logprob).square()

    # J. Schulman. Approximating kl divergence, 2020.
    # # URL http://joschu.net/blog/kl-approx.html.
    if kl_penalty in ("low_var_kl", "k3"):
        kl = ref_logprob - logprob
        # For numerical stability
        kl = torch.clamp(kl, min=-20, max=20)
        ratio = torch.exp(kl)
        kld = (ratio - kl - 1).contiguous()
        return torch.clamp(kld, min=-10, max=10)

    if kl_penalty == "full":
        # so, here logprob and ref_logprob should contain the logits for every token in vocabulary
        raise NotImplementedError

    raise NotImplementedError


def compute_pf_ppo_reweight_data(
    data,
    reweight_method: str = "pow",
    weight_pow: float = 2.0,
):
    """Reweight the data based on the token_level_scores.

    Args:
        data: DataProto object, containing batch, non_tensor_batch and meta_info
        reweight_method: str, choices: "pow", "max_min", "max_random"
        weight_pow: float, the power of the weight

    Returns:

    """

    @torch.no_grad()
    def compute_weights(scores: torch.Tensor, reweight_method: str, weight_pow: float) -> torch.Tensor:
        """Compute importance weights for resampling based on scores.

        Args:
            scores (torch.Tensor): Tensor of scores to compute weights from.
            reweight_method (str): Method for computing weights ('pow', 'max_min', 'max_random').
            weight_pow (float): Power exponent for 'pow' method.

        Returns:
            torch.Tensor: Computed importance weights.

        Raises:
            ValueError: If reweight_method is not supported.
        """
        if reweight_method == "pow":
            weights = torch.pow(torch.abs(scores), weight_pow)
        elif reweight_method == "max_min":
            max_score = torch.max(scores)
            min_score = torch.min(scores)
            weights = torch.where((scores == max_score) | (scores == min_score), 1.0, 0.0)
        elif reweight_method == "max_random":
            max_score = torch.max(scores)
            weights = torch.where(scores == max_score, 0.4, 0.1)
        else:
            raise ValueError(f"Unsupported reweight_method: {reweight_method}")
        return weights

    scores = data.batch["token_level_scores"].sum(dim=-1)
    weights = compute_weights(scores, reweight_method, weight_pow)
    weights = torch.clamp(weights + 1e-8, min=1e-8)

    batch_size = scores.shape[0]
    sample_indices = torch.multinomial(weights, batch_size, replacement=True)

    resampled_batch = {key: tensor[sample_indices] for key, tensor in data.batch.items()}

    sample_indices_np = sample_indices.numpy()
    resampled_non_tensor_batch = {}
    for key, array in data.non_tensor_batch.items():
        if isinstance(array, np.ndarray):
            resampled_non_tensor_batch[key] = array[sample_indices_np]
        else:
            resampled_non_tensor_batch[key] = [array[i] for i in sample_indices_np]

    resampled_meta_info = {}
    for key, value in data.meta_info.items():
        if isinstance(value, list) and len(value) == batch_size:
            resampled_meta_info[key] = [value[i] for i in sample_indices_np]
        else:
            resampled_meta_info[key] = value

    from copy import deepcopy

    resampled_data = deepcopy(data)
    resampled_data.batch = type(data.batch)(resampled_batch)
    resampled_data.batch.batch_size = data.batch.batch_size
    resampled_data.non_tensor_batch = resampled_non_tensor_batch
    resampled_data.meta_info = resampled_meta_info

    return resampled_data


def compute_policy_loss_reinforce(
    rollout_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: Optional[torch.Tensor] = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Compute REINFORCE-style policy gradient loss with optional IS correction.

    This function implements policy gradient (REINFORCE) with optional importance
    sampling correction for rollout-training policy mismatch.

    Mathematical formulation:
        Without IS (rollout_is_weights=None):
            L = -E[log π(a|s) * A(s,a)]
            Gradient: ∇_θ L = -E[∇log π(a|s) * A] (standard REINFORCE)

        With IS (rollout_is_weights provided):
            L = -E_π_rollout[w * log π(a|s) * A(s,a)]
            where w = π_current / π_rollout (truncated IS weight)
            Gradient: ∇_θ L = -E[w * ∇log π(a|s) * A] (IS-corrected policy gradient)

    Args:
        rollout_log_prob: Log probabilities from rollout policy (e.g., vLLM BF16).
            Shape: (batch_size, seq_length). Used for KL computation.
        log_prob: Log probabilities from current training policy.
            Shape: (batch_size, seq_length)
        advantages: Advantage estimates for each token.
            Shape: (batch_size, seq_length)
        response_mask: Mask indicating valid tokens (1 for valid, 0 for padding).
            Shape: (batch_size, seq_length). Should already include rejection sampling.
        loss_agg_mode: Loss aggregation strategy (see agg_loss for details).
        config: Actor config (required for global_batch_info).
        rollout_is_weights: Pre-computed IS weights (π_current / π_rollout).
            Shape: (batch_size, seq_length). None to disable IS correction.

    Returns:
        Tuple of (loss, metrics):
            loss: Scalar policy gradient loss
            metrics: Dictionary with "actor/ppo_kl"

    Note:
        Unlike PPO (compute_policy_loss_vanilla), this function:
        - Does NOT use PPO clipping
        - Uses log π(a|s) directly (not ratio)
        - IS weights are applied as multiplicative factor
    """
    assert config is not None, "ActorConfig must be provided for REINFORCE loss"

    # Compute pure policy gradient loss with optional IS correction
    # Standard REINFORCE: L = -E[log π(a|s) * A]
    # With IS: L = -E[w * log π(a|s) * A] where w = π_current / π_rollout
    if rollout_is_weights is not None:
        # IS-corrected policy gradient: L = -E[stopgrad(w) · log π · A]
        pg_losses = -advantages * log_prob * rollout_is_weights
    else:
        # Standard REINFORCE: L = -E[log π · A]
        pg_losses = -advantages * log_prob

    # Aggregate loss
    pg_loss = agg_loss(
        loss_mat=pg_losses,
        loss_mask=response_mask,
        loss_agg_mode=loss_agg_mode,
        **config.global_batch_info,
    )

    # Compute KL divergence between current and rollout policy
    negative_approx_kl = log_prob - rollout_log_prob
    kl_divergence = verl_F.masked_mean(-negative_approx_kl, response_mask)

    pg_metrics = {
        "actor/ppo_kl": kl_divergence.detach().item(),
    }

    return pg_loss, pg_metrics


@register_policy_loss("bypass_mode")
def compute_policy_loss_bypass_mode(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """Bypass mode policy loss supporting both REINFORCE and PPO-clip.

    This function is the entry point for bypass mode, where old_log_prob = rollout_log_prob.
    It computes IS weights and rejection masks, then dispatches to either REINFORCE or
    PPO-clip loss based on the loss_type configuration.

    IMPORTANT - Bypass mode semantics:
        In bypass mode, the trainer sets old_log_prob = rollout_log_prob.
        This means:
        - For REINFORCE: We use IS weights w = π_current / π_rollout explicitly
        - For PPO-clip: The PPO ratio π_current / π_old = π_current / π_rollout
          already incorporates the IS correction through clipping, so we do NOT
          apply additional IS weights (would be double-counting)

    Loss types:
        - "ppo_clip" (default): PPO clipped objective (compute_policy_loss_vanilla)
            L = -E[min(r*A, clip(r)*A)] where r = π_current / π_rollout
            Note: IS weights are NOT applied (clipping handles the ratio)
        - "reinforce": REINFORCE-style policy gradient with IS correction
            L = -E[w * log π(a|s) * A] where w = π_current / π_rollout

    Args:
        old_log_prob: In bypass mode, this is actually rollout_log_prob.
            Shape: (batch_size, seq_length)
        log_prob: Current policy log probabilities.
            Shape: (batch_size, seq_length)
        advantages: Advantage estimates.
            Shape: (batch_size, seq_length)
        response_mask: Valid token mask (1=valid, 0=padding).
            Shape: (batch_size, seq_length)
        loss_agg_mode: Loss aggregation mode (passed to underlying loss function).
        config: Actor config containing rollout_correction settings in policy_loss.
        rollout_is_weights: Pre-computed IS weights (ignored, computed internally).

    Config options (in config.policy_loss.rollout_correction):
        loss_type: "ppo_clip" (default) or "reinforce"
        rollout_is: IS aggregation level ("token", "sequence", or None)
        rollout_is_threshold: Upper threshold for truncating IS weights (default: 2.0)
        rollout_rs: Rejection sampling level (see rollout_corr_helper for supported modes)
        rollout_rs_threshold: Threshold specification for rejection sampling
        rollout_is_batch_normalize: Whether to normalize IS weights to mean=1.0

    Returns:
        Tuple of (loss, metrics):
            loss: Scalar policy loss
            metrics: Dictionary with rollout correction metrics and actor/ppo_kl
    """
    from verl.trainer.ppo.rollout_corr_helper import compute_rollout_correction_and_rejection_mask

    assert config is not None, "config is required for bypass_mode loss"

    # Extract rollout_correction config from policy_loss
    rollout_corr_config = config.policy_loss.get("rollout_correction", None) if hasattr(config, "policy_loss") else None

    if rollout_corr_config is None:
        raise ValueError(
            "rollout_correction config not found in policy_loss. "
            "When using loss_mode='bypass_mode', ensure rollout_correction config is passed."
        )

    # Extract parameters
    loss_type = rollout_corr_config.get("loss_type", "ppo_clip")
    rollout_is = rollout_corr_config.get("rollout_is", None)
    rollout_is_threshold = rollout_corr_config.get("rollout_is_threshold", 2.0)
    rollout_is_batch_normalize = rollout_corr_config.get("rollout_is_batch_normalize", False)
    rollout_rs = rollout_corr_config.get("rollout_rs", None)
    rollout_rs_threshold = rollout_corr_config.get("rollout_rs_threshold", None)

    # In bypass mode: old_log_prob IS rollout_log_prob
    rollout_log_prob = old_log_prob

    # Compute IS weights and rejection mask
    # Note: For PPO-clip, we still compute IS weights for metrics, but don't apply them
    with torch.no_grad():
        rollout_is_weights_proto, modified_response_mask, rollout_metrics = (
            compute_rollout_correction_and_rejection_mask(
                old_log_prob=log_prob,  # Current policy (for IS ratio: π_current / π_rollout)
                rollout_log_prob=rollout_log_prob,  # Rollout policy
                response_mask=response_mask,
                rollout_is=rollout_is,
                rollout_is_threshold=rollout_is_threshold,
                rollout_is_batch_normalize=rollout_is_batch_normalize,
                rollout_rs=rollout_rs,
                rollout_rs_threshold=rollout_rs_threshold,
            )
        )

    # Extract IS weights tensor (or None if disabled)
    computed_is_weights = rollout_is_weights_proto.batch["rollout_is_weights"] if rollout_is_weights_proto else None

    # Apply rejection mask (RS + veto)
    effective_mask = modified_response_mask

    # Dispatch to appropriate loss function based on loss_type
    if loss_type == "reinforce":
        # REINFORCE: Apply IS weights explicitly
        pg_loss, pg_metrics = compute_policy_loss_reinforce(
            rollout_log_prob=rollout_log_prob,
            log_prob=log_prob,
            advantages=advantages,
            response_mask=effective_mask,
            loss_agg_mode=loss_agg_mode,
            config=config,
            rollout_is_weights=computed_is_weights,
        )

    elif loss_type == "ppo_clip":
        # PPO-clip: The ratio π_current/π_old = π_current/π_rollout already handles IS
        # DO NOT apply IS weights - would be double-counting!
        # The clipping mechanism constrains the effective IS ratio
        pg_loss, pg_metrics = compute_policy_loss_vanilla(  # type: ignore[call-arg]
            old_log_prob=rollout_log_prob,  # = old_log_prob in bypass mode
            log_prob=log_prob,
            advantages=advantages,
            response_mask=effective_mask,
            loss_agg_mode=loss_agg_mode,
            config=config,
            rollout_is_weights=None,  # Explicitly None - no IS weights for PPO-clip
        )

    else:
        raise ValueError(f"Invalid loss_type: {loss_type}. Must be 'reinforce' or 'ppo_clip'.")

    # Merge rollout correction metrics
    pg_metrics.update(rollout_metrics)

    return pg_loss, pg_metrics


@register_policy_loss("seeupo_turn")
def compute_policy_loss_seeupo_turn(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L)  — not used; M is used instead
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    # SeeUPO-specific — injected via data.batch
    seeupo_seg_mask: torch.Tensor = None,   # (B, L) bool — tokens for this turn only
    seeupo_M: torch.Tensor = None,          # (B,)        — M_{t+1} effective advantage
) -> tuple[torch.Tensor, dict]:
    """
    PPO-clip loss for a single turn segment t, weighted by M_{t+1}.
    Called T times in reverse order by update_policy_seeupo.
    """
    assert seeupo_seg_mask is not None and seeupo_M is not None, (
        "seeupo_seg_mask and seeupo_M must be provided for seeupo_turn loss"
    )

    clip_ratio = config.clip_ratio

    seg_mask_f = seeupo_seg_mask.float()                          # (B, L)
    n_valid    = seg_mask_f.sum().clamp(min=1)

    log_ratio  = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)
    ratio      = torch.exp(log_ratio)                             # (B, L)

    # M_{t+1}: (B,) → (B, 1), detached — it's a weighting coefficient not a trainable quantity
    adv = seeupo_M.to(log_prob.device).detach().unsqueeze(1)     # (B, 1)

    pg_loss1 = -adv * ratio                                       # (B, L)
    pg_loss2 = -adv * torch.clamp(ratio, 1 - clip_ratio, 1 + clip_ratio)
    pg_loss  = torch.maximum(pg_loss1, pg_loss2) * seg_mask_f    # zero outside segment

    loss      = pg_loss.sum() / n_valid
    clip_frac = ((pg_loss2 > pg_loss1) * seg_mask_f).sum() / n_valid
    ppo_kl    = ((-log_ratio) * seg_mask_f).sum() / n_valid

    metrics = {
        "actor/pg_loss":     loss.detach().item(),
        "actor/pg_clipfrac": clip_frac.detach().item(),
        "actor/ppo_kl":      ppo_kl.detach().item(),
    }
    return loss, metrics


@register_adv_est(AdvantageEstimator.MAXRL)
def compute_maxrl_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,  # kept for API compat, unused
    config: Optional[AlgoConfig] = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Compute outcome-level MaxRL advantages.

    For rewards with a known lower bound, first convert rewards into a
    nonnegative utility:

        u(r) = max(r - reward_floor, 0).

    For wiki_final's -1/0/1 reward and reward_floor=-1, this maps
    -1 -> 0, 0 -> 1, and 1 -> 2. The resulting MaxRL objective is
    log E[u(r)], whose empirical policy-gradient coefficient is:

        A_i = (u_i - mean(u)) / mean(u).

    Args:
        token_level_rewards: shape (bs, response_length)
        response_mask:        shape (bs, response_length)
        index:                uid array for grouping samples by prompt
        epsilon:              small value to avoid division by zero
        norm_adv_by_std_in_grpo: unused, kept for interface compatibility
        config:               algorithm config

    Returns:
        advantages: (bs, response_length)
        returns:    (bs, response_length)  [same as advantages]
    """
    # Outcome reward: scalar per response

    scores = token_level_rewards.sum(dim=-1)  # (bs,)
    reward_floor = _resolve_maxrl_reward_floor(scores, config)

    # Group by prompt uid
    id2indices: dict = defaultdict(list)

    with torch.no_grad():
        utilities = (scores - reward_floor).clamp(min=0.0)
        bsz = scores.shape[0]
        for i in range(bsz):
            id2indices[index[i]].append(i)

        advantages = torch.zeros_like(scores)

        for idxs in id2indices.values():
            group_utility = torch.stack([utilities[i] for i in idxs])
            utility_mean = group_utility.mean()

            if utility_mean <= epsilon:
                # No positive-utility samples in this group.
                continue

            group_adv = (group_utility - utility_mean) / (utility_mean + epsilon)

            for k, i in enumerate(idxs):
                advantages[i] = group_adv[k]

        # Broadcast scalar advantage to token level
        advantages = advantages.unsqueeze(-1) * response_mask  # (bs, response_length)

    return advantages, advantages


@register_adv_est(AdvantageEstimator.MSE_GATE)
def compute_mse_gate_outcome_advantage(
    token_level_rewards: torch.Tensor,
    response_mask: torch.Tensor,
    index: np.ndarray,
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config: Optional[AlgoConfig] = None,
    old_log_probs: Optional[torch.Tensor] = None,
    sum_pi_squared: Optional[torch.Tensor] = None,
    return_metrics: bool = False,
    **kwargs,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    Choose MaxRL or GRPO per prompt group using a finite-sample MSE proxy.

    For outcome rewards with a known lower bound, define a nonnegative utility

        u(r) = max(r - reward_floor, 0).

    MaxRL then optimizes log E[u(r)]. For wiki_final's -1/0/1 reward and
    reward_floor=-1, this maps -1 -> 0, 0 -> 1, 1 -> 2, so malformed outputs
    are no longer treated the same as well-formed wrong answers.

    When u(r) is affine in r, MaxRL and GRPO share the same centered direction
    inside a prompt group:

        A_maxrl = (u - mean(u)) / mean(u)
        A_grpo  = (u - mean(u)) / std(u)

    This is equivalent to GRPO on raw rewards when u is a constant shift of r.
    GRPO is therefore a shrinked/scaled MaxRL update. This estimator uses a
    detached per-response score-norm proxy to estimate whether that shrinkage
    reduces mean-squared error for the current group:

        MSE(MaxRL) ~= var(mean(MaxRL contribution))
        MSE(GRPO)  ~= (1 - alpha)^2 * ||g||^2 + alpha^2 * var

    where alpha maps MaxRL to GRPO. By default the two advantages are blended
    with inverse-MSE weights; set ``algorithm.mse_gate_soft=False`` to recover
    a hard lower-MSE gate. Set ``algorithm.mse_gate_maxrl_reward_floor`` to the
    task reward floor; the default is -1.0 for wiki_final.
    """
    with torch.no_grad():
        scores = token_level_rewards.sum(dim=-1)
        device = scores.device
        dtype = scores.dtype
        soft_gate = True
        reward_floor = _resolve_maxrl_reward_floor(scores, config)
        if config is not None:
            soft_gate = bool(config.get("mse_gate_soft", soft_gate))
        utilities = (scores - reward_floor).clamp(min=0.0).to(dtype=dtype)

        mask_f = response_mask.to(device=device, dtype=torch.float32)
        if old_log_probs is None:
            score_norm = mask_f.sum(dim=-1).clamp(min=epsilon)
        else:
            old_log_probs = old_log_probs.to(device=device, dtype=torch.float32)
            pi_taken = old_log_probs.exp().clamp(min=0.0, max=1.0)
            if sum_pi_squared is not None:
                sum_pi_squared = sum_pi_squared.to(device=device, dtype=torch.float32)
                token_score_norm = 1.0 - 2.0 * pi_taken + sum_pi_squared
            else:
                token_score_norm = (1.0 - pi_taken).square()
            token_score_norm = torch.where(
                response_mask.bool(),
                token_score_norm.clamp(min=epsilon),
                torch.zeros_like(token_score_norm),
            )
            score_norm = token_score_norm.sum(dim=-1).clamp(min=epsilon)

        id2indices: dict = defaultdict(list)
        bsz = scores.shape[0]
        for i in range(bsz):
            id2indices[index[i]].append(i)

        advantages = torch.zeros_like(scores)
        num_groups = 0
        grpo_groups = 0
        maxrl_groups = 0
        zero_groups = 0
        grpo_samples = 0
        maxrl_samples = 0
        zero_samples = 0
        utility_means = []
        alphas = []
        mse_maxrl_values = []
        mse_grpo_values = []
        var_values = []
        signal_values = []
        grpo_weights = []

        for idxs in id2indices.values():
            num_groups += 1
            if len(idxs) < 2:
                zero_groups += 1
                zero_samples += len(idxs)
                continue

            group_utility = torch.stack([utilities[i] for i in idxs])
            group_norm = torch.stack([score_norm[i] for i in idxs]).to(dtype=torch.float32)
            n = group_utility.numel()
            utility_mean = group_utility.mean()
            utility_centered = group_utility - utility_mean
            utility_std = group_utility.std(unbiased=True)

            if utility_mean <= epsilon or utility_std <= epsilon:
                zero_groups += 1
                zero_samples += n
                continue

            maxrl_adv = utility_centered / (utility_mean + epsilon)
            if norm_adv_by_std_in_grpo:
                # If reward_floor is a true lower bound, utility is just a
                # constant shift of reward; centered utility is centered reward.
                grpo_adv = utility_centered / (utility_std + epsilon)
                alpha = utility_mean / (utility_std + epsilon)
            else:
                grpo_adv = utility_centered
                alpha = utility_mean

            # Scalar proxy for per-response gradient contribution magnitude.
            # sqrt(score_norm) plays the role of ||score_i||.
            maxrl_contrib = maxrl_adv.to(torch.float32) * group_norm.sqrt()
            mean_contrib = maxrl_contrib.mean()
            if n > 1:
                var_mean = ((maxrl_contrib - mean_contrib).square().sum()) / (n * (n - 1))
            else:
                var_mean = torch.zeros((), device=device, dtype=torch.float32)
            signal_sq = (mean_contrib.square() - var_mean).clamp(min=0.0)

            mse_maxrl = var_mean
            mse_grpo = (1.0 - alpha).square() * signal_sq + alpha.square() * var_mean
            if soft_gate:
                grpo_weight = (mse_maxrl + epsilon) / (mse_grpo + mse_maxrl + 2.0 * epsilon)
                group_adv = grpo_weight * grpo_adv + (1.0 - grpo_weight) * maxrl_adv
                use_grpo = grpo_weight >= 0.5
            else:
                use_grpo = mse_grpo < mse_maxrl
                grpo_weight = torch.where(
                    use_grpo,
                    torch.ones((), device=device, dtype=torch.float32),
                    torch.zeros((), device=device, dtype=torch.float32),
                )
                group_adv = torch.where(use_grpo, grpo_adv, maxrl_adv)

            if bool(use_grpo.detach().item()):
                grpo_groups += 1
                grpo_samples += n
            else:
                maxrl_groups += 1
                maxrl_samples += n

            utility_means.append(utility_mean.detach().to(torch.float32))
            alphas.append(alpha.detach().to(torch.float32))
            mse_maxrl_values.append(mse_maxrl.detach().to(torch.float32))
            mse_grpo_values.append(mse_grpo.detach().to(torch.float32))
            var_values.append(var_mean.detach().to(torch.float32))
            signal_values.append(signal_sq.detach().to(torch.float32))
            grpo_weights.append(grpo_weight.detach().to(torch.float32))

            for local_idx, batch_idx in enumerate(idxs):
                advantages[batch_idx] = group_adv[local_idx].to(dtype=dtype)

        advantages = advantages.unsqueeze(-1) * response_mask

    if not return_metrics:
        return advantages, advantages

    def _mean_or_zero(values: list[torch.Tensor]) -> float:
        if not values:
            return 0.0
        return float(torch.stack(values).mean().detach().cpu().item())

    group_denom = max(num_groups, 1)
    sample_denom = max(grpo_samples + maxrl_samples + zero_samples, 1)
    metrics = {
        "mse_gate/groups": float(num_groups),
        "mse_gate/grpo_groups": float(grpo_groups),
        "mse_gate/maxrl_groups": float(maxrl_groups),
        "mse_gate/zero_groups": float(zero_groups),
        "mse_gate/grpo_group_ratio": float(grpo_groups / group_denom),
        "mse_gate/maxrl_group_ratio": float(maxrl_groups / group_denom),
        "mse_gate/zero_group_ratio": float(zero_groups / group_denom),
        "mse_gate/grpo_sample_ratio": float(grpo_samples / sample_denom),
        "mse_gate/maxrl_sample_ratio": float(maxrl_samples / sample_denom),
        "mse_gate/zero_sample_ratio": float(zero_samples / sample_denom),
        "mse_gate/utility_mean": _mean_or_zero(utility_means),
        "mse_gate/alpha_mean": _mean_or_zero(alphas),
        "mse_gate/mse_maxrl_mean": _mean_or_zero(mse_maxrl_values),
        "mse_gate/mse_grpo_mean": _mean_or_zero(mse_grpo_values),
        "mse_gate/var_mean": _mean_or_zero(var_values),
        "mse_gate/signal_sq_mean": _mean_or_zero(signal_values),
        "mse_gate/grpo_weight_mean": _mean_or_zero(grpo_weights),
        "mse_gate/soft_enabled": float(soft_gate),
        "mse_gate/maxrl_reward_floor": float(reward_floor.detach().cpu().item()),
    }

    return advantages, advantages, metrics

@register_policy_loss("empo")
def compute_policy_loss_empo(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L)  — GRPO scalar broadcast
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    turn_index: torch.Tensor = None, # (B, L)  — 0-based turn id, -1 = non-response
) -> tuple[torch.Tensor, dict]:
    """
    EMPO: single-pass multi-turn policy gradient with per-turn IS weights.

    For each token at turn k, the effective weight is:
        w_k = Π_{j=0}^{k}  exp( Σ_{tokens in turn j} (log π_θ - log π_old) )

    This is the sequence-level IS ratio accumulated over all turns up to k.
    One backward pass — no sequential optimizer steps needed.
    """
    if turn_index is None:
        # No turn structure — fall back to vanilla PPO
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio

    # Per-token log ratio and ratio
    log_ratio = torch.clamp(log_prob - old_log_prob, min=-20.0, max=20.0)  # (B, L)

    # ── step 1: compute sequence-level IS ratio per turn ─────────────────
    # seg_log_ratio[b, k] = Σ_{t in turn k} (log π_θ[b,t] - log π_old[b,t])
    B, L = log_prob.shape
    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1

    seg_log_ratio = torch.zeros(B, num_turns, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)                          # (B, L) bool
        seg_log_ratio[:, k] = (log_ratio * mask_k.float()).sum(dim=-1)  # (B,)

    # ── step 2: cumulative IS weight per turn ─────────────────────────────
    # cumul_log_ratio[b, k] = Σ_{j=0}^{k} seg_log_ratio[b, j]
    # w_k[b] = exp(cumul_log_ratio[b, k])
    cumul_log_ratio = torch.cumsum(seg_log_ratio, dim=1)    # (B, num_turns)
    # Clamp for numerical stability before exp
    cumul_log_ratio = torch.clamp(cumul_log_ratio, min=-20.0, max=20.0)
    turn_is_weights = torch.exp(cumul_log_ratio)            # (B, num_turns)

    # ── step 3: broadcast IS weight to each token based on its turn ───────
    # token_is_weight[b, t] = w_{turn_index[b,t]}[b]
    token_is_weight = torch.zeros(B, L, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)                          # (B, L) bool
        # turn_is_weights[:, k] shape (B,) → broadcast to masked positions
        token_is_weight[mask_k] = turn_is_weights[:, k].unsqueeze(1).expand(B, L)[mask_k]

    # ── step 4: recover per-sample scalar advantage ───────────────────────
    # GRPO broadcast: same value at every response token — recover scalar
    resp_counts = response_mask.float().sum(-1).clamp(min=1)             # (B,)
    traj_adv = (advantages * response_mask.float()).sum(-1) / resp_counts # (B,)  scalar per traj

    # Broadcast scalar advantage to all response tokens
    adv_token = traj_adv.unsqueeze(1).expand(B, L)                       # (B, L)

    # ── step 5: PPO-clip loss weighted by IS ──────────────────────────────
    # Standard per-token ratio (same as vanilla PPO)
    ratio = torch.exp(log_ratio)                                          # (B, L)

    pg_loss1 = -adv_token * ratio
    pg_loss2 = -adv_token * torch.clamp(ratio, 1 - clip_ratio, 1 + clip_ratio)
    pg_loss_clipped = torch.maximum(pg_loss1, pg_loss2)                  # (B, L)

    # Apply IS weight — tokens at later turns get larger IS corrections
    pg_loss_weighted = pg_loss_clipped * token_is_weight * response_mask.float()

    # Aggregate
    n_valid = response_mask.float().sum().clamp(min=1)
    pg_loss = pg_loss_weighted.sum() / n_valid

    # ── metrics ───────────────────────────────────────────────────────────
    clip_frac = ((pg_loss2 > pg_loss1) * response_mask.float()).sum() / n_valid
    ppo_kl    = ((-log_ratio) * response_mask.float()).sum() / n_valid
    mean_is   = (token_is_weight * response_mask.float()).sum() / n_valid

    metrics = {
        "actor/pg_clipfrac":    clip_frac.detach().item(),
        "actor/ppo_kl":         ppo_kl.detach().item(),
        "actor/empo_mean_is":   mean_is.detach().item(),   # monitor IS weight magnitude
    }
    return pg_loss, metrics



# ========= utils =========

def _ranknorm_masked(x: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    out = torch.zeros_like(x)  # dtype == x.dtype (可能是 bf16)
    valid = mask > 0
    n = int(valid.sum().item())
    if n <= 1:
        return out

    flat_idx = torch.nonzero(valid.reshape(-1), as_tuple=True)[0]
    flat_x = x.reshape(-1)[flat_idx]

    order = torch.argsort(flat_x)  # ascending

    # ranks dtype 和 out 一致，避免 index_put dtype mismatch
    ranks = torch.empty_like(order, dtype=out.dtype)
    ranks[order] = torch.arange(n, device=x.device, dtype=out.dtype)

    denom = torch.as_tensor(max(n - 1, 1), device=x.device, dtype=out.dtype)

    out_flat = out.reshape(-1)
    out_flat[flat_idx] = ranks / denom
    return out

def _compute_cov_token(
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    mask: torch.Tensor,
) -> torch.Tensor:
    """
    Cov(y_i) = (logp_i - mean_logp) * (A_i - mean_A) over valid tokens in the batch.
    Invalid -> 0.
    """
    valid = mask > 0
    denom = valid.sum().clamp_min(1)
    mean_logp = (log_prob * valid).sum() / denom
    mean_adv = (advantages * valid).sum() / denom
    cov = (log_prob - mean_logp) * (advantages - mean_adv)
    return cov * valid


def _sapo_gates(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    tau_pos: float,
    tau_neg: float,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Return (gates, ratio, log_ratio).
    """
    tau_pos_t = torch.as_tensor(tau_pos, dtype=advantages.dtype, device=advantages.device)
    tau_neg_t = torch.as_tensor(tau_neg, dtype=advantages.dtype, device=advantages.device)

    log_ratio = log_prob - old_log_prob
    log_ratio = torch.clamp(log_ratio, min=-20.0, max=20.0)
    ratio = torch.exp(log_ratio)

    taus = torch.where(advantages > 0, tau_pos_t, tau_neg_t)
    gates = torch.sigmoid(taus * (ratio - 1.0)) * (4.0 / taus)
    return gates, ratio, log_ratio


def _dapo_grpo_hard_gate_from_ratio(
    ratio: torch.Tensor,
    clip_ratio_low: float,
    clip_ratio_high: float,
) -> torch.Tensor:
    """
    DAPO-style asymmetric hard clip band indicator:
      keep gradient only if ratio in [1-clip_low, 1+clip_high].
    """
    lo = torch.as_tensor(1.0 - clip_ratio_low, device=ratio.device, dtype=ratio.dtype)
    hi = torch.as_tensor(1.0 + clip_ratio_high, device=ratio.device, dtype=ratio.dtype)
    return ((ratio >= lo) & (ratio <= hi)).to(ratio.dtype)


def _sigmoid01(x: torch.Tensor, eps: float = 1e-6) -> torch.Tensor:
    """
    Map [0,1] -> (0,1) with a default "neutral" center at 0.5 and slope ~1.
    This is a *parameter-light* squashing; you can change slope if you want harder gating.
    """
    # center to [-0.5, 0.5], then apply sigmoid
    return torch.sigmoid((x - 0.5) / (0.25 + eps))  # slope ~4 near center


# ========= main method =========

@register_policy_loss("async_sapo_grpo_pack")
def compute_policy_loss_async_sapo_grpo_pack(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional["ActorConfig"] = None,
    rollout_is_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    Mixture gate:
        weight = (1-pack) * w_sapo + pack * w_grpo_hard
        loss_token = - weight * advantages

    pack uses:
        offpol_rank = ranknorm(|log_ratio|)
        cov_rank   = ranknorm(relu(Cov_token))
        Cov_token  = (logp - mean_logp)*(A - mean_A)

    then (stronger penalty for high pack):
        pack = sigmoid(offpol_rank) * sigmoid(cov_rank)
      or alternatively:
        pack = sigmoid(offpol_rank * cov_rank)
    Controlled by config.pack_sigmoid_mode.
    """
    assert config is not None
    assert isinstance(config, ActorConfig)

    valid = response_mask > 0

    # 1) SAPO weights + ratio
    w_sapo, ratio, log_ratio = _sapo_gates(
        old_log_prob=old_log_prob,
        log_prob=log_prob,
        advantages=advantages,
        tau_pos=config.tau_pos,
        tau_neg=config.tau_neg,
    )

    # 2) DAPO/GRPO asymmetric clip hard gate
    clip_ratio_low = getattr(config, "clip_ratio_low", None)
    clip_ratio_high = getattr(config, "clip_ratio_high", None)

    # Provide sane defaults if missing
    if clip_ratio_low is None:
        clip_ratio_low = 0.20
    if clip_ratio_high is None:
        clip_ratio_high = 0.28

    w_grpo_hard = _dapo_grpo_hard_gate_from_ratio(
        ratio=ratio,
        clip_ratio_low=float(clip_ratio_low),
        clip_ratio_high=float(clip_ratio_high),
    )

    # 3) compute off-policy rank
    offpol_strength = log_ratio.abs() * valid
    offpol_rank = _ranknorm_masked(offpol_strength, response_mask)  # [0,1], invalid=0

    # 4) compute cov rank (token-wise covariance)
    cov_token = _compute_cov_token(log_prob=log_prob, advantages=advantages, mask=response_mask)
    cov_pos = torch.relu(cov_token) * valid
    cov_rank = _ranknorm_masked(cov_pos, response_mask)  # [0,1]

    # 5) pack squashing (stronger punishment at high values)
    # pack_mode = getattr(config, "pack_sigmoid_mode", "prod_sigmoid")  # "prod_sigmoid" | "sigmoid_prod" | "none"

    # if pack_mode == "prod_sigmoid":
    #     pack = _sigmoid01(offpol_rank) * _sigmoid01(cov_rank)
    # elif pack_mode == "sigmoid_prod":
    #     pack = _sigmoid01(offpol_rank * cov_rank)
    # elif pack_mode == "none":
    #     pack = offpol_rank * cov_rank
    # else:
    #     raise ValueError(f"Unknown pack_sigmoid_mode: {pack_mode}")

    pack = _sigmoid01(offpol_rank) * _sigmoid01(cov_rank)
    pack = pack * valid

    # 6) mixture weight
    weight = (1.0 - pack) * w_sapo + pack * w_grpo_hard

    # 7) loss
    pg_losses = -weight * advantages
    pg_losses = pg_losses * valid

    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    pg_loss = agg_loss(
        loss_mat=pg_losses, loss_mask=response_mask, loss_agg_mode=loss_agg_mode, **get_agg_loss_kwargs(config.global_batch_info),   # ← was **config.global_batch_info

    )

    # 8) metrics
    ppo_kl = verl_F.masked_mean(-(log_prob - old_log_prob), response_mask)
    # Avoid quantile on empty
    pack_p95 = torch.quantile(pack[valid], 0.95).detach().item() if int(valid.sum()) > 0 else 0.0
    metrics = {
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pack_mean": verl_F.masked_mean(pack, response_mask).detach().item(),
        "actor/pack_p95": pack_p95,
        "actor/offpol_rank_mean": verl_F.masked_mean(offpol_rank, response_mask).detach().item(),
        "actor/cov_rank_mean": verl_F.masked_mean(cov_rank, response_mask).detach().item(),
        "actor/w_grpo_hard_frac": verl_F.masked_mean(w_grpo_hard, response_mask).detach().item(),
        "actor/clip_low": float(clip_ratio_low),
        "actor/clip_high": float(clip_ratio_high),
    }
    return pg_loss, metrics


@register_adv_est(AdvantageEstimator.GIGPO)
def compute_gigpo_advantage(
    token_level_rewards: torch.Tensor,   # (B, response_length)
    response_mask: torch.Tensor,         # (B, response_length)
    turn_index: torch.Tensor,            # (B, response_length) — 0-based, -1=non-response
    index: np.ndarray,                   # (B,) — uid grouping key (same prompt = same uid)
    omega: float = 1.0,                  # weight for step advantage
    gamma: float = 1.0,                  # discount factor for step returns
    epsilon: float = 1e-6,
    norm_adv_by_std: bool = True,        # False → Leave-One-Out (Fnorm=1)
    config: Optional[AlgoConfig] = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    GiGPO: two-level advantage = episode-level (GRPO) + omega * step-level (turn-grouped GRPO).

    Episode-level: same as GRPO — normalize total trajectory reward across group.
    Step-level: for each turn k, normalize the discounted-return-from-turn-k
                across all samples in the same uid-group that have turn k.

    Returns advantages (B, response_length) to drop into existing batch["advantages"].
    """

    B, L = token_level_rewards.shape

    # ── 1. total trajectory reward per sample ─────────────────────────────
    total_reward = token_level_rewards.sum(dim=-1)   # (B,)

    # ── 2. episode-level advantage (standard GRPO) ────────────────────────
    id2scores: dict = defaultdict(list)
    for i in range(B):
        id2scores[index[i]].append((i, total_reward[i]))

    episode_adv = torch.zeros(B, dtype=torch.float32)
    with torch.no_grad():
        for uid, pairs in id2scores.items():
            idxs   = [p[0] for p in pairs]
            scores = torch.stack([p[1] for p in pairs])
            mean_s = scores.mean()
            if norm_adv_by_std and len(scores) > 1:
                std_s = scores.std().clamp(min=epsilon)
                normed = (scores - mean_s) / std_s
            else:
                normed = scores - mean_s   # Leave-One-Out style (Fnorm=1)
            for rank, idx in enumerate(idxs):
                episode_adv[idx] = normed[rank]

    # ── 3. discounted return from each turn k onward ──────────────────────
    # R_t^(i) = Σ_{k=t}^{T} γ^{k-t} r_k^(i)
    # We use turn-level rewards: r_k^(i) = sum of token_level_rewards in turn k for sample i.
    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1

    # turn_reward[i, k] = total reward earned during turn k of sample i
    turn_reward = torch.zeros(B, num_turns, dtype=torch.float32)
    for k in range(num_turns):
        mask_k = (turn_index == k)                              # (B, L) bool
        turn_reward[:, k] = (token_level_rewards * mask_k.float()).sum(dim=-1)

    # discounted_return_from[i, k] = Σ_{j=k}^{num_turns-1} γ^{j-k} * turn_reward[i,j]
    discounted_return_from = torch.zeros(B, num_turns, dtype=torch.float32)
    running = torch.zeros(B, dtype=torch.float32)
    for k in range(num_turns - 1, -1, -1):
        running = turn_reward[:, k] + gamma * running
        discounted_return_from[:, k] = running

    # ── 4. step-level advantage per turn ─────────────────────────────────
    # For each (uid_group, turn_k), normalize discounted_return_from[:, k]
    # across all samples in that uid_group that actually have turn k tokens.
    step_adv = torch.zeros(B, num_turns, dtype=torch.float32)

    with torch.no_grad():
        for k in range(num_turns):
            # Which samples actually have tokens at turn k?
            has_turn_k = (turn_index == k).any(dim=-1)          # (B,) bool

            # Group by uid among samples that have turn k
            group_map: dict = defaultdict(list)
            for i in range(B):
                if has_turn_k[i]:
                    group_map[index[i]].append(i)

            for uid, idxs in group_map.items():
                if len(idxs) < 2:
                    # Only one sample at this (uid, turn) — no relative comparison possible
                    # Set to 0 (no step signal) rather than arbitrary normalization
                    step_adv[idxs[0], k] = 0.0
                    continue
                returns_k = torch.stack([discounted_return_from[i, k] for i in idxs])
                mean_r = returns_k.mean()
                if norm_adv_by_std:
                    std_r = returns_k.std().clamp(min=epsilon)
                    normed = (returns_k - mean_r) / std_r
                else:
                    normed = returns_k - mean_r
                for rank, idx in enumerate(idxs):
                    step_adv[idx, k] = normed[rank]

    # ── 5. combine: A(a_t^(i)) = A_E(τ_i) + ω * A_S(a_t^(i)) ────────────
    # Broadcast episode_adv to all tokens: (B,) → (B, L)
    episode_adv_token = episode_adv.unsqueeze(1).expand(B, L)   # (B, L)

    # Broadcast step_adv to tokens by turn: (B, num_turns) → (B, L)
    step_adv_token = torch.zeros(B, L, dtype=torch.float32)
    for k in range(num_turns):
        mask_k = (turn_index == k)                              # (B, L) bool
        step_adv_token[mask_k] = (
            step_adv[:, k].unsqueeze(1).expand(B, L)[mask_k]
        )

    combined_adv = (episode_adv_token + omega * step_adv_token) * response_mask.float()

    return combined_adv, combined_adv   # returns same shape as GRPO: (advantages, returns)

@register_policy_loss("empo_new")
def compute_policy_loss_empo(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L) — GRPO scalar broadcast
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Single-pass multi-turn PG with per-turn IS weights.
    turn_index is read from config.global_batch_info injected by ppo_loss.
    """

    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio
    B, L = log_prob.shape
    log_ratio = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)   # (B, L)

    # Sequence-level IS ratio per turn: sum of token log-ratios within each segment
    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1
    seg_log_ratio = torch.zeros(B, num_turns, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        seg_log_ratio[:, k] = (log_ratio * mask_k.float()).sum(dim=-1)

    # Cumulative IS weight: w_k = prod_{j=0}^{k} IS_j
    # cumul_log_ratio = torch.cumsum(seg_log_ratio, dim=1).clamp(-20.0, 20.0)  # (B, num_turns)
    turn_is_weights = torch.exp(seg_log_ratio)                               # (B, num_turns)

    # Broadcast IS weight to each token by its turn
    token_is_weight = torch.ones(B, L, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        token_is_weight[mask_k] = turn_is_weights[:, k].unsqueeze(1).expand(B, L)[mask_k]

    # Recover scalar advantage per trajectory
    resp_counts = response_mask.float().sum(-1).clamp(min=1)
    traj_adv    = (advantages * response_mask.float()).sum(-1) / resp_counts  # (B,)
    adv_token   = traj_adv.unsqueeze(1).expand(B, L)                          # (B, L)

    ratio     = torch.exp(log_ratio)
    pg_loss1  = -adv_token * ratio
    pg_loss2  = -adv_token * torch.clamp(ratio, 1 - clip_ratio, 1 + clip_ratio)
    pg_loss   = torch.maximum(pg_loss1, pg_loss2) * token_is_weight * response_mask.float()

    n_valid  = response_mask.float().sum().clamp(min=1)
    loss     = pg_loss.sum() / n_valid

    clip_frac = ((pg_loss2 > pg_loss1) * response_mask.float()).sum() / n_valid
    ppo_kl    = ((-log_ratio) * response_mask.float()).sum() / n_valid
    mean_is   = (token_is_weight * response_mask.float()).sum() / n_valid

    metrics = {
        "actor/pg_clipfrac":   clip_frac.detach().item(),
        "actor/ppo_kl":        ppo_kl.detach().item(),
        "actor/empo_mean_is":  mean_is.detach().item(),
    }
    return loss, metrics

@register_policy_loss("empo_new_clip")
def compute_policy_loss_empo(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L) — GRPO scalar broadcast
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Single-pass multi-turn PG with per-turn IS weights.
    turn_index is read from config.global_batch_info injected by ppo_loss.
    """

    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio
    B, L = log_prob.shape
    log_ratio = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)   # (B, L)

    # Sequence-level IS ratio per turn: sum of token log-ratios within each segment
    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1
    seg_log_ratio = torch.zeros(B, num_turns, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        seg_log_ratio[:, k] = (log_ratio * mask_k.float()).sum(dim=-1)

    # Cumulative IS weight: w_k = prod_{j=0}^{k} IS_j
    # cumul_log_ratio = torch.cumsum(seg_log_ratio, dim=1).clamp(-20.0, 20.0)  # (B, num_turns)
    turn_is_weights = torch.exp(seg_log_ratio)                               # (B, num_turns)

    # Broadcast IS weight to each token by its turn
    token_is_weight = torch.ones(B, L, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        token_is_weight[mask_k] = turn_is_weights[:, k].unsqueeze(1).expand(B, L)[mask_k]

    # Recover scalar advantage per trajectory
    resp_counts = response_mask.float().sum(-1).clamp(min=1)
    traj_adv    = (advantages * response_mask.float()).sum(-1) / resp_counts  # (B,)
    adv_token   = traj_adv.unsqueeze(1).expand(B, L)                          # (B, L)

    ratio     = torch.exp(log_ratio)
    # pg_loss1  = -adv_token * ratio
    # pg_loss2  = -adv_token * torch.clamp(ratio, 1 - clip_ratio, 1 + clip_ratio)
    # pg_loss   = torch.maximum(pg_loss1, pg_loss2) * token_is_weight * response_mask.float()

    # Combined ratio: turn-level IS * token-level ratio
    combined_ratio = token_is_weight * ratio          # (B, L)
    pg_loss1 = -adv_token * combined_ratio
    pg_loss2 = -adv_token * torch.clamp(combined_ratio, 1 - clip_ratio, 1 + clip_ratio)
    pg_loss  = torch.maximum(pg_loss1, pg_loss2) * response_mask.float()

    n_valid  = response_mask.float().sum().clamp(min=1)
    loss     = pg_loss.sum() / n_valid

    clip_frac = ((pg_loss2 > pg_loss1) * response_mask.float()).sum() / n_valid
    ppo_kl    = ((-log_ratio) * response_mask.float()).sum() / n_valid
    mean_is   = (token_is_weight * response_mask.float()).sum() / n_valid

    metrics = {
        "actor/pg_clipfrac":   clip_frac.detach().item(),
        "actor/ppo_kl":        ppo_kl.detach().item(),
        "actor/empo_mean_is":  mean_is.detach().item(),
    }
    return loss, metrics


@register_policy_loss("seeupo_turn_new")
def compute_policy_loss_seeupo_turn(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Per-turn PPO-clip loss for SeeUPO sequential update.
    seeupo_seg_mask and seeupo_M are read from config.global_batch_info,
    injected there by train_mini_batch_seeupo before each engine.train_batch call.
    """
    seg_mask = config.global_batch_info.get("seeupo_seg_mask", None)
    M        = config.global_batch_info.get("seeupo_M", None)

    if seg_mask is None or M is None:
        # Fallback to vanilla if not injected
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio  = config.clip_ratio
    seg_mask_f  = seg_mask.to(log_prob.device).float()
    n_valid     = seg_mask_f.sum().clamp(min=1)

    log_ratio   = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)
    ratio       = torch.exp(log_ratio)

    adv = M.to(log_prob.device).detach().unsqueeze(1)               # (B, 1)

    pg_loss1 = -adv * ratio
    pg_loss2 = -adv * torch.clamp(ratio, 1 - clip_ratio, 1 + clip_ratio)
    pg_loss  = torch.maximum(pg_loss1, pg_loss2) * seg_mask_f

    loss      = pg_loss.sum() / n_valid
    clip_frac = ((pg_loss2 > pg_loss1) * seg_mask_f).sum() / n_valid
    ppo_kl    = ((-log_ratio) * seg_mask_f).sum() / n_valid

    metrics = {
        "actor/pg_clipfrac": clip_frac.detach().item(),
        "actor/ppo_kl":      ppo_kl.detach().item(),
    }
    return loss, metrics

@register_adv_est(AdvantageEstimator.IGPO)
def compute_igpo_advantage(
    token_level_rewards: torch.Tensor,   # (B, response_length)
    response_mask: torch.Tensor,         # (B, response_length)
    turn_index: torch.Tensor,            # (B, response_length) 0-based; -1=non-response
    index: np.ndarray,                   # (B,) uid key
    gamma: float = 1.0,
    epsilon: float = 1e-6,
    norm_adv_by_std: bool = True,
    config: Optional[Any] = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    IGPO advantage estimator (Eqs. 5-7).
 
    1. r_{i,k}   = sum of IG token rewards in turn k for sample i.
    2. A_{i,k}   = global z-normalise over all valid (i,k) pairs     [Eq. 6]
    3. Ã_{i,k}   = sum_{j>=k} gamma^{j-k} * A_{i,j}                 [Eq. 7]
    4. Broadcast Ã_{i,k} to every decision token in turn k.
 
    Set config.igpo_group_by_uid=True to switch to per-uid normalisation.
    """
    B, L   = token_level_rewards.shape
    device = token_level_rewards.device
 
    group_by_uid = False
    if config is not None:
        if hasattr(config, "igpo_group_by_uid"):
            group_by_uid = bool(config.igpo_group_by_uid)
        elif hasattr(config, "get"):
            group_by_uid = bool(config.get("igpo_group_by_uid", False))
 
    # 1. Number of turns
    valid = turn_index[turn_index >= 0]
    if valid.numel() == 0:
        zeros = torch.zeros_like(token_level_rewards)
        return zeros, zeros
    num_turns = int(valid.max().item()) + 1
 
    # 2. Per-turn scalar rewards
    has_turn    = torch.zeros(B, num_turns, dtype=torch.bool,    device=device)
    turn_reward = torch.zeros(B, num_turns, dtype=torch.float32, device=device)
    for k in range(num_turns):
        mask_k            = (turn_index == k)
        has_turn[:, k]    = mask_k.any(dim=-1)
        turn_reward[:, k] = (token_level_rewards * mask_k.float()).sum(dim=-1)
 
    # 3. Immediate normalised advantage A_{i,k}  [Eq. 6]
    imm_adv = torch.zeros(B, num_turns, dtype=torch.float32, device=device)
 
    if group_by_uid:
        with torch.no_grad():
            for k in range(num_turns):
                grp: dict = defaultdict(list)
                for i in range(B):
                    if has_turn[i, k]:
                        grp[index[i]].append(i)
                for uid, idxs in grp.items():
                    vals   = torch.stack([turn_reward[i, k] for i in idxs])
                    mean_v = vals.mean()
                    if norm_adv_by_std and len(idxs) > 1:
                        normed = (vals - mean_v) / vals.std().clamp(min=epsilon)
                    else:
                        normed = vals - mean_v
                    for rank, idx in enumerate(idxs):
                        imm_adv[idx, k] = normed[rank]
    else:
        # Global z-normalisation  [Eq. 5-6]
        all_r  = turn_reward[has_turn]
        r_mean = all_r.mean() if all_r.numel() >= 1 else torch.tensor(0.0, device=device)
        r_std  = (all_r.std().clamp(min=epsilon)
                  if (norm_adv_by_std and all_r.numel() >= 2)
                  else torch.tensor(1.0, device=device))
        imm_adv[has_turn] = (turn_reward[has_turn] - r_mean) / r_std
 
    # 4. Discounted cumulative advantage Ã_{i,k}  [Eq. 7]
    cum_adv = torch.zeros(B, num_turns, dtype=torch.float32, device=device)
    running = torch.zeros(B,            dtype=torch.float32, device=device)
    for k in range(num_turns - 1, -1, -1):
        running       = imm_adv[:, k] + gamma * running
        cum_adv[:, k] = running
 
    # 5. Broadcast to token dimension
    adv_token = torch.zeros(B, L, dtype=torch.float32, device=device)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        adv_token[mask_k] = cum_adv[:, k].unsqueeze(1).expand(B, L)[mask_k]
 
    adv_token = adv_token * response_mask.float()
    return adv_token, adv_token


# ---------------------------------------------------------------------------
# Core FutureKL computation
# ---------------------------------------------------------------------------
 
def compute_future_kl(
    delta_log_p: torch.Tensor,          # (B, T)  Δlog p per token
    response_mask: torch.Tensor,        # (B, T)  1 for valid response tokens
    importance_ratio: torch.Tensor,     # (B, T)  π_θ / π_θ_old
    dual_clip_c: float = 10.0,          # hard-clip threshold for stability mask
    gamma: float = 1.0,                 # discount factor γ ∈ (0,1]
) -> torch.Tensor:
    """
    Compute the soft-decayed, masked FutureKL for every token position.
 
    FutureKL_t = Σ_{k=t}^{T}  M_k · γ^{k-t} · Δlog p_k
 
    where M_k = 1[importance_ratio_k ≤ dual_clip_c]
 
    Returns:
        future_kl: (B, T)  — zero for masked-out (non-response) positions
    """
    B, T = delta_log_p.shape
 
    # Stability mask  M_k  — exclude tokens whose importance ratio exceeds c
    stability_mask = (importance_ratio <= dual_clip_c).float()   # (B, T)
 
    # Combine with response mask so padding never contributes
    effective_mask = stability_mask * response_mask              # (B, T)
 
    # Masked signal to accumulate
    masked_delta = effective_mask * delta_log_p                  # (B, T)
 
    # Build discount powers  [γ^0, γ^1, ..., γ^{T-1}]  on the correct device
    powers = torch.arange(T, device=delta_log_p.device, dtype=delta_log_p.dtype)
    discount = gamma ** powers                                   # (T,)
 
    # Multiply each token k by γ^k so that suffix-sum starting at t gives
    # Σ_{k≥t} γ^{k-t} · x_k  =  γ^{-t} · Σ_{k≥t} γ^k · x_k
    weighted = masked_delta * discount.unsqueeze(0)              # (B, T)
 
    # Reverse cumsum to get suffix sums, then un-weight by γ^{-t}
    suffix_sum = torch.flip(
        torch.cumsum(torch.flip(weighted, dims=[1]), dim=1),
        dims=[1],
    )                                                            # (B, T)
 
    future_kl = suffix_sum / discount.unsqueeze(0).clamp(min=1e-8)  # (B, T)
 
    # Zero out non-response positions (they should not affect the loss)
    future_kl = future_kl * response_mask
 
    return future_kl
 
 
# ---------------------------------------------------------------------------
# FIPO policy loss
# ---------------------------------------------------------------------------
 
@register_policy_loss("fipo")
def compute_policy_loss_fipo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config: Optional[ActorConfig] = None,
    rollout_is_weights: torch.Tensor | None = None,
    # ---- FIPO-specific hyper-parameters (can also live in config) ----------
    future_kl_gamma: float = 1.0,       # γ — discount / half-life via τ = -1/log2(γ)
    future_kl_eps_flow: float = 0.0,    # ε_flow  — lower clip for f_t
    future_kl_eps_high: float = 0.2,    # ε_high  — upper clip for f_t
    future_kl_large_ratio_threshold: float = 10.0,   # reset f_t=1 guard
) -> tuple[torch.Tensor, dict[str, Any]]:
    """
    FIPO policy loss (Eq. 8 in the paper).
 
    All PPO / dual-clip mechanics are identical to vanilla; the only change is
    that advantages are modulated by the Future-KL influence weight f_t before
    being used in the clipped objective.
 
    Extra args (beyond the vanilla signature):
        future_kl_gamma:              discount factor γ ∈ (0, 1].
        future_kl_eps_flow / _high:   clipping bounds for f_t.
        future_kl_large_ratio_threshold:
            For tokens with  Â < 0  AND ratio > this value, reset f_t = 1
            (prevents over-penalisation, per the last sentence of Sec 3.2.2).
    """
    assert config is not None
    assert not isinstance(config, AlgoConfig)
 
    # ------------------------------------------------------------------
    # 1. Read clipping hyper-parameters (same as vanilla)
    # ------------------------------------------------------------------
    clip_ratio   = config.clip_ratio
    clip_ratio_low  = config.clip_ratio_low  if config.clip_ratio_low  is not None else clip_ratio
    clip_ratio_high = config.clip_ratio_high if config.clip_ratio_high is not None else clip_ratio
    clip_ratio_c    = config.get("clip_ratio_c", 3.0)
 
    # Allow FIPO params to be overridden from config if present
    gamma     = config.get("future_kl_gamma",     future_kl_gamma)
    eps_flow  = config.get("future_kl_eps_flow",  future_kl_eps_flow)
    print("eps_flow: ", eps_flow)
    eps_high  = config.get("future_kl_eps_high",  future_kl_eps_high)
    large_thr = config.get("future_kl_large_ratio_threshold", future_kl_large_ratio_threshold)
 
    assert clip_ratio_c > 1.0, (
        "clip_ratio_c for dual-clip PPO must be > 1.0, "
        f"got {clip_ratio_c}."
    )
 
    # ------------------------------------------------------------------
    # 2. Per-token quantities
    # ------------------------------------------------------------------
    # Δlog p_t  (clamped for numerical stability, same as vanilla approx_kl)
    delta_log_p = torch.clamp(log_prob - old_log_prob, min=-20.0, max=20.0)  # (B, T)
    ratio       = torch.exp(delta_log_p)                                      # (B, T)
 
    # Standard PPO KL diagnostic
    ppo_kl = verl_F.masked_mean(-delta_log_p, response_mask)
 
    # ------------------------------------------------------------------
    # 3. FutureKL and influence weight  f_t  (Eq. 6–7)
    # ------------------------------------------------------------------
    future_kl = compute_future_kl(
        delta_log_p=delta_log_p,
        response_mask=response_mask,
        importance_ratio=ratio,
        dual_clip_c=clip_ratio_c,     # reuse the dual-clip threshold as c
        gamma=gamma,
    )                                                                          # (B, T)
 
    # f_t = clip( exp(FutureKL_t), 1−ε_flow, 1+ε_high )
    f_t = torch.clamp(
        torch.exp(future_kl),
        min=1.0 - eps_flow,
        max=1.0 + eps_high,
    )                                                                          # (B, T)
 
    # Safety reset: for negative-advantage tokens with very large ratio,
    # set f_t = 1 to avoid over-penalisation (last paragraph of Sec 3.2.2)
    large_ratio_mask = ratio > large_thr                                       # (B, T)
    neg_adv_mask     = advantages < 0                                          # (B, T)
    reset_mask       = large_ratio_mask & neg_adv_mask
    f_t = torch.where(reset_mask, torch.ones_like(f_t), f_t)
 
    # ------------------------------------------------------------------
    # 4. Modulated advantage  Ã_t = Â_t · f_t  (Eq. 7)
    # ------------------------------------------------------------------
    mod_advantages = advantages * f_t                                          # (B, T)
 
    # ------------------------------------------------------------------
    # 5. Clipped PPO objective with dual-clip (same structure as vanilla)
    #    but using mod_advantages in place of advantages
    # ------------------------------------------------------------------
    pg_losses1 = -mod_advantages * ratio
    pg_losses2 = -mod_advantages * torch.clamp(ratio, 1 - clip_ratio_low, 1 + clip_ratio_high)
    clip_pg_losses1 = torch.maximum(pg_losses1, pg_losses2)
 
    pg_clipfrac = verl_F.masked_mean(
        torch.gt(pg_losses2, pg_losses1).float(), response_mask
    )
 
    # Dual-clip: for negative advantages, also clamp at −clip_ratio_c * Ã
    pg_losses3      = -mod_advantages * clip_ratio_c
    clip_pg_losses2 = torch.min(pg_losses3, clip_pg_losses1)
    pg_clipfrac_lower = verl_F.masked_mean(
        torch.gt(clip_pg_losses1, pg_losses3) * (mod_advantages < 0).float(),
        response_mask,
    )
 
    pg_losses = torch.where(mod_advantages < 0, clip_pg_losses2, clip_pg_losses1)
 
    # ------------------------------------------------------------------
    # 6. Optional rollout IS correction (same as vanilla)
    # ------------------------------------------------------------------
    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights
 
    # ------------------------------------------------------------------
    # 7. Aggregate loss
    # ------------------------------------------------------------------
    pg_loss = agg_loss(
        loss_mat=pg_losses,
        loss_mask=response_mask,
        loss_agg_mode=loss_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )
 
    # ------------------------------------------------------------------
    # 8. Diagnostics
    # ------------------------------------------------------------------
    mean_future_kl   = verl_F.masked_mean(future_kl, response_mask)
    mean_f_t         = verl_F.masked_mean(f_t, response_mask)
    future_kl_pos_frac = verl_F.masked_mean(
        (future_kl > 0).float(), response_mask
    )
 
    pg_metrics = {
        "actor/pg_clipfrac":        pg_clipfrac.detach().item(),
        "actor/ppo_kl":             ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower":  pg_clipfrac_lower.detach().item(),
        # FIPO-specific diagnostics
        "actor/fipo_future_kl_mean":     mean_future_kl.detach().item(),
        "actor/fipo_f_t_mean":           mean_f_t.detach().item(),
        "actor/fipo_future_kl_pos_frac": future_kl_pos_frac.detach().item(),
    }
 
    return pg_loss, pg_metrics
 

@register_policy_loss("seeupo_turn_new_clip")   # corrected version
def compute_policy_loss_seeupo_turn_fixed(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Fixed SeeUPO per-turn loss.

    Separates IS weight (seeupo_IS) from trajectory advantage (seeupo_A) so that
    PPO clipping is applied to the combined product IS * ratio_token, which is the
    true deviation from the rollout policy. The advantage only scales the clipped term.

    Config keys read from global_batch_info:
        seeupo_seg_mask : (B, L) bool   — tokens belonging to current bucket
        seeupo_IS       : (B,)  float   — accumulated IS weight for this bucket
        seeupo_A        : (B,)  float   — trajectory advantage (never mutated)
    Falls back to vanilla if keys are absent.
    """
    seg_mask = config.global_batch_info.get("seeupo_seg_mask", None)
    IS_acc   = config.global_batch_info.get("seeupo_IS", None)
    A_traj   = config.global_batch_info.get("seeupo_A", None)

    if seg_mask is None or IS_acc is None or A_traj is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio
    seg_mask_f = seg_mask.to(log_prob.device).float()   # (B, L)
    n_valid    = seg_mask_f.sum().clamp(min=1)

    log_ratio = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)  # (B, L)
    ratio     = torch.exp(log_ratio)                                 # (B, L)

    IS = IS_acc.to(log_prob.device).detach().unsqueeze(1)   # (B, 1)
    A  = A_traj.to(log_prob.device).detach().unsqueeze(1)   # (B, 1)

    # Combined ratio: full policy deviation from rollout at this token
    combined_ratio = IS * ratio                                      # (B, L)

    # Clip the combined ratio — this is the correct trust-region bound
    clipped_combined = torch.clamp(combined_ratio, 1 - clip_ratio, 1 + clip_ratio)

    pg_loss1 = -A * combined_ratio
    pg_loss2 = -A * clipped_combined
    pg_loss  = torch.maximum(pg_loss1, pg_loss2) * seg_mask_f

    loss      = pg_loss.sum() / n_valid
    clip_frac = ((pg_loss2 > pg_loss1) * seg_mask_f).sum() / n_valid
    ppo_kl    = ((-log_ratio) * seg_mask_f).sum() / n_valid

    # Extra diagnostics useful for comparing against buggy baseline
    mean_IS   = (IS.expand_as(seg_mask_f) * seg_mask_f).sum() / n_valid
    mean_combined_ratio = (combined_ratio * seg_mask_f).sum() / n_valid

    metrics = {
        "actor/pg_clipfrac":            clip_frac.detach().item(),
        "actor/ppo_kl":                 ppo_kl.detach().item(),
        "actor/seeupo_mean_IS":         mean_IS.detach().item(),
        "actor/seeupo_mean_comb_ratio": mean_combined_ratio.detach().item(),
    }
    return loss, metrics


@register_adv_est("grpo_erl_split")
def compute_grpo_erl_split_advantage(
    token_level_rewards: torch.Tensor,   # (B, L) — same as GRPO input
    response_mask: torch.Tensor,         # (B, L)
    index: np.ndarray,                   # (B,) uid for grouping
    erl_split_idx: np.ndarray,           # (B,) int — index into response_mask dim; 0 = no split
    erl_first_reward: np.ndarray,        # (B,) float — r1 per sample
    erl_second_reward: np.ndarray,       # (B,) float — r2 per sample
    erl_reflected: np.ndarray,           # (B,) bool — whether reflection happened
    epsilon: float = 1e-6,
    norm_adv_by_std_in_grpo: bool = True,
    config=None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """
    GRPO advantage for ERL split trajectories.

    For samples where reflection happened (erl_reflected=True):
        - try1 tokens   (response_mask=1, position < erl_split_idx) get adv = normalize(r1)
        - reflect+try2  (response_mask=1, position >= erl_split_idx) get adv = normalize(r2)

    Normalization is done **per reward type** across the batch:
        - r1 values are normalized among all samples in the same uid group
        - r2 values are normalized among all samples in the same uid group
          (only samples where erl_reflected=True contribute to the r2 stats)

    For samples where erl_reflected=False, falls back to standard GRPO
    using token_level_rewards (same as compute_grpo_outcome_advantage).

    Returns:
        advantages: (B, L)
        returns:    (B, L)  — same as advantages (outcome supervision)
    """
    B, L = response_mask.shape
    device = token_level_rewards.device

    # ── Compute scalar scores for non-split samples (standard GRPO path) ──
    base_scores = token_level_rewards.sum(dim=-1)   # (B,)

    # ── Group-level normalization for r1 and r2 separately ──────────────
    id2r1, id2r2, id2base = defaultdict(list), defaultdict(list), defaultdict(list)

    with torch.no_grad():
        for i in range(B):
            uid = index[i]
            if erl_reflected[i]:
                id2r1[uid].append(float(erl_first_reward[i]))
                id2r2[uid].append(float(erl_second_reward[i]))
            else:
                id2base[uid].append(base_scores[i])

        def _mean_std(vals):
            if len(vals) == 0:
                return 0.0, 1.0
            if len(vals) == 1:
                return float(vals[0]), 1.0
            t = torch.tensor(vals, dtype=torch.float32)
            return t.mean().item(), t.std().item()

        # Build normalized r1/r2 lookup per uid
        uid2r1_stats   = {uid: _mean_std(v) for uid, v in id2r1.items()}
        uid2r2_stats   = {uid: _mean_std(v) for uid, v in id2r2.items()}
        uid2base_stats = {uid: _mean_std(v) for uid, v in id2base.items()}

        def _norm(val, mean, std):
            if norm_adv_by_std_in_grpo:
                return (val - mean) / (std + epsilon)
            return val - mean

        advantages = torch.zeros(B, L, dtype=token_level_rewards.dtype, device=device)

        for i in range(B):
            uid   = index[i]
            split = int(erl_split_idx[i])

            if erl_reflected[i] and split > 0:
                # ── Split path ──────────────────────────────────────
                r1_mean, r1_std = uid2r1_stats.get(uid, (0.0, 1.0))
                r2_mean, r2_std = uid2r2_stats.get(uid, (0.0, 1.0))

                r1_norm = _norm(float(erl_first_reward[i]),  r1_mean, r1_std)
                r2_norm = _norm(float(erl_second_reward[i]), r2_mean, r2_std)

                # try1 segment: mask=1 tokens before split
                try1_cols = response_mask[i, :split].nonzero(as_tuple=True)[0]
                advantages[i, try1_cols] = r1_norm

                # reflect+try2 segment: mask=1 tokens from split onward
                try2_cols = response_mask[i, split:].nonzero(as_tuple=True)[0] + split
                advantages[i, try2_cols] = r2_norm

            else:
                # ── Standard GRPO path (no reflection) ───────────────
                # Fall back to base_scores normalization
                if uid in uid2base_stats:
                    b_mean, b_std = uid2base_stats[uid]
                else:
                    # uid only has reflected samples; use global base stats or zero
                    b_mean, b_std = 0.0, 1.0

                norm_score = _norm(base_scores[i].item(), b_mean, b_std)
                resp_cols  = response_mask[i].nonzero(as_tuple=True)[0]
                advantages[i, resp_cols] = norm_score

    return advantages, advantages

@register_policy_loss("empo_new_clip_norm")
def compute_policy_loss_empo(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L) — GRPO scalar broadcast
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio
    B, L = log_prob.shape
    log_ratio = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)   # (B, L)

    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1
    seg_log_ratio = torch.zeros(B, num_turns, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        seg_log_ratio[:, k] = (log_ratio * mask_k.float()).sum(dim=-1)

    turn_is_weights = torch.exp(seg_log_ratio)   # (B, num_turns)

    # Recover scalar advantage per trajectory (same as before)
    resp_counts = response_mask.float().sum(-1).clamp(min=1)
    traj_adv    = (advantages * response_mask.float()).sum(-1) / resp_counts  # (B,)

    # Build per-turn loss matrix: (IS_k * adv) / len(turn_k)
    # We'll accumulate a (B, L) pg_loss matrix where each turn's tokens are
    # normalized by that turn's own token count — giving the desired sum of per-turn terms.
    pg_loss_mat = torch.zeros(B, L, device=log_prob.device, dtype=log_prob.dtype)

    ratio = torch.exp(log_ratio)   # (B, L)

    # Track metrics accumulators
    clip_num = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    kl_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    is_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    denom    = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)

    for k in range(num_turns):
        mask_k     = (turn_index == k) & (response_mask.bool())   # (B, L), valid tokens in turn k
        turn_len_k = mask_k.float().sum(-1).clamp(min=1)           # (B,) token count per seq for turn k
        is_k       = turn_is_weights[:, k]                         # (B,)
        adv_k      = traj_adv                                      # (B,) — shared advantage

        # Combined ratio for this turn: IS_k * token_ratio
        combined_k = is_k.unsqueeze(1) * ratio                     # (B, L)

        pg1_k = -adv_k.unsqueeze(1) * combined_k
        pg2_k = -adv_k.unsqueeze(1) * torch.clamp(combined_k, 1 - clip_ratio, 1 + clip_ratio)
        raw_k = torch.maximum(pg1_k, pg2_k)                        # (B, L)

        # Normalize each token's loss contribution by its turn's length
        # so the per-turn sum becomes: sum_t[ raw_k_t / len(turn_k) ]  = (IS_k * adv_k) / len(turn_k)
        norm_k = raw_k / turn_len_k.unsqueeze(1)                   # (B, L)
        pg_loss_mat = pg_loss_mat + norm_k * mask_k.float()

        # Metrics (token-level, for consistency)
        n_k = mask_k.float().sum().clamp(min=1)
        clip_num += ((pg2_k > pg1_k) * mask_k.float()).sum()
        kl_num   += ((-log_ratio) * mask_k.float()).sum()
        is_num   += (is_k.unsqueeze(1).expand(B, L) * mask_k.float()).sum()
        denom    += n_k

    # Now aggregate: each turn's tokens already carry the /len(turn_k) normalization,
    # so we want seq-mean over sequences (sum over tokens is correct per sequence).
    # Use seq-mean-token-sum so agg_loss just does: sum(seq_loss) / B
    loss = agg_loss(
        loss_mat=pg_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    metrics = {
        "actor/pg_clipfrac":  (clip_num / denom).item(),
        "actor/ppo_kl":       (kl_num   / denom).item(),
        "actor/empo_mean_is": (is_num   / denom).item(),
    }
    return loss, metrics


@register_policy_loss("empo_new_clip_norm_dual_clip")
def compute_policy_loss_empo(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L) — GRPO scalar broadcast
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio
    B, L = log_prob.shape
    log_ratio = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)   # (B, L)

    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1
    seg_log_ratio = torch.zeros(B, num_turns, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        seg_log_ratio[:, k] = (log_ratio * mask_k.float()).sum(dim=-1)

    turn_is_weights = torch.exp(seg_log_ratio)   # (B, num_turns)

    # Recover scalar advantage per trajectory (same as before)
    resp_counts = response_mask.float().sum(-1).clamp(min=1)
    traj_adv    = (advantages * response_mask.float()).sum(-1) / resp_counts  # (B,)

    # Build per-turn loss matrix: (IS_k * adv) / len(turn_k)
    # We'll accumulate a (B, L) pg_loss matrix where each turn's tokens are
    # normalized by that turn's own token count — giving the desired sum of per-turn terms.
    pg_loss_mat = torch.zeros(B, L, device=log_prob.device, dtype=log_prob.dtype)

    ratio = torch.exp(log_ratio)   # (B, L)

    # Track metrics accumulators
    clip_num = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    kl_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    is_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    denom    = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)

    for k in range(num_turns):
        mask_k     = (turn_index == k) & (response_mask.bool())   # (B, L), valid tokens in turn k
        turn_len_k = mask_k.float().sum(-1).clamp(min=1)           # (B,) token count per seq for turn k
        is_k       = turn_is_weights[:, k]                         # (B,)
        adv_k      = traj_adv                                      # (B,) — shared advantage

        # Combined ratio for this turn: IS_k * token_ratio
        combined_k = is_k.unsqueeze(1) * ratio                     # (B, L)

        pg1_k = -adv_k.unsqueeze(1) * combined_k
        pg2_k = -adv_k.unsqueeze(1) * torch.clamp(combined_k, 1 - clip_ratio, 1 + clip_ratio)
        raw_k = torch.maximum(pg1_k, pg2_k)                        # (B, L)


        # --- NEW: Add Dual-Clip for Negative Advantages ---
        clip_ratio_c = config.get("clip_ratio_c", 3.0) if config else 3.0
        pg3_k = -adv_k.unsqueeze(1) * clip_ratio_c
        clip_raw_k2 = torch.min(pg3_k, raw_k)
        
        # Apply the lower bound ONLY when advantage is negative
        raw_k = torch.where(adv_k.unsqueeze(1) < 0, clip_raw_k2, raw_k)
        # --------------------------------------------------

        # Normalize each token's loss contribution by its turn's length
        # so the per-turn sum becomes: sum_t[ raw_k_t / len(turn_k) ]  = (IS_k * adv_k) / len(turn_k)
        norm_k = raw_k / turn_len_k.unsqueeze(1)                   # (B, L)
        pg_loss_mat = pg_loss_mat + norm_k * mask_k.float()

        # Metrics (token-level, for consistency)
        n_k = mask_k.float().sum().clamp(min=1)
        clip_num += ((pg2_k > pg1_k) * mask_k.float()).sum()
        kl_num   += ((-log_ratio) * mask_k.float()).sum()
        is_num   += (is_k.unsqueeze(1).expand(B, L) * mask_k.float()).sum()
        denom    += n_k

    # Now aggregate: each turn's tokens already carry the /len(turn_k) normalization,
    # so we want seq-mean over sequences (sum over tokens is correct per sequence).
    # Use seq-mean-token-sum so agg_loss just does: sum(seq_loss) / B
    loss = agg_loss(
        loss_mat=pg_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    metrics = {
        "actor/pg_clipfrac":  (clip_num / denom).item(),
        "actor/ppo_kl":       (kl_num   / denom).item(),
        "actor/empo_mean_is": (is_num   / denom).item(),
    }
    return loss, metrics





@register_policy_loss("empo_new_clip_norm_dual_clip_correct")
def compute_policy_loss_empo(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L) — GRPO scalar broadcast
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio
    B, L = log_prob.shape
    log_ratio = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)   # (B, L)

    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1
    seg_log_ratio = torch.zeros(B, num_turns, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        seg_log_ratio[:, k] = (log_ratio * mask_k.float()).sum(dim=-1)

    turn_is_weights = torch.exp(seg_log_ratio)   # (B, num_turns)

    # Recover scalar advantage per trajectory (same as before)
    resp_counts = response_mask.float().sum(-1).clamp(min=1)
    traj_adv = (advantages * response_mask.float()).sum(-1) / resp_counts  # (B,)

    # Build per-turn loss matrix: each token in turn k gets the same turn-level
    # surrogate, then divided by turn length so the turn contributes once overall.
    pg_loss_mat = torch.zeros(B, L, device=log_prob.device, dtype=log_prob.dtype)

    # Track metrics accumulators
    clip_num = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    kl_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    is_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    denom    = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)

    for k in range(num_turns):
        mask_k     = (turn_index == k) & (response_mask.bool())   # (B, L), valid tokens in turn k
        turn_len_k = mask_k.float().sum(-1).clamp(min=1)          # (B,) token count per seq for turn k
        is_k       = turn_is_weights[:, k]                        # (B,)
        adv_k      = traj_adv                                     # (B,) — shared advantage

        # FIX: do NOT multiply by per-token ratio again.
        combined_k = is_k.unsqueeze(1)                            # (B, 1), broadcast over tokens

        pg1_k = -adv_k.unsqueeze(1) * combined_k
        pg2_k = -adv_k.unsqueeze(1) * torch.clamp(combined_k, 1 - clip_ratio, 1 + clip_ratio)
        raw_k = torch.maximum(pg1_k, pg2_k)                       # (B, 1) broadcastable to (B, L)

        # Dual-clip for negative advantages
        clip_ratio_c = config.get("clip_ratio_c", 3.0) if config else 3.0
        pg3_k = -adv_k.unsqueeze(1) * clip_ratio_c
        clip_raw_k2 = torch.min(pg3_k, raw_k)

        # Apply the lower bound ONLY when advantage is negative
        raw_k = torch.where(adv_k.unsqueeze(1) < 0, clip_raw_k2, raw_k)

        # Normalize each token's loss contribution by its turn's length
        norm_k = raw_k / turn_len_k.unsqueeze(1)                  # (B, 1), broadcast over tokens
        pg_loss_mat = pg_loss_mat + norm_k * mask_k.float()

        # Metrics (token-level, for consistency with your original code)
        n_k = mask_k.float().sum().clamp(min=1)
        clip_num += ((pg2_k > pg1_k).float() * mask_k.float()).sum()
        kl_num   += ((-log_ratio) * mask_k.float()).sum()
        is_num   += (is_k.unsqueeze(1).expand(B, L) * mask_k.float()).sum()
        denom    += n_k

    loss = agg_loss(
        loss_mat=pg_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    metrics = {
        "actor/pg_clipfrac":  (clip_num / denom).item(),
        "actor/ppo_kl":       (kl_num   / denom).item(),
        "actor/empo_mean_is": (is_num   / denom).item(),
    }
    return loss, metrics



@register_policy_loss("empo_new_clip_norm_dual_clip_correct_token_mean")
def compute_policy_loss_empo(
    old_log_prob: torch.Tensor,      # (B, L)
    log_prob: torch.Tensor,          # (B, L)
    advantages: torch.Tensor,        # (B, L) — GRPO scalar broadcast
    response_mask: torch.Tensor,     # (B, L)
    loss_agg_mode: str = "token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask,
            loss_agg_mode, config, rollout_is_weights,
        )

    clip_ratio = config.clip_ratio
    B, L = log_prob.shape
    log_ratio = torch.clamp(log_prob - old_log_prob, -20.0, 20.0)   # (B, L)

    num_turns = int((turn_index[turn_index >= 0]).max().item()) + 1
    seg_log_ratio = torch.zeros(B, num_turns, device=log_prob.device, dtype=log_prob.dtype)
    for k in range(num_turns):
        mask_k = (turn_index == k)
        seg_log_ratio[:, k] = (log_ratio * mask_k.float()).sum(dim=-1)

    turn_is_weights = torch.exp(seg_log_ratio)   # (B, num_turns)

    # Recover scalar advantage per trajectory (same as before)
    resp_counts = response_mask.float().sum(-1).clamp(min=1)
    traj_adv = (advantages * response_mask.float()).sum(-1) / resp_counts  # (B,)

    # Build per-turn loss matrix: each token in turn k gets the same turn-level
    # surrogate, then divided by turn length so the turn contributes once overall.
    pg_loss_mat = torch.zeros(B, L, device=log_prob.device, dtype=log_prob.dtype)

    # Track metrics accumulators
    clip_num = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    kl_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    is_num   = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)
    denom    = torch.zeros(1, device=log_prob.device, dtype=log_prob.dtype)

    for k in range(num_turns):
        mask_k     = (turn_index == k) & (response_mask.bool())   # (B, L), valid tokens in turn k
        turn_len_k = mask_k.float().sum(-1).clamp(min=1)          # (B,) token count per seq for turn k
        is_k       = turn_is_weights[:, k]                        # (B,)
        adv_k      = traj_adv                                     # (B,) — shared advantage

        # FIX: do NOT multiply by per-token ratio again.
        combined_k = is_k.unsqueeze(1)                            # (B, 1), broadcast over tokens

        pg1_k = -adv_k.unsqueeze(1) * combined_k
        pg2_k = -adv_k.unsqueeze(1) * torch.clamp(combined_k, 1 - clip_ratio, 1 + clip_ratio)
        raw_k = torch.maximum(pg1_k, pg2_k)                       # (B, 1) broadcastable to (B, L)

        # Dual-clip for negative advantages
        clip_ratio_c = config.get("clip_ratio_c", 3.0) if config else 3.0
        pg3_k = -adv_k.unsqueeze(1) * clip_ratio_c
        clip_raw_k2 = torch.min(pg3_k, raw_k)

        # Apply the lower bound ONLY when advantage is negative
        raw_k = torch.where(adv_k.unsqueeze(1) < 0, clip_raw_k2, raw_k)

        # Normalize each token's loss contribution by its turn's length
        norm_k = raw_k / turn_len_k.unsqueeze(1)                  # (B, 1), broadcast over tokens
        pg_loss_mat = pg_loss_mat + norm_k * mask_k.float()

        # Metrics (token-level, for consistency with your original code)
        n_k = mask_k.float().sum().clamp(min=1)
        clip_num += ((pg2_k > pg1_k).float() * mask_k.float()).sum()
        kl_num   += ((-log_ratio) * mask_k.float()).sum()
        is_num   += (is_k.unsqueeze(1).expand(B, L) * mask_k.float()).sum()
        denom    += n_k

    loss = agg_loss(
        loss_mat=pg_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="token-mean",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    metrics = {
        "actor/pg_clipfrac":  (clip_num / denom).item(),
        "actor/ppo_kl":       (kl_num   / denom).item(),
        "actor/empo_mean_is": (is_num   / denom).item(),
    }
    return loss, metrics


def _compute_token_level_grpo_losses(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    config,
    rollout_is_weights=None,
) -> tuple[torch.Tensor, dict]:
    """Token-level clipped GRPO/PPO loss matrix, before custom aggregation."""
    clip_ratio = config.clip_ratio
    clip_ratio_low = getattr(config, "clip_ratio_low", None)
    clip_ratio_high = getattr(config, "clip_ratio_high", None)
    if clip_ratio_low is None:
        clip_ratio_low = clip_ratio
    if clip_ratio_high is None:
        clip_ratio_high = clip_ratio

    clip_ratio_c = config.get("clip_ratio_c", 3.0) if hasattr(config, "get") else getattr(config, "clip_ratio_c", 3.0)
    assert clip_ratio_c > 1.0, (
        "The lower bound of the clip_ratio_c for dual-clip PPO should be greater than 1.0,"
        + f" but get the value: {clip_ratio_c}."
    )

    negative_approx_kl = torch.clamp(log_prob - old_log_prob, min=-20.0, max=20.0)
    ratio = torch.exp(negative_approx_kl)

    pg_losses1 = -advantages * ratio
    pg_losses2 = -advantages * torch.clamp(ratio, 1 - clip_ratio_low, 1 + clip_ratio_high)
    clip_pg_losses1 = torch.maximum(pg_losses1, pg_losses2)

    pg_losses3 = -advantages * clip_ratio_c
    clip_pg_losses2 = torch.min(pg_losses3, clip_pg_losses1)
    pg_losses = torch.where(advantages < 0, clip_pg_losses2, clip_pg_losses1)

    if rollout_is_weights is not None:
        pg_losses = pg_losses * rollout_is_weights

    valid_tokens = response_mask.float()
    denom = valid_tokens.sum().clamp(min=1)
    pg_clipfrac = ((pg_losses2 > pg_losses1).float() * valid_tokens).sum() / denom
    ppo_kl = ((-negative_approx_kl) * valid_tokens).sum() / denom
    pg_clipfrac_lower = (
        ((clip_pg_losses1 > pg_losses3).float() * (advantages < 0).float() * valid_tokens).sum() / denom
    )

    metrics = {
        "actor/pg_clipfrac": pg_clipfrac.detach().item(),
        "actor/ppo_kl": ppo_kl.detach().item(),
        "actor/pg_clipfrac_lower": pg_clipfrac_lower.detach().item(),
    }
    return pg_losses, metrics


def _build_turn_mean_loss_mat(
    token_losses: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
    divide_by_turn_count: bool,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Apply per-turn token averaging: (1 / m_i,k) * sum_t loss_i,k,t."""
    batch_size, _ = token_losses.shape
    valid_mask = response_mask.bool() & (turn_index >= 0)
    valid_turn_ids = turn_index[valid_mask]
    if valid_turn_ids.numel() == 0:
        return torch.zeros_like(token_losses), torch.zeros(
            batch_size, device=token_losses.device, dtype=token_losses.dtype
        )

    num_turns = int(valid_turn_ids.max().item()) + 1
    turn_counts = torch.zeros(batch_size, device=token_losses.device, dtype=token_losses.dtype)

    for k in range(num_turns):
        mask_k = valid_mask & (turn_index == k)
        turn_counts += (mask_k.float().sum(-1) > 0).float()

    turn_counts = turn_counts.clamp(min=1)
    turn_mean_loss_mat = torch.zeros_like(token_losses)

    for k in range(num_turns):
        mask_k = valid_mask & (turn_index == k)
        turn_len_k = mask_k.float().sum(-1).clamp(min=1)
        normalizer = turn_len_k.unsqueeze(1)
        if divide_by_turn_count:
            normalizer = normalizer * turn_counts.unsqueeze(1)
        turn_mean_loss_mat += token_losses * mask_k.float() / normalizer

    return turn_mean_loss_mat, turn_counts


def _get_turn_index(config, turn_index: Optional[torch.Tensor]) -> Optional[torch.Tensor]:
    if turn_index is not None:
        return turn_index
    if config is None or not hasattr(config, "global_batch_info"):
        return None
    return config.global_batch_info.get("turn_index", None)


def _build_turn_level_ppo_loss_mat(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
    config,
    rollout_is_weights: Optional[torch.Tensor] = None,
    turn_ratio_mode: Optional[str] = None,
) -> tuple[torch.Tensor, torch.Tensor, dict]:
    """Build a loss matrix whose sequence sum is mean over turn-level PPO losses.

    Supports two turn-ratio definitions (select via config):
      - "product":    ratio_turn = exp(sum_t log_ratio_t) (current behavior)
      - "geo_mean":   ratio_turn = exp(mean_t log_ratio_t) (length-normalized, more stable)

    Configure with either:
      - actor.policy_loss.turn_level_ppo_ratio_mode = "product" | "geo_mean"
      - actor.turn_level_ppo_ratio_mode            = "product" | "geo_mean" (fallback)
    """
    clip_ratio = config.clip_ratio
    clip_ratio_low = getattr(config, "clip_ratio_low", None)
    clip_ratio_high = getattr(config, "clip_ratio_high", None)
    if clip_ratio_low is None:
        clip_ratio_low = clip_ratio
    if clip_ratio_high is None:
        clip_ratio_high = clip_ratio
    clip_ratio_c = config.get("clip_ratio_c", 3.0) if hasattr(config, "get") else getattr(config, "clip_ratio_c", 3.0)

    # Turn-level ratio mode:
    # - "product"  : exp(sum log-ratio) (exact per-turn probability ratio; can grow with turn length)
    # - "geo_mean" : exp(mean log-ratio) (length-normalized; tends to be more stable across varying turn lengths)
    policy_loss_cfg = _config_get(config, "policy_loss", None)
    resolved_turn_ratio_mode = turn_ratio_mode
    if resolved_turn_ratio_mode is None:
        resolved_turn_ratio_mode = _config_get(policy_loss_cfg, "turn_level_ppo_ratio_mode", None)
    if resolved_turn_ratio_mode is None:
        # Default to the length-normalized (geometric-mean) turn ratio for stability.
        resolved_turn_ratio_mode = _config_get(config, "turn_level_ppo_ratio_mode", "geo_mean")
    if resolved_turn_ratio_mode not in ("product", "geo_mean"):
        raise ValueError(
            f"Invalid turn_level_ppo_ratio_mode: {resolved_turn_ratio_mode}. Must be one of ['product', 'geo_mean']."
        )

    valid_mask = response_mask.bool() & (turn_index >= 0)
    valid_turn_ids = turn_index[valid_mask]
    if valid_turn_ids.numel() == 0:
        return torch.zeros_like(log_prob), torch.zeros(log_prob.shape[0], device=log_prob.device), {
            "actor/turn_count_mean": 0.0,
            "actor/turn_ratio_mean": 0.0,
            "actor/turn_ppo_kl": 0.0,
            "actor/turn_ratio_mode": 1.0 if resolved_turn_ratio_mode == "geo_mean" else 0.0,
        }

    batch_size, _ = log_prob.shape
    num_turns = int(valid_turn_ids.max().item()) + 1
    dtype = log_prob.dtype
    device = log_prob.device

    turn_lengths = torch.zeros(batch_size, num_turns, device=device, dtype=dtype)
    turn_losses = torch.zeros(batch_size, num_turns, device=device, dtype=dtype)
    turn_log_ratios = torch.zeros(batch_size, num_turns, device=device, dtype=dtype)
    turn_valid = torch.zeros(batch_size, num_turns, device=device, dtype=dtype)

    token_log_ratio = (log_prob - old_log_prob) * response_mask.to(dtype=dtype)
    if rollout_is_weights is not None:
        rollout_is_weights = rollout_is_weights.to(device=device, dtype=dtype)

    # Clip bounds are expressed in log space for numerical stability.
    #
    # For "product" ratios, bounds compound with turn length: ratio_turn is exp(sum log_ratio),
    # so a per-token bound (1±eps) becomes (1±eps)^T. We implement that as T * log(1±eps).
    #
    # For "geo_mean" ratios, ratio_turn is exp(mean log_ratio), so we clip with log(1±eps)
    # directly (no length scaling).
    log_one_minus_low = math.log(max(1.0 - clip_ratio_low, 1e-8))
    log_one_plus_high = math.log(1.0 + clip_ratio_high)
    log_clip_c = math.log(clip_ratio_c)
    log_one_minus_low_t = torch.as_tensor(log_one_minus_low, device=device, dtype=dtype)
    log_one_plus_high_t = torch.as_tensor(log_one_plus_high, device=device, dtype=dtype)
    log_clip_c_t = torch.as_tensor(log_clip_c, device=device, dtype=dtype)

    for turn_id in range(num_turns):
        turn_mask = valid_mask & (turn_index == turn_id)
        turn_mask_f = turn_mask.to(dtype=dtype)
        turn_len = turn_mask_f.sum(-1)
        has_turn = turn_len > 0
        turn_lengths[:, turn_id] = turn_len
        turn_valid[:, turn_id] = has_turn.to(dtype=dtype)

        log_ratio_turn_sum = (token_log_ratio * turn_mask_f).sum(-1)
        if resolved_turn_ratio_mode == "geo_mean":
            log_ratio_turn = log_ratio_turn_sum / turn_len.clamp(min=1.0)
        else:
            log_ratio_turn = log_ratio_turn_sum
        log_ratio_turn = torch.clamp(log_ratio_turn, min=-20.0, max=20.0)
        ratio_turn = torch.exp(log_ratio_turn)
        adv_turn = (advantages * turn_mask_f).sum(-1) / turn_len.clamp(min=1.0)

        # Per-turn clip bounds in log space.
        if resolved_turn_ratio_mode == "geo_mean":
            turn_log_lo = log_one_minus_low_t
            turn_log_hi = log_one_plus_high_t
            turn_log_c = log_clip_c_t
        else:
            # "product" ratios compound with length.
            turn_log_lo = turn_len * log_one_minus_low
            turn_log_hi = turn_len * log_one_plus_high
            turn_log_c = turn_len * log_clip_c
        log_ratio_clipped = torch.minimum(torch.maximum(log_ratio_turn, turn_log_lo), turn_log_hi)
        ratio_turn_clipped = torch.exp(log_ratio_clipped)
        ratio_turn_dual = torch.exp(torch.minimum(log_ratio_turn, turn_log_c))

        pg_loss1 = -adv_turn * ratio_turn
        pg_loss2 = -adv_turn * ratio_turn_clipped
        clip_pg_loss1 = torch.maximum(pg_loss1, pg_loss2)
        pg_loss3 = -adv_turn * ratio_turn_dual
        clip_pg_loss2 = torch.min(pg_loss3, clip_pg_loss1)
        pg_loss = torch.where(adv_turn < 0, clip_pg_loss2, clip_pg_loss1)

        if rollout_is_weights is not None:
            turn_is = (rollout_is_weights * turn_mask_f).sum(-1) / turn_len.clamp(min=1.0)
            pg_loss = pg_loss * turn_is.detach()

        turn_losses[:, turn_id] = torch.where(has_turn, pg_loss, torch.zeros_like(pg_loss))
        turn_log_ratios[:, turn_id] = torch.where(has_turn, log_ratio_turn, torch.zeros_like(log_ratio_turn))

    turn_counts = turn_valid.sum(-1).clamp(min=1.0)
    loss_mat = torch.zeros_like(log_prob)
    for turn_id in range(num_turns):
        turn_mask_f = (valid_mask & (turn_index == turn_id)).to(dtype=dtype)
        token_loss = turn_losses[:, turn_id].unsqueeze(1) / (
            turn_counts.unsqueeze(1) * turn_lengths[:, turn_id].clamp(min=1.0).unsqueeze(1)
        )
        loss_mat = loss_mat + token_loss * turn_mask_f

    valid_turn_count = turn_valid.sum().clamp(min=1.0)
    turn_ratio = torch.exp(turn_log_ratios.clamp(min=-20.0, max=20.0))
    if resolved_turn_ratio_mode == "geo_mean":
        clipped_turn = ((turn_log_ratios < log_one_minus_low) | (turn_log_ratios > log_one_plus_high)).to(dtype=dtype)
    else:
        clipped_turn = (
            (turn_log_ratios < turn_lengths * log_one_minus_low)
            | (turn_log_ratios > turn_lengths * log_one_plus_high)
        ).to(dtype=dtype)
    seq_mask = (response_mask.float().sum(-1) > 0).float()
    seq_denom = seq_mask.sum().clamp(min=1.0)
    metrics = {
        "actor/pg_clipfrac": ((clipped_turn * turn_valid).sum() / valid_turn_count).detach().item(),
        "actor/ppo_kl": ((-turn_log_ratios * turn_valid).sum() / valid_turn_count).detach().item(),
        "actor/turn_ratio_mean": ((turn_ratio * turn_valid).sum() / valid_turn_count).detach().item(),
        "actor/turn_count_mean": ((turn_counts * seq_mask).sum() / seq_denom).detach().item(),
        # 0.0 = "product", 1.0 = "geo_mean"
        "actor/turn_ratio_mode": 1.0 if resolved_turn_ratio_mode == "geo_mean" else 0.0,
    }
    return loss_mat, turn_counts, metrics


@register_policy_loss("turn_level_ppo")
def compute_policy_loss_turn_level_ppo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config=None,
    rollout_is_weights=None,
    turn_index: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Turn-level PPO: clip one importance ratio per assistant turn.

    The ratio for turn i is computed from per-token log-ratios within that turn.
    By default this uses a length-normalized ("geo_mean") turn ratio for stability,
    but can be configured to use the raw product ratio.
    The turn advantage is the mean token advantage in that turn.
    """
    assert config is not None
    turn_index = _get_turn_index(config, turn_index)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    if not (response_mask.bool() & (turn_index >= 0)).any():
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    loss_mat, _, metrics = _build_turn_level_ppo_loss_mat(
        old_log_prob, log_prob, advantages, response_mask, turn_index, config, rollout_is_weights
    )
    loss = agg_loss(
        loss_mat=loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )
    return loss, metrics


@register_policy_loss("turn_level_ppo_product")
def compute_policy_loss_turn_level_ppo_product(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config=None,
    rollout_is_weights=None,
    turn_index: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Turn-level PPO with raw (product) turn importance ratios.

    The per-turn ratio is:
        ratio_turn = exp( sum_t (log pi_theta - log pi_old) )
    This matches the exact per-turn probability ratio, but its scale compounds
    with turn length and can be higher-variance than the geo-mean version.
    """
    assert config is not None
    turn_index = _get_turn_index(config, turn_index)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    if not (response_mask.bool() & (turn_index >= 0)).any():
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    loss_mat, _, metrics = _build_turn_level_ppo_loss_mat(
        old_log_prob,
        log_prob,
        advantages,
        response_mask,
        turn_index,
        config,
        rollout_is_weights,
        turn_ratio_mode="product",
    )
    loss = agg_loss(
        loss_mat=loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )
    return loss, metrics


@register_policy_loss("turn_level_ppo_geo_mean")
def compute_policy_loss_turn_level_ppo_geo_mean(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config=None,
    rollout_is_weights=None,
    turn_index: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Turn-level PPO with length-normalized (geometric-mean) turn importance ratios.

    The turn ratio is:
        ratio_turn = exp( mean_t (log pi_theta - log pi_old) )
    This keeps a single ratio per turn but avoids the exponential-with-length
    scaling of the raw product ratio.

    Note: this loss forces geo-mean ratios regardless of any config defaults.
    """
    assert config is not None
    turn_index = _get_turn_index(config, turn_index)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    if not (response_mask.bool() & (turn_index >= 0)).any():
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    loss_mat, _, metrics = _build_turn_level_ppo_loss_mat(
        old_log_prob,
        log_prob,
        advantages,
        response_mask,
        turn_index,
        config,
        rollout_is_weights,
        turn_ratio_mode="geo_mean",
    )
    loss = agg_loss(
        loss_mat=loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )
    return loss, metrics


@register_policy_loss("grpo_turn_normalized")
def compute_policy_loss_grpo_turn_normalized(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Turn-normalized GRPO:
        L_i = (1 / K_i) * sum_k (1 / m_i,k) * sum_t ell_i,k,t.
    """
    assert config is not None
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    token_losses, metrics = _compute_token_level_grpo_losses(
        old_log_prob, log_prob, advantages, response_mask, config, rollout_is_weights
    )
    pg_loss_mat, turn_counts = _build_turn_mean_loss_mat(
        token_losses, response_mask, turn_index, divide_by_turn_count=True
    )

    loss = agg_loss(
        loss_mat=pg_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    seq_mask = (response_mask.float().sum(-1) > 0).float()
    seq_denom = seq_mask.sum().clamp(min=1)
    metrics["actor/turn_count_mean"] = ((turn_counts * seq_mask).sum() / seq_denom).detach().item()
    return loss, metrics


@register_policy_loss("grpo_double_length_normalized")
def compute_policy_loss_grpo_double_length_normalized(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Double-length-normalized GRPO:
        L_i = (1 / N_i) * sum_k (1 / m_i,k) * sum_t ell_i,k,t.
    """
    assert config is not None
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    token_losses, metrics = _compute_token_level_grpo_losses(
        old_log_prob, log_prob, advantages, response_mask, config, rollout_is_weights
    )
    pg_loss_mat, turn_counts = _build_turn_mean_loss_mat(
        token_losses, response_mask, turn_index, divide_by_turn_count=False
    )

    loss = agg_loss(
        loss_mat=pg_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-mean",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    seq_mask = (response_mask.float().sum(-1) > 0).float()
    seq_denom = seq_mask.sum().clamp(min=1)
    metrics["actor/turn_count_mean"] = ((turn_counts * seq_mask).sum() / seq_denom).detach().item()
    return loss, metrics



##########


def _config_get(config, key: str, default):
    """Get a value from config with a default fallback."""
    if config is None:
        return default
    if hasattr(config, "get"):
        return config.get(key, default)
    return getattr(config, key, default)

def _masked_mean(values: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    mask = mask.to(dtype=values.dtype, device=values.device)
    return (values * mask).sum() / mask.sum().clamp(min=1)


_STATE_PREDICTIVE_LOSS_MODES = {
    "state_predictive_grpo",
    "state_predictive_grpo_normalized",
}


def _state_predictive_singleton_index(mask: torch.Tensor) -> torch.Tensor:
    state_index = torch.full(mask.shape, -1, device=mask.device, dtype=torch.int32)
    valid_positions = torch.nonzero(mask.bool(), as_tuple=False).flatten()
    if valid_positions.numel() > 0:
        state_index[valid_positions] = torch.arange(valid_positions.numel(), device=mask.device, dtype=torch.int32)
    return state_index


def _state_predictive_build_features(
    token_losses: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    config,
    update_sketch: Optional[torch.Tensor] = None,
) -> tuple[torch.Tensor, bool]:
    """Build detached token credit features used only for state discovery."""
    use_update_sketch = bool(_config_get(config.policy_loss, "state_predictive_use_update_sketch", True))
    if use_update_sketch and update_sketch is not None and update_sketch.dim() == 3:
        features = update_sketch.to(device=token_losses.device, dtype=torch.float32).detach()
        features = features * advantages.detach().to(dtype=torch.float32).unsqueeze(-1)
        used_update_sketch = True
    else:
        features = token_losses.detach().to(dtype=torch.float32).unsqueeze(-1)
        used_update_sketch = False

    if bool(_config_get(config.policy_loss, "state_predictive_normalize_features", True)):
        mask = response_mask.to(device=features.device, dtype=torch.float32).unsqueeze(-1)
        denom = mask.sum(dim=(0, 1), keepdim=True).clamp(min=1.0)
        mean = (features * mask).sum(dim=(0, 1), keepdim=True) / denom
        centered = (features - mean) * mask
        var = centered.square().sum(dim=(0, 1), keepdim=True) / denom
        features = (features - mean) / var.clamp(min=1e-6).sqrt()
        features = features * mask

    return features, used_update_sketch


def _state_predictive_segment_numpy(
    values: np.ndarray,
    *,
    min_segment_len: int,
    max_segment_len: int,
    dinkelbach_iters: int,
    tol: float,
    eps: float,
) -> tuple[list[tuple[int, int]], float, float, float, bool]:
    """Exact Dinkelbach + DP segmentation over a bounded segment-length class."""
    n = int(values.shape[0])
    if n <= 1:
        return [(0, 0)] if n == 1 else [], 0.0, 0.0, 0.0, True

    min_segment_len = max(2, int(min_segment_len))
    max_segment_len = max(min_segment_len, int(max_segment_len))
    max_segment_len = min(max_segment_len, n)
    dinkelbach_iters = max(1, int(dinkelbach_iters))

    if n < min_segment_len:
        return [(t, t) for t in range(n)], 0.0, 0.0, 0.0, True

    x = values.astype(np.float64, copy=False)
    prefix = np.zeros((n + 1, x.shape[1]), dtype=np.float64)
    prefix[1:] = np.cumsum(x, axis=0)
    sq_prefix = np.zeros(n + 1, dtype=np.float64)
    sq_prefix[1:] = np.cumsum(np.sum(x * x, axis=1), axis=0)
    global_mean = prefix[n] / max(n, 1)

    # Precompute additive segment signal and predictive-noise costs.
    signal_cost = np.full((n + 1, max_segment_len + 1), -np.inf, dtype=np.float64)
    noise_cost = np.full((n + 1, max_segment_len + 1), np.inf, dtype=np.float64)
    for end in range(1, n + 1):
        max_len_here = min(max_segment_len, end)
        for seg_len in range(min_segment_len, max_len_here + 1):
            start = end - seg_len
            seg_sum = prefix[end] - prefix[start]
            seg_mean = seg_sum / seg_len
            seg_sq_sum = sq_prefix[end] - sq_prefix[start]
            rss = float(seg_sq_sum - seg_len * np.dot(seg_mean, seg_mean))
            if rss < 0.0 and rss > -1e-7:
                rss = 0.0
            rss = max(rss, 0.0)
            centered = seg_mean - global_mean
            signal_cost[end, seg_len] = seg_len * float(np.dot(centered, centered))
            noise_cost[end, seg_len] = ((seg_len + 1.0) / (seg_len - 1.0)) * rss

    def solve_for_eta(eta: float) -> tuple[list[tuple[int, int]], float, float, float, bool]:
        dp = np.full(n + 1, -np.inf, dtype=np.float64)
        prev_len = np.full(n + 1, -1, dtype=np.int64)
        dp[0] = 0.0
        for end in range(1, n + 1):
            max_len_here = min(max_segment_len, end)
            if max_len_here < min_segment_len:
                continue
            lens = np.arange(min_segment_len, max_len_here + 1, dtype=np.int64)
            starts = end - lens
            valid = np.isfinite(dp[starts])
            if not np.any(valid):
                continue
            lens = lens[valid]
            starts = starts[valid]
            scores = dp[starts] + signal_cost[end, lens] - eta * noise_cost[end, lens]
            best_pos = int(np.argmax(scores))
            dp[end] = float(scores[best_pos])
            prev_len[end] = int(lens[best_pos])

        if not np.isfinite(dp[n]) or prev_len[n] < 0:
            return [(t, t) for t in range(n)], 0.0, 0.0, 0.0, True

        segments: list[tuple[int, int]] = []
        total_signal = 0.0
        total_noise = 0.0
        end = n
        while end > 0:
            seg_len = int(prev_len[end])
            start = end - seg_len
            segments.append((start, end - 1))
            total_signal += float(signal_cost[end, seg_len])
            total_noise += float(noise_cost[end, seg_len])
            end = start
        segments.reverse()
        ratio = total_signal / (total_noise + eps)
        return segments, total_signal, total_noise, ratio, False

    eta = 0.0
    best_segments: list[tuple[int, int]] | None = None
    best_signal = 0.0
    best_noise = 0.0
    best_ratio = 0.0
    fallback = True
    for _ in range(dinkelbach_iters):
        segments, total_signal, total_noise, ratio, fallback = solve_for_eta(eta)
        best_segments = segments
        best_signal = total_signal
        best_noise = total_noise
        best_ratio = ratio
        delta = total_signal - eta * (total_noise + eps)
        if abs(delta) <= tol * max(1.0, abs(total_signal)):
            break
        eta = ratio

    if best_segments is None:
        return [(t, t) for t in range(n)], 0.0, 0.0, 0.0, True
    return best_segments, best_signal, best_noise, best_ratio, fallback


def _build_state_predictive_index(
    features: torch.Tensor,
    response_mask: torch.Tensor,
    config,
) -> tuple[torch.Tensor, dict[str, float]]:
    device = response_mask.device
    batch_size, seq_len = response_mask.shape
    state_index = torch.full((batch_size, seq_len), -1, device=device, dtype=torch.int32)

    min_segment_len = int(_config_get(config.policy_loss, "state_predictive_min_segment_len", 2))
    max_segment_len = int(_config_get(config.policy_loss, "state_predictive_max_segment_len", 128))
    dinkelbach_iters = int(_config_get(config.policy_loss, "state_predictive_dinkelbach_iters", 6))
    tol = float(_config_get(config.policy_loss, "state_predictive_tol", 1e-4))
    eps = float(_config_get(config.policy_loss, "state_predictive_eps", 1e-8))

    state_counts: list[float] = []
    state_lengths: list[float] = []
    ratios: list[float] = []
    signals: list[float] = []
    noises: list[float] = []
    fallback_count = 0
    valid_seq_count = 0

    with torch.no_grad():
        for row in range(batch_size):
            mask_row = response_mask[row].bool()
            valid_positions = torch.nonzero(mask_row, as_tuple=False).flatten()
            if valid_positions.numel() == 0:
                continue
            valid_seq_count += 1
            values = features[row, valid_positions].detach().float().cpu().numpy()
            segments, signal, noise, ratio, fallback = _state_predictive_segment_numpy(
                values,
                min_segment_len=min_segment_len,
                max_segment_len=max_segment_len,
                dinkelbach_iters=dinkelbach_iters,
                tol=tol,
                eps=eps,
            )
            if fallback:
                fallback_count += 1
            for state_id, (start, end) in enumerate(segments):
                token_positions = valid_positions[start : end + 1]
                state_index[row, token_positions] = state_id
                state_lengths.append(float(end - start + 1))
            state_counts.append(float(len(segments)))
            ratios.append(float(ratio))
            signals.append(float(signal))
            noises.append(float(noise))

    denom = max(valid_seq_count, 1)
    metrics = {
        "actor/state_predictive/state_count_mean": float(sum(state_counts) / max(len(state_counts), 1)),
        "actor/state_predictive/state_len_mean": float(sum(state_lengths) / max(len(state_lengths), 1)),
        "actor/state_predictive/state_len_max": float(max(state_lengths) if state_lengths else 0.0),
        "actor/state_predictive/snr_mean": float(sum(ratios) / max(len(ratios), 1)),
        "actor/state_predictive/signal_mean": float(sum(signals) / max(len(signals), 1)),
        "actor/state_predictive/noise_mean": float(sum(noises) / max(len(noises), 1)),
        "actor/state_predictive/fallback_frac": float(fallback_count / denom),
        "actor/state_predictive/max_segment_len": float(max_segment_len),
    }
    return state_index, metrics


def _build_state_predictive_index_torch(
    features: torch.Tensor,
    response_mask: torch.Tensor,
    config,
) -> tuple[torch.Tensor, dict[str, float]]:
    """Torch implementation of state discovery that keeps DP scoring on-device.

    The exact partition recurrence is still left-to-right in sequence length,
    but each step scores all rows and candidate segment lengths in parallel.
    """
    device = response_mask.device
    batch_size, seq_len = response_mask.shape
    dtype = torch.float32
    mask = response_mask.bool()
    state_index = torch.full((batch_size, seq_len), -1, device=device, dtype=torch.int32)
    lengths = mask.sum(dim=-1).to(dtype=torch.long)
    valid_seq_mask = lengths > 0
    valid_seq_count = int(valid_seq_mask.sum().item())

    min_segment_len = max(2, int(_config_get(config.policy_loss, "state_predictive_min_segment_len", 2)))
    max_segment_len_cfg = int(_config_get(config.policy_loss, "state_predictive_max_segment_len", 128))
    dinkelbach_iters = max(1, int(_config_get(config.policy_loss, "state_predictive_dinkelbach_iters", 6)))
    tol = float(_config_get(config.policy_loss, "state_predictive_tol", 1e-4))
    eps = float(_config_get(config.policy_loss, "state_predictive_eps", 1e-8))

    if valid_seq_count == 0:
        return state_index, {
            "actor/state_predictive/state_count_mean": 0.0,
            "actor/state_predictive/state_len_mean": 0.0,
            "actor/state_predictive/state_len_max": 0.0,
            "actor/state_predictive/snr_mean": 0.0,
            "actor/state_predictive/signal_mean": 0.0,
            "actor/state_predictive/noise_mean": 0.0,
            "actor/state_predictive/fallback_frac": 0.0,
            "actor/state_predictive/max_segment_len": float(max_segment_len_cfg),
            "actor/state_predictive/segment_backend_torch": 1.0,
        }

    compact_rank = mask.to(dtype=torch.long).cumsum(dim=-1) - 1
    valid_rows, valid_cols = torch.nonzero(mask, as_tuple=True)
    state_index[valid_rows, valid_cols] = compact_rank[valid_rows, valid_cols].to(torch.int32)

    max_n = int(lengths.max().item())
    if max_n < min_segment_len:
        state_counts = lengths[valid_seq_mask].to(dtype=dtype)
        return state_index, {
            "actor/state_predictive/state_count_mean": state_counts.mean().detach().item(),
            "actor/state_predictive/state_len_mean": 1.0,
            "actor/state_predictive/state_len_max": 1.0,
            "actor/state_predictive/snr_mean": 0.0,
            "actor/state_predictive/signal_mean": 0.0,
            "actor/state_predictive/noise_mean": 0.0,
            "actor/state_predictive/fallback_frac": 1.0,
            "actor/state_predictive/max_segment_len": float(max_segment_len_cfg),
            "actor/state_predictive/segment_backend_torch": 1.0,
        }

    max_segment_len = min(max(min_segment_len, max_segment_len_cfg), max_n)
    feature_dim = features.shape[-1]
    values = torch.zeros(batch_size, max_n, feature_dim, device=device, dtype=dtype)
    values[valid_rows, compact_rank[valid_rows, valid_cols]] = features[valid_rows, valid_cols].detach().to(dtype=dtype)

    prefix = torch.zeros(batch_size, max_n + 1, feature_dim, device=device, dtype=dtype)
    prefix[:, 1:] = torch.cumsum(values, dim=1)
    sq_values = values.square().sum(dim=-1)
    sq_prefix = torch.zeros(batch_size, max_n + 1, device=device, dtype=dtype)
    sq_prefix[:, 1:] = torch.cumsum(sq_values, dim=1)

    batch_ids = torch.arange(batch_size, device=device)
    safe_lengths = lengths.clamp(min=1)
    global_sum = prefix[batch_ids, safe_lengths]
    global_mean = global_sum / safe_lengths.to(dtype=dtype).unsqueeze(-1)

    neg_inf = -torch.inf
    signal_cost = torch.full((batch_size, max_n + 1, max_segment_len + 1), neg_inf, device=device, dtype=dtype)
    noise_cost = torch.zeros((batch_size, max_n + 1, max_segment_len + 1), device=device, dtype=dtype)

    for seg_len in range(min_segment_len, max_segment_len + 1):
        seg_sum = prefix[:, seg_len:] - prefix[:, : max_n + 1 - seg_len]
        seg_sq_sum = sq_prefix[:, seg_len:] - sq_prefix[:, : max_n + 1 - seg_len]
        seg_mean = seg_sum / float(seg_len)
        rss = seg_sq_sum - float(seg_len) * seg_mean.square().sum(dim=-1)
        rss = rss.clamp_min(0.0)
        centered = seg_mean - global_mean.unsqueeze(1)
        signal = float(seg_len) * centered.square().sum(dim=-1)
        noise = ((seg_len + 1.0) / (seg_len - 1.0)) * rss
        end_ids = torch.arange(seg_len, max_n + 1, device=device).unsqueeze(0)
        valid = end_ids <= lengths.unsqueeze(1)
        signal_cost[:, seg_len:, seg_len] = torch.where(valid, signal, torch.full_like(signal, neg_inf))
        noise_cost[:, seg_len:, seg_len] = torch.where(valid, noise, torch.zeros_like(noise))

    def solve_for_eta(eta: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        dp = torch.full((batch_size, max_n + 1), neg_inf, device=device, dtype=dtype)
        signal_dp = torch.zeros((batch_size, max_n + 1), device=device, dtype=dtype)
        noise_dp = torch.zeros((batch_size, max_n + 1), device=device, dtype=dtype)
        prev_len = torch.full((batch_size, max_n + 1), -1, device=device, dtype=torch.long)
        dp[:, 0] = 0.0

        for end in range(1, max_n + 1):
            max_len_here = min(max_segment_len, end)
            if max_len_here < min_segment_len:
                continue
            lens = torch.arange(min_segment_len, max_len_here + 1, device=device, dtype=torch.long)
            starts = end - lens
            sig = signal_cost[:, end, lens]
            noi = noise_cost[:, end, lens]
            candidate_scores = dp[:, starts] + sig - eta.unsqueeze(-1) * noi
            best_scores, best_pos = torch.max(candidate_scores, dim=1)
            valid = torch.isfinite(best_scores) & (end <= lengths)
            best_lens = lens[best_pos]
            best_signal = (signal_dp[:, starts] + sig).gather(1, best_pos.unsqueeze(1)).squeeze(1)
            best_noise = (noise_dp[:, starts] + noi).gather(1, best_pos.unsqueeze(1)).squeeze(1)
            dp[:, end] = torch.where(valid, best_scores, torch.full_like(best_scores, neg_inf))
            signal_dp[:, end] = torch.where(valid, best_signal, torch.zeros_like(best_signal))
            noise_dp[:, end] = torch.where(valid, best_noise, torch.zeros_like(best_noise))
            prev_len[:, end] = torch.where(valid, best_lens, torch.full_like(best_lens, -1))

        total_signal = signal_dp[batch_ids, lengths]
        total_noise = noise_dp[batch_ids, lengths]
        finished = torch.isfinite(dp[batch_ids, lengths]) & (lengths >= min_segment_len)
        ratio = total_signal / (total_noise + eps)
        fallback = valid_seq_mask & ~finished
        total_signal = torch.where(finished, total_signal, torch.zeros_like(total_signal))
        total_noise = torch.where(finished, total_noise, torch.zeros_like(total_noise))
        ratio = torch.where(finished, ratio, torch.zeros_like(ratio))
        return prev_len, total_signal, total_noise, ratio, fallback

    eta = torch.zeros(batch_size, device=device, dtype=dtype)
    best_prev_len = None
    best_signal = torch.zeros(batch_size, device=device, dtype=dtype)
    best_noise = torch.zeros(batch_size, device=device, dtype=dtype)
    best_ratio = torch.zeros(batch_size, device=device, dtype=dtype)
    best_fallback = valid_seq_mask.clone()
    for _ in range(dinkelbach_iters):
        prev_len, total_signal, total_noise, ratio, fallback = solve_for_eta(eta)
        best_prev_len = prev_len
        best_signal = total_signal
        best_noise = total_noise
        best_ratio = ratio
        best_fallback = fallback
        delta = total_signal - eta * (total_noise + eps)
        converged = delta.abs() <= tol * torch.maximum(torch.ones_like(total_signal), total_signal.abs())
        eta = torch.where(fallback | converged, eta, ratio)

    assert best_prev_len is not None
    compact_state_index = torch.full((batch_size, max_n), -1, device=device, dtype=torch.int32)
    state_counts: list[float] = []
    state_lengths: list[float] = []
    fallback_count = 0
    finished_rows = (valid_seq_mask & ~best_fallback & (lengths >= min_segment_len)).detach().cpu().tolist()
    fallback_rows = (valid_seq_mask & ~torch.as_tensor(finished_rows, device=device, dtype=torch.bool)).detach().cpu().tolist()
    prev_len_cpu = best_prev_len.detach().cpu()
    lengths_cpu = lengths.detach().cpu()

    for row, is_fallback in enumerate(fallback_rows):
        if not valid_seq_mask[row]:
            continue
        if is_fallback:
            n = int(lengths_cpu[row].item())
            compact_state_index[row, :n] = torch.arange(n, device=device, dtype=torch.int32)
            state_counts.append(float(n))
            state_lengths.extend([1.0] * n)
            fallback_count += 1

    for row, is_finished in enumerate(finished_rows):
        if not is_finished:
            continue
        end = int(lengths_cpu[row].item())
        row_segments: list[tuple[int, int, int]] = []
        while end > 0:
            seg_len = int(prev_len_cpu[row, end].item())
            if seg_len <= 0:
                break
            start = end - seg_len
            row_segments.append((start, end, seg_len))
            end = start
        if end != 0:
            n = int(lengths_cpu[row].item())
            compact_state_index[row, :n] = torch.arange(n, device=device, dtype=torch.int32)
            state_counts.append(float(n))
            state_lengths.extend([1.0] * n)
            fallback_count += 1
        else:
            row_segments.reverse()
            for state_id, (start, end, seg_len) in enumerate(row_segments):
                compact_state_index[row, start:end] = state_id
                state_lengths.append(float(seg_len))
            state_counts.append(float(len(row_segments)))

    state_index = torch.full((batch_size, seq_len), -1, device=device, dtype=torch.int32)
    state_index[valid_rows, valid_cols] = compact_state_index[valid_rows, compact_rank[valid_rows, valid_cols]]

    valid_for_metrics = valid_seq_mask
    signal_values = best_signal[valid_for_metrics & ~best_fallback].detach()
    noise_values = best_noise[valid_for_metrics & ~best_fallback].detach()
    ratio_values = best_ratio[valid_for_metrics & ~best_fallback].detach()
    denom = max(valid_seq_count, 1)
    metrics = {
        "actor/state_predictive/state_count_mean": float(sum(state_counts) / max(len(state_counts), 1)),
        "actor/state_predictive/state_len_mean": float(sum(state_lengths) / max(len(state_lengths), 1)),
        "actor/state_predictive/state_len_max": float(max(state_lengths) if state_lengths else 0.0),
        "actor/state_predictive/snr_mean": ratio_values.mean().detach().item() if ratio_values.numel() else 0.0,
        "actor/state_predictive/signal_mean": signal_values.mean().detach().item() if signal_values.numel() else 0.0,
        "actor/state_predictive/noise_mean": noise_values.mean().detach().item() if noise_values.numel() else 0.0,
        "actor/state_predictive/fallback_frac": float(fallback_count / denom),
        "actor/state_predictive/max_segment_len": float(max_segment_len),
        "actor/state_predictive/segment_backend_torch": 1.0,
    }
    return state_index, metrics


def _state_predictive_metrics_from_index(
    state_index: torch.Tensor,
    response_mask: torch.Tensor,
    config,
) -> dict[str, float]:
    """Lightweight metrics when state_index is precomputed outside the loss."""
    mask = response_mask.bool() & (state_index >= 0)
    seq_mask = mask.any(dim=-1)
    state_counts_t = torch.where(
        seq_mask,
        state_index.clamp_min(-1).amax(dim=-1).to(dtype=torch.float32) + 1.0,
        torch.zeros(state_index.shape[0], device=state_index.device, dtype=torch.float32),
    )
    state_counts = state_counts_t[seq_mask].detach().cpu().tolist()
    state_lengths: list[float] = []
    state_index_cpu = state_index.detach().cpu()
    mask_cpu = mask.detach().cpu()
    for row in range(state_index.shape[0]):
        ids = state_index_cpu[row, mask_cpu[row]]
        if ids.numel() == 0:
            continue
        counts = torch.bincount(ids.to(torch.long))
        state_lengths.extend(float(x) for x in counts.tolist() if x > 0)

    total_states = max(sum(state_counts), 1.0)
    total_tokens = float(response_mask.to(dtype=torch.float32).sum().detach().item())
    max_segment_len = int(_config_get(config.policy_loss, "state_predictive_max_segment_len", 128))
    return {
        "actor/state_predictive/state_count_mean": float(sum(state_counts) / max(len(state_counts), 1)),
        "actor/state_predictive/state_len_mean": float(total_tokens / total_states),
        "actor/state_predictive/state_len_max": float(max(state_lengths) if state_lengths else 0.0),
        "actor/state_predictive/snr_mean": 0.0,
        "actor/state_predictive/signal_mean": 0.0,
        "actor/state_predictive/noise_mean": 0.0,
        "actor/state_predictive/fallback_frac": 0.0,
        "actor/state_predictive/max_segment_len": float(max_segment_len),
        "actor/state_predictive/precomputed_state_index": 1.0,
    }


def build_state_predictive_index_from_update_sketch(
    *,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    update_sketch: Optional[torch.Tensor],
    config,
) -> tuple[Optional[torch.Tensor], dict[str, float]]:
    """Precompute state_index when state discovery depends only on update_sketch."""
    use_update_sketch = bool(_config_get(config.policy_loss, "state_predictive_use_update_sketch", True))
    if not use_update_sketch or update_sketch is None or update_sketch.dim() != 3:
        return None, {}

    token_losses = torch.zeros_like(advantages)
    features, used_update_sketch = _state_predictive_build_features(
        token_losses=token_losses,
        advantages=advantages,
        response_mask=response_mask,
        config=config,
        update_sketch=update_sketch,
    )
    if not used_update_sketch:
        return None, {}

    segment_backend = str(_config_get(config.policy_loss, "state_predictive_segment_backend", "numpy")).lower()
    if segment_backend in {"torch", "gpu", "cuda"}:
        state_index, metrics = _build_state_predictive_index_torch(features, response_mask, config)
    else:
        state_index, metrics = _build_state_predictive_index(features, response_mask, config)
        metrics["actor/state_predictive/segment_backend_torch"] = 0.0
    metrics["actor/state_predictive/precomputed_state_index"] = 1.0
    return state_index, metrics


@register_policy_loss("state_predictive_grpo")
@register_policy_loss("state_predictive_grpo_normalized")
def compute_policy_loss_state_predictive_grpo(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config=None,
    rollout_is_weights=None,
    update_sketch: Optional[torch.Tensor] = None,
    state_index: Optional[torch.Tensor] = None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Learn contiguous state segments by predictive gradient SNR, then apply a
    state-level GRPO-style loss. The segmentation is detached and can use the
    update-sketch credit proxy S(e_y - pi) when the actor provides it.
    """
    assert config is not None

    token_losses, base_metrics = _compute_token_level_grpo_losses(
        old_log_prob, log_prob, advantages, response_mask, config, rollout_is_weights
    )
    features, used_update_sketch = _state_predictive_build_features(
        token_losses=token_losses,
        advantages=advantages,
        response_mask=response_mask,
        config=config,
        update_sketch=update_sketch,
    )
    if state_index is not None:
        state_index = state_index.to(device=log_prob.device, dtype=torch.int32)
        state_metrics = _state_predictive_metrics_from_index(state_index, response_mask, config)
    else:
        state_index_start = time.perf_counter()
        segment_backend = str(_config_get(config.policy_loss, "state_predictive_segment_backend", "numpy")).lower()
        if segment_backend in {"torch", "gpu", "cuda"}:
            state_index, state_metrics = _build_state_predictive_index_torch(features, response_mask, config)
        else:
            state_index, state_metrics = _build_state_predictive_index(features, response_mask, config)
            state_metrics["actor/state_predictive/segment_backend_torch"] = 0.0
        state_metrics["actor/state_predictive/build_index_seconds"] = time.perf_counter() - state_index_start
        state_index = state_index.to(device=log_prob.device)

    loss_type = str(_config_get(config.policy_loss, "state_predictive_loss_type", "state_level"))
    if loss_type == "normalized" or _config_get(config.policy_loss, "loss_mode", "") == "state_predictive_grpo_normalized":
        loss_mat, state_counts = _build_turn_mean_loss_mat(
            token_losses, response_mask, state_index, divide_by_turn_count=True
        )
        loss = agg_loss(
            loss_mat=loss_mat,
            loss_mask=response_mask,
            loss_agg_mode="seq-mean-token-sum",
            **get_agg_loss_kwargs(config.global_batch_info),
        )
        metrics = dict(base_metrics)
        seq_mask = (response_mask.float().sum(-1) > 0).float()
        seq_denom = seq_mask.sum().clamp(min=1.0)
        metrics["actor/state_predictive/loss_type"] = 0.0
        metrics["actor/state_predictive/loss_state_count_mean"] = (
            (state_counts * seq_mask).sum() / seq_denom
        ).detach().item()
    else:
        ratio_mode = str(_config_get(config.policy_loss, "state_predictive_ratio_mode", "geo_mean"))
        loss_mat, state_counts, metrics = _build_turn_level_ppo_loss_mat(
            old_log_prob,
            log_prob,
            advantages,
            response_mask,
            state_index,
            config,
            rollout_is_weights,
            turn_ratio_mode=ratio_mode,
        )
        loss = agg_loss(
            loss_mat=loss_mat,
            loss_mask=response_mask,
            loss_agg_mode="seq-mean-token-sum",
            **get_agg_loss_kwargs(config.global_batch_info),
        )
        seq_mask = (response_mask.float().sum(-1) > 0).float()
        seq_denom = seq_mask.sum().clamp(min=1.0)
        metrics["actor/state_predictive/loss_type"] = 1.0
        metrics["actor/state_predictive/loss_state_count_mean"] = (
            (state_counts * seq_mask).sum() / seq_denom
        ).detach().item()

    metrics.update(state_metrics)
    metrics["actor/state_predictive/used_update_sketch"] = float(used_update_sketch)
    return loss, metrics


@register_policy_loss("grpo_adaptive_grpo_turn_normalized")
def compute_policy_loss_grpo_adaptive_grpo_turn_normalized(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Adaptive mixture:
        L = (1 - lambda) * L_grpo + lambda * L_turn.

    lambda is based on a detached within-turn vs between-turn variance proxy.
    It stays near 0 when token-level GRPO is likely lower variance, and moves
    toward 1 when turn-correlated variation plus length imbalance dominate.
    """
    assert config is not None
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    token_losses, metrics = _compute_token_level_grpo_losses(
        old_log_prob, log_prob, advantages, response_mask, config, rollout_is_weights
    )

    grpo_agg_mode = _config_get(config, "adaptive_grpo_agg_mode", "seq-mean-token-mean")
    grpo_loss = agg_loss(
        loss_mat=token_losses,
        loss_mask=response_mask,
        loss_agg_mode=grpo_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    turn_loss_mat, turn_counts = _build_turn_mean_loss_mat(
        token_losses, response_mask, turn_index, divide_by_turn_count=True
    )
    turn_loss = agg_loss(
        loss_mat=turn_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    ratio_low = float(_config_get(config, "adaptive_turn_ratio_low", 1.0))
    ratio_high = float(_config_get(config, "adaptive_turn_ratio_high", 3.0))
    lambda_turn, adaptive_metrics = _compute_turn_variance_lambda(
        token_losses, response_mask, turn_index, ratio_low=ratio_low, ratio_high=ratio_high
    )
    loss = (1.0 - lambda_turn) * grpo_loss + lambda_turn * turn_loss

    seq_mask = (response_mask.float().sum(-1) > 0).float()
    seq_denom = seq_mask.sum().clamp(min=1)
    metrics.update(adaptive_metrics)
    metrics["actor/grpo_loss"] = grpo_loss.detach().item()
    metrics["actor/turn_loss"] = turn_loss.detach().item()
    metrics["actor/turn_count_mean"] = ((turn_counts * seq_mask).sum() / seq_denom).detach().item()
    return loss, metrics


def _build_per_turn_adaptive_loss_mat(
    token_losses: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
    ratio_low: float,
    ratio_high: float,
) -> tuple[torch.Tensor, torch.Tensor, dict]:
    """
    Build per-turn adaptive loss matrix:
        L_i = sum_k w_i,k * mean_t ell_i,k,t.

    w_i,k interpolates between GRPO turn weight m_i,k / N_i and
    turn-normalized weight 1 / K_i according to a detached per-turn ratio:
        R_i,k = m_i,k * tau_i^2 / sigma_i,k^2.
    """
    batch_size, _ = token_losses.shape
    valid_mask = response_mask.bool() & (turn_index >= 0)
    valid_turn_ids = turn_index[valid_mask]
    if valid_turn_ids.numel() == 0:
        metrics = {
            "actor/per_turn_adaptive_lambda_mean": 0.0,
            "actor/per_turn_adaptive_ratio_mean": 0.0,
            "actor/per_turn_between_var_mean": 0.0,
            "actor/per_turn_within_var_mean": 0.0,
            "actor/per_turn_weight_entropy": 0.0,
        }
        return (
            torch.zeros_like(token_losses),
            torch.zeros(batch_size, device=token_losses.device, dtype=token_losses.dtype),
            metrics,
        )

    num_turns = int(valid_turn_ids.max().item()) + 1
    detached_losses = token_losses.detach()
    turn_lengths = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)
    turn_means = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)
    within_vars = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)

    for turn_id in range(num_turns):
        turn_mask = valid_mask & (turn_index == turn_id)
        turn_mask_float = turn_mask.float()
        turn_len = turn_mask_float.sum(-1)
        turn_sum = (detached_losses * turn_mask_float).sum(-1)
        turn_mean = turn_sum / turn_len.clamp(min=1)
        within_sum_squares = ((detached_losses - turn_mean.unsqueeze(1)).pow(2) * turn_mask_float).sum(-1)
        within_var = within_sum_squares / (turn_len - 1).clamp(min=1)

        turn_lengths[:, turn_id] = turn_len
        turn_means[:, turn_id] = turn_mean
        within_vars[:, turn_id] = torch.where(turn_len > 1, within_var, torch.zeros_like(within_var))

    turn_mask = (turn_lengths > 0).float()
    turn_counts = turn_mask.sum(-1).clamp(min=1)
    token_counts = turn_lengths.sum(-1).clamp(min=1)

    seq_turn_mean = (turn_means * turn_mask).sum(-1) / turn_counts
    between_sum_squares = ((turn_means - seq_turn_mean.unsqueeze(1)).pow(2) * turn_mask).sum(-1)
    between_var_raw = between_sum_squares / (turn_counts - 1).clamp(min=1)
    correction = ((within_vars / turn_lengths.clamp(min=1)) * turn_mask).sum(-1) / turn_counts
    between_var = (between_var_raw - correction).clamp(min=0)

    ratio = turn_lengths * between_var.unsqueeze(1) / within_vars.clamp(min=1e-8)
    ratio = ratio * turn_mask

    if ratio_high <= ratio_low or ratio_high <= 0:
        lambda_turn = torch.zeros_like(ratio)
    else:
        log_ratio = torch.log(ratio.clamp(min=1e-8))
        log_low = torch.log(torch.tensor(max(ratio_low, 1e-8), device=token_losses.device, dtype=token_losses.dtype))
        log_high = torch.log(torch.tensor(ratio_high, device=token_losses.device, dtype=token_losses.dtype))
        lambda_turn = ((log_ratio - log_low) / (log_high - log_low).clamp(min=1e-8)).clamp(0.0, 1.0)
        lambda_turn = lambda_turn * turn_mask

    grpo_turn_weight = turn_lengths / token_counts.unsqueeze(1)
    equal_turn_weight = turn_mask / turn_counts.unsqueeze(1)
    mixed_turn_weight = (1.0 - lambda_turn) * grpo_turn_weight + lambda_turn * equal_turn_weight
    mixed_turn_weight = mixed_turn_weight * turn_mask
    mixed_turn_weight = mixed_turn_weight / mixed_turn_weight.sum(-1, keepdim=True).clamp(min=1e-8)

    adaptive_loss_mat = torch.zeros_like(token_losses)
    for turn_id in range(num_turns):
        turn_mask_float = (valid_mask & (turn_index == turn_id)).float()
        token_weight = mixed_turn_weight[:, turn_id].unsqueeze(1) / turn_lengths[:, turn_id].clamp(min=1).unsqueeze(1)
        adaptive_loss_mat += token_losses * turn_mask_float * token_weight

    valid_turn_count = turn_mask.sum().clamp(min=1)
    weight_entropy = -(mixed_turn_weight.clamp(min=1e-8).log() * mixed_turn_weight * turn_mask).sum(-1)
    normalized_entropy = weight_entropy / turn_counts.clamp(min=2).log()

    metrics = {
        "actor/per_turn_adaptive_lambda_mean": (lambda_turn.sum() / valid_turn_count).detach().item(),
        "actor/per_turn_adaptive_ratio_mean": (ratio.sum() / valid_turn_count).detach().item(),
        "actor/per_turn_between_var_mean": _masked_mean(between_var, turn_counts > 1).detach().item(),
        "actor/per_turn_within_var_mean": ((within_vars * turn_mask).sum() / valid_turn_count).detach().item(),
        "actor/per_turn_weight_entropy": _masked_mean(normalized_entropy, turn_counts > 1).detach().item(),
    }
    return adaptive_loss_mat, turn_counts, metrics


@register_policy_loss("grpo_per_turn_adaptive_normalized")
def compute_policy_loss_grpo_per_turn_adaptive_normalized(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    Per-turn adaptive GRPO:
        L_i = sum_k w_i,k * mean_t ell_i,k,t.

    Each turn weight w_i,k is a detached interpolation between vanilla GRPO's
    m_i,k / N_i and turn-normalized GRPO's 1 / K_i.
    """
    assert config is not None
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    token_losses, metrics = _compute_token_level_grpo_losses(
        old_log_prob, log_prob, advantages, response_mask, config, rollout_is_weights
    )

    ratio_low = float(_config_get(config, "adaptive_per_turn_ratio_low", 1.0))
    ratio_high = float(_config_get(config, "adaptive_per_turn_ratio_high", 10.0))
    adaptive_loss_mat, turn_counts, adaptive_metrics = _build_per_turn_adaptive_loss_mat(
        token_losses, response_mask, turn_index, ratio_low=ratio_low, ratio_high=ratio_high
    )

    loss = agg_loss(
        loss_mat=adaptive_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    grpo_agg_mode = _config_get(config, "adaptive_grpo_agg_mode", "seq-mean-token-mean")
    grpo_loss = agg_loss(
        loss_mat=token_losses,
        loss_mask=response_mask,
        loss_agg_mode=grpo_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )
    turn_loss_mat, _ = _build_turn_mean_loss_mat(
        token_losses, response_mask, turn_index, divide_by_turn_count=True
    )
    turn_loss = agg_loss(
        loss_mat=turn_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    seq_mask = (response_mask.float().sum(-1) > 0).float()
    seq_denom = seq_mask.sum().clamp(min=1)
    metrics.update(adaptive_metrics)
    metrics["actor/grpo_loss"] = grpo_loss.detach().item()
    metrics["actor/turn_loss"] = turn_loss.detach().item()
    metrics["actor/turn_count_mean"] = ((turn_counts * seq_mask).sum() / seq_denom).detach().item()
    return loss, metrics


def _compute_turn_variance_lambda(
    token_losses: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
    ratio_low: float,
    ratio_high: float,
) -> tuple[torch.Tensor, dict]:
    """
    Estimate whether turn averaging should reduce variance.

    The proxy uses a random-effects model x_{k,t} = u_k + eps_{k,t}.
    Turn normalization is favored when estimated between-turn variance and
    length imbalance dominate the token-noise cost of averaging turns equally.
    """
    with torch.no_grad():
        detached_losses = token_losses.detach()
        valid_mask = response_mask.bool() & (turn_index >= 0)
        valid_turn_ids = turn_index[valid_mask]
        if valid_turn_ids.numel() == 0:
            lambda_turn = torch.zeros((), device=token_losses.device, dtype=token_losses.dtype)
            metrics = {
                "actor/adaptive_turn_lambda": 0.0,
                "actor/turn_variance_ratio": 0.0,
                "actor/within_turn_loss_var": 0.0,
                "actor/between_turn_loss_var": 0.0,
                "actor/turn_length_bias_gain": 0.0,
                "actor/token_noise_cost": 0.0,
            }
            return lambda_turn, metrics

        batch_size = token_losses.shape[0]
        num_turns = int(valid_turn_ids.max().item()) + 1
        turn_lengths = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)
        turn_means = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)

        within_sum_squares = torch.zeros((), device=token_losses.device, dtype=token_losses.dtype)
        within_degrees = torch.zeros((), device=token_losses.device, dtype=token_losses.dtype)

        for turn_id in range(num_turns):
            turn_mask = valid_mask & (turn_index == turn_id)
            turn_mask_float = turn_mask.float()
            turn_len = turn_mask_float.sum(-1)
            turn_sum = (detached_losses * turn_mask_float).sum(-1)
            turn_mean = turn_sum / turn_len.clamp(min=1)

            turn_lengths[:, turn_id] = turn_len
            turn_means[:, turn_id] = turn_mean
            within_sum_squares += ((detached_losses - turn_mean.unsqueeze(1)).pow(2) * turn_mask_float).sum()
            within_degrees += (turn_len - 1).clamp(min=0).sum()

        turn_mask = (turn_lengths > 0).float()
        turn_counts = turn_mask.sum(-1)
        sequence_mask = turn_counts > 1
        turn_counts_clamped = turn_counts.clamp(min=1)
        token_counts = turn_lengths.sum(-1).clamp(min=1)

        within_var = within_sum_squares / within_degrees.clamp(min=1)

        sequence_turn_mean = (turn_means * turn_mask).sum(-1) / turn_counts_clamped
        between_sum_squares = ((turn_means - sequence_turn_mean.unsqueeze(1)).pow(2) * turn_mask).sum()
        between_degrees = (turn_counts - 1).clamp(min=0).sum()
        between_var_raw = between_sum_squares / between_degrees.clamp(min=1)

        valid_turn_count = turn_mask.sum().clamp(min=1)
        mean_inv_turn_len = (turn_mask / turn_lengths.clamp(min=1)).sum() / valid_turn_count
        between_var = (between_var_raw - within_var * mean_inv_turn_len).clamp(min=0)

        length_weight_squares = ((turn_lengths / token_counts.unsqueeze(1)).pow(2) * turn_mask).sum(-1)
        turn_length_bias_gain = (length_weight_squares - 1.0 / turn_counts_clamped).clamp(min=0)
        token_noise_cost = (
            (turn_mask / turn_lengths.clamp(min=1)).sum(-1) / turn_counts_clamped.pow(2)
            - 1.0 / token_counts
        ).clamp(min=0)

        mean_bias_gain = _masked_mean(turn_length_bias_gain, sequence_mask)
        mean_noise_cost = _masked_mean(token_noise_cost, sequence_mask)
        variance_ratio = (between_var * mean_bias_gain) / (within_var * mean_noise_cost + 1e-8)

        if ratio_high <= ratio_low:
            lambda_turn = torch.zeros((), device=token_losses.device, dtype=token_losses.dtype)
        else:
            lambda_turn = ((variance_ratio - ratio_low) / (ratio_high - ratio_low)).clamp(0.0, 1.0)

    metrics = {
        "actor/adaptive_turn_lambda": lambda_turn.detach().item(),
        "actor/turn_variance_ratio": variance_ratio.detach().item(),
        "actor/within_turn_loss_var": within_var.detach().item(),
        "actor/between_turn_loss_var": between_var.detach().item(),
        "actor/turn_length_bias_gain": mean_bias_gain.detach().item(),
        "actor/token_noise_cost": mean_noise_cost.detach().item(),
    }
    return lambda_turn, metrics



def _ratio_to_soft_gate(ratio: torch.Tensor) -> torch.Tensor:
    """No-threshold soft gate: sigmoid(log R) = R / (1 + R)."""
    ratio = ratio.clamp(min=0)
    return ratio / (1.0 + ratio)


def _compute_turn_variance_soft_lambda(
    token_losses: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
) -> tuple[torch.Tensor, dict]:
    lambda_turn, metrics = _compute_turn_variance_lambda(
        token_losses,
        response_mask,
        turn_index,
        ratio_low=0.0,
        ratio_high=1.0,
    )
    ratio = torch.tensor(
        metrics["actor/turn_variance_ratio"], device=token_losses.device, dtype=token_losses.dtype
    )
    lambda_turn = _ratio_to_soft_gate(ratio)
    metrics["actor/adaptive_turn_lambda"] = lambda_turn.detach().item()
    metrics["actor/adaptive_turn_gate"] = lambda_turn.detach().item()
    return lambda_turn, metrics


def _build_per_turn_soft_adaptive_loss_mat(
    token_losses: torch.Tensor,
    response_mask: torch.Tensor,
    turn_index: torch.Tensor,
) -> tuple[torch.Tensor, torch.Tensor, dict]:
    """
    Per-turn soft gate version with lambda_i,k = R_i,k / (1 + R_i,k).
    This avoids ratio_low / ratio_high threshold hyperparameters.
    """
    batch_size, _ = token_losses.shape
    valid_mask = response_mask.bool() & (turn_index >= 0)
    valid_turn_ids = turn_index[valid_mask]
    if valid_turn_ids.numel() == 0:
        metrics = {
            "actor/per_turn_soft_lambda_mean": 0.0,
            "actor/per_turn_soft_ratio_mean": 0.0,
            "actor/per_turn_soft_between_var_mean": 0.0,
            "actor/per_turn_soft_within_var_mean": 0.0,
            "actor/per_turn_soft_weight_entropy": 0.0,
        }
        return (
            torch.zeros_like(token_losses),
            torch.zeros(batch_size, device=token_losses.device, dtype=token_losses.dtype),
            metrics,
        )

    num_turns = int(valid_turn_ids.max().item()) + 1
    detached_losses = token_losses.detach()
    turn_lengths = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)
    turn_means = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)
    within_vars = torch.zeros(batch_size, num_turns, device=token_losses.device, dtype=token_losses.dtype)

    for turn_id in range(num_turns):
        turn_mask = valid_mask & (turn_index == turn_id)
        turn_mask_float = turn_mask.float()
        turn_len = turn_mask_float.sum(-1)
        turn_sum = (detached_losses * turn_mask_float).sum(-1)
        turn_mean = turn_sum / turn_len.clamp(min=1)
        within_sum_squares = ((detached_losses - turn_mean.unsqueeze(1)).pow(2) * turn_mask_float).sum(-1)
        within_var = within_sum_squares / (turn_len - 1).clamp(min=1)

        turn_lengths[:, turn_id] = turn_len
        turn_means[:, turn_id] = turn_mean
        within_vars[:, turn_id] = torch.where(turn_len > 1, within_var, torch.zeros_like(within_var))

    turn_mask = (turn_lengths > 0).float()
    turn_counts = turn_mask.sum(-1).clamp(min=1)
    token_counts = turn_lengths.sum(-1).clamp(min=1)

    seq_turn_mean = (turn_means * turn_mask).sum(-1) / turn_counts
    between_sum_squares = ((turn_means - seq_turn_mean.unsqueeze(1)).pow(2) * turn_mask).sum(-1)
    between_var_raw = between_sum_squares / (turn_counts - 1).clamp(min=1)
    correction = ((within_vars / turn_lengths.clamp(min=1)) * turn_mask).sum(-1) / turn_counts
    between_var = (between_var_raw - correction).clamp(min=0)

    ratio = turn_lengths * between_var.unsqueeze(1) / within_vars.clamp(min=1e-8)
    ratio = ratio * turn_mask
    lambda_turn = _ratio_to_soft_gate(ratio) * turn_mask

    grpo_turn_weight = turn_lengths / token_counts.unsqueeze(1)
    equal_turn_weight = turn_mask / turn_counts.unsqueeze(1)
    mixed_turn_weight = (1.0 - lambda_turn) * grpo_turn_weight + lambda_turn * equal_turn_weight
    mixed_turn_weight = mixed_turn_weight * turn_mask
    mixed_turn_weight = mixed_turn_weight / mixed_turn_weight.sum(-1, keepdim=True).clamp(min=1e-8)

    adaptive_loss_mat = torch.zeros_like(token_losses)
    for turn_id in range(num_turns):
        turn_mask_float = (valid_mask & (turn_index == turn_id)).float()
        token_weight = mixed_turn_weight[:, turn_id].unsqueeze(1) / turn_lengths[:, turn_id].clamp(min=1).unsqueeze(1)
        adaptive_loss_mat += token_losses * turn_mask_float * token_weight

    valid_turn_count = turn_mask.sum().clamp(min=1)
    weight_entropy = -(mixed_turn_weight.clamp(min=1e-8).log() * mixed_turn_weight * turn_mask).sum(-1)
    normalized_entropy = weight_entropy / turn_counts.clamp(min=2).log()

    metrics = {
        "actor/per_turn_soft_lambda_mean": (lambda_turn.sum() / valid_turn_count).detach().item(),
        "actor/per_turn_soft_ratio_mean": (ratio.sum() / valid_turn_count).detach().item(),
        "actor/per_turn_soft_between_var_mean": _masked_mean(between_var, turn_counts > 1).detach().item(),
        "actor/per_turn_soft_within_var_mean": ((within_vars * turn_mask).sum() / valid_turn_count).detach().item(),
        "actor/per_turn_soft_weight_entropy": _masked_mean(normalized_entropy, turn_counts > 1).detach().item(),
    }
    return adaptive_loss_mat, turn_counts, metrics


@register_policy_loss("grpo_soft_adaptive_grpo_turn_normalized")
def compute_policy_loss_grpo_soft_adaptive_grpo_turn_normalized(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-mean",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    No-threshold soft-gated trajectory-level adaptive objective:
        L = (1 - lambda) * L_grpo + lambda * L_turn,
        lambda = R / (1 + R).
    """
    assert config is not None
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    token_losses, metrics = _compute_token_level_grpo_losses(
        old_log_prob, log_prob, advantages, response_mask, config, rollout_is_weights
    )

    grpo_agg_mode = _config_get(config, "adaptive_grpo_agg_mode", "seq-mean-token-mean")
    grpo_loss = agg_loss(
        loss_mat=token_losses,
        loss_mask=response_mask,
        loss_agg_mode=grpo_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    turn_loss_mat, turn_counts = _build_turn_mean_loss_mat(
        token_losses, response_mask, turn_index, divide_by_turn_count=True
    )
    turn_loss = agg_loss(
        loss_mat=turn_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    lambda_turn, adaptive_metrics = _compute_turn_variance_soft_lambda(token_losses, response_mask, turn_index)
    loss = (1.0 - lambda_turn) * grpo_loss + lambda_turn * turn_loss

    seq_mask = (response_mask.float().sum(-1) > 0).float()
    seq_denom = seq_mask.sum().clamp(min=1)
    metrics.update(adaptive_metrics)
    metrics["actor/grpo_loss"] = grpo_loss.detach().item()
    metrics["actor/turn_loss"] = turn_loss.detach().item()
    metrics["actor/turn_count_mean"] = ((turn_counts * seq_mask).sum() / seq_denom).detach().item()
    return loss, metrics


@register_policy_loss("grpo_per_turn_soft_adaptive_normalized")
def compute_policy_loss_grpo_per_turn_soft_adaptive_normalized(
    old_log_prob: torch.Tensor,
    log_prob: torch.Tensor,
    advantages: torch.Tensor,
    response_mask: torch.Tensor,
    loss_agg_mode: str = "seq-mean-token-sum",
    config=None,
    rollout_is_weights=None,
    **kwargs,
) -> tuple[torch.Tensor, dict]:
    """
    No-threshold per-turn soft-gated adaptive objective:
        lambda_i,k = R_i,k / (1 + R_i,k).
    """
    assert config is not None
    turn_index = config.global_batch_info.get("turn_index", None)
    if turn_index is None:
        return compute_policy_loss_vanilla(
            old_log_prob, log_prob, advantages, response_mask, loss_agg_mode, config, rollout_is_weights
        )

    turn_index = turn_index.to(log_prob.device)
    token_losses, metrics = _compute_token_level_grpo_losses(
        old_log_prob, log_prob, advantages, response_mask, config, rollout_is_weights
    )
    adaptive_loss_mat, turn_counts, adaptive_metrics = _build_per_turn_soft_adaptive_loss_mat(
        token_losses, response_mask, turn_index
    )

    loss = agg_loss(
        loss_mat=adaptive_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    grpo_agg_mode = _config_get(config, "adaptive_grpo_agg_mode", "seq-mean-token-mean")
    grpo_loss = agg_loss(
        loss_mat=token_losses,
        loss_mask=response_mask,
        loss_agg_mode=grpo_agg_mode,
        **get_agg_loss_kwargs(config.global_batch_info),
    )
    turn_loss_mat, _ = _build_turn_mean_loss_mat(
        token_losses, response_mask, turn_index, divide_by_turn_count=True
    )
    turn_loss = agg_loss(
        loss_mat=turn_loss_mat,
        loss_mask=response_mask,
        loss_agg_mode="seq-mean-token-sum",
        **get_agg_loss_kwargs(config.global_batch_info),
    )

    seq_mask = (response_mask.float().sum(-1) > 0).float()
    seq_denom = seq_mask.sum().clamp(min=1)
    metrics.update(adaptive_metrics)
    metrics["actor/grpo_loss"] = grpo_loss.detach().item()
    metrics["actor/turn_loss"] = turn_loss.detach().item()
    metrics["actor/turn_count_mean"] = ((turn_counts * seq_mask).sum() / seq_denom).detach().item()
    return loss, metrics
