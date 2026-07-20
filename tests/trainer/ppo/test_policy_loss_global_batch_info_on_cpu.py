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

from verl.trainer.ppo.core_algos import compute_policy_loss_gspo, get_agg_loss_kwargs
from verl.workers.config import ActorConfig


def test_get_agg_loss_kwargs_excludes_algorithm_metadata():
    turn_index = torch.tensor([[0, 0, 1]])
    global_batch_info = {
        "dp_size": 1,
        "batch_num_tokens": None,
        "global_batch_size": None,
        "loss_scale_factor": None,
        "turn_index": turn_index,
        "seeupo_seg_mask": torch.ones_like(turn_index),
    }

    assert get_agg_loss_kwargs(global_batch_info) == {
        "dp_size": 1,
        "batch_num_tokens": None,
        "global_batch_size": None,
        "loss_scale_factor": None,
    }


def test_gspo_ignores_turn_index_when_aggregating_loss():
    config = ActorConfig(
        strategy="fsdp",
        rollout_n=1,
        ppo_micro_batch_size_per_gpu=1,
        loss_agg_mode="seq-mean-token-mean",
    )
    config.global_batch_info = {
        "dp_size": 1,
        "batch_num_tokens": None,
        "global_batch_size": None,
        "loss_scale_factor": None,
        "turn_index": torch.tensor([[0, 0, 1], [0, 1, -1]]),
    }

    old_log_prob = torch.zeros((2, 3))
    log_prob = torch.zeros((2, 3), requires_grad=True)
    advantages = torch.tensor([[1.0, 1.0, -1.0], [-1.0, 1.0, 0.0]])
    response_mask = torch.tensor([[1, 1, 1], [1, 1, 0]], dtype=torch.bool)

    loss, metrics = compute_policy_loss_gspo(
        old_log_prob=old_log_prob,
        log_prob=log_prob,
        advantages=advantages,
        response_mask=response_mask,
        config=config,
    )

    assert loss.ndim == 0
    assert torch.isfinite(loss)
    assert set(metrics) == {"actor/pg_clipfrac", "actor/ppo_kl", "actor/pg_clipfrac_lower"}
