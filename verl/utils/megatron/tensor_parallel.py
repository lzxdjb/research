# Copyright 2024 Bytedance Ltd. and/or its affiliates
# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.
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
Utilities for using tensor_parallel in megatron
"""

from typing import TYPE_CHECKING

import torch
import torch.distributed as dist
from megatron.core import parallel_state as mpu
from torch.nn import init

if TYPE_CHECKING:
    from megatron.core import ModelParallelConfig


def update_kwargs_with_config(dictionary: dict, config: "ModelParallelConfig"):
    dictionary["config"] = config
    return dictionary


def get_default_kwargs_for_model_parallel_config():
    model_parallel_config_kwargs = {
        "params_dtype": torch.float32,
        "use_cpu_initialization": False,
        "perform_initialization": True,
        "gradient_accumulation_fusion": False,
        "sequence_parallel": False,
    }
    return model_parallel_config_kwargs


def get_default_model_parallel_config():
    from megatron.core import ModelParallelConfig

    return ModelParallelConfig(**get_default_kwargs_for_model_parallel_config())


def get_common_default_kwargs_for_parallel_linear():
    default_model_parallel_config = get_default_model_parallel_config()
    common_default_kwargs = {
        "init_method": init.xavier_normal_,
        "stride": 1,
        "keep_master_weight_for_test": False,
        "config": default_model_parallel_config,
    }
    return common_default_kwargs


def get_default_kwargs_for_column_parallel_linear():
    from megatron.core import ModelParallelConfig

    model_parallel_config_kwargs = get_default_kwargs_for_model_parallel_config()
    column_parallel_config_kwargs = {
        "async_tensor_model_parallel_allreduce": False,
    }
    model_parallel_config_kwargs.update(column_parallel_config_kwargs)
    column_default_kwargs = {
        "config": ModelParallelConfig(**model_parallel_config_kwargs),
    }
    common_default_kwargs = get_common_default_kwargs_for_parallel_linear()
    common_default_kwargs.update(column_default_kwargs)
    return common_default_kwargs


def get_default_kwargs_for_row_parallel_linear():
    common_default_kwargs = get_common_default_kwargs_for_parallel_linear()
    return common_default_kwargs


def get_default_kwargs_for_parallel_embedding():
    from megatron.core import ModelParallelConfig

    model_parallel_config_kwargs = get_default_kwargs_for_model_parallel_config()
    embedding_default_kwargs = {
        "init_method": init.xavier_normal_,
        "config": ModelParallelConfig(**model_parallel_config_kwargs),
    }
    return embedding_default_kwargs


def is_tensor_parallel_param(param):
    return hasattr(param, "tensor_model_parallel") and param.tensor_model_parallel


def get_tensor_parallel_partition_dim(param):
    assert is_tensor_parallel_param(param)
    return param.partition_dim


def get_tensor_parallel_partition_stride(param):
    assert is_tensor_parallel_param(param)
    return param.partition_stride


class _VocabParallelEntropy(torch.autograd.Function):
    @staticmethod
    def forward(ctx, vocab_parallel_logits: torch.Tensor) -> torch.Tensor:
        @torch.compile(dynamic=True)
        def mul_reduce(a, b):
            return (a * b).sum(dim=-1, keepdim=True)

        logits_max = vocab_parallel_logits.max(dim=-1, keepdim=True).values
        dist.all_reduce(logits_max, op=dist.ReduceOp.MAX, group=mpu.get_tensor_model_parallel_group())
        normalized_vocab_parallel_logits = vocab_parallel_logits - logits_max
        normalized_exp_logits = normalized_vocab_parallel_logits.exp_()
        normalized_sum_exp_logits = normalized_exp_logits.sum(dim=-1, keepdim=True)
        dist.all_reduce(normalized_sum_exp_logits, group=mpu.get_tensor_model_parallel_group())
        softmax_logits = normalized_exp_logits.div_(normalized_sum_exp_logits)
        sum_softmax_times_logits = mul_reduce(softmax_logits, vocab_parallel_logits)
        dist.all_reduce(sum_softmax_times_logits, group=mpu.get_tensor_model_parallel_group())
        entropy = logits_max + normalized_sum_exp_logits.log() - sum_softmax_times_logits
        ctx.save_for_backward(vocab_parallel_logits, softmax_logits, sum_softmax_times_logits)
        return entropy.squeeze(dim=-1)

    @staticmethod
    def backward(ctx, grad_output: torch.Tensor) -> torch.Tensor:
        vocab_parallel_logits, softmax_logits, sum_softmax_times_logits = ctx.saved_tensors
        # reuse softmax_logits as grad
        vocab_parallel_logits.sub_(sum_softmax_times_logits)
        softmax_logits.mul_(vocab_parallel_logits)
        softmax_logits.mul_(grad_output.unsqueeze(dim=-1))
        # recover vocab_parallel_logits
        vocab_parallel_logits.add_(sum_softmax_times_logits)
        softmax_logits.mul_(-1)
        return softmax_logits


def vocab_parallel_entropy(vocab_parallel_logits: torch.Tensor) -> torch.Tensor:
    """Compute entropy when the logits are sharded in tp ranks

    Args:
        vocab_parallel_logits: (total_nnz, vocab_size // tp_size)

    Returns: (total_nnz,)

    """
    return _VocabParallelEntropy.apply(vocab_parallel_logits)


def vocab_parallel_log_probs_from_logits(logits, labels):
    """TODO(zhangchi.usc1992): We may change the implementation later"""
    from megatron.core import tensor_parallel

    return -tensor_parallel.vocab_parallel_cross_entropy(vocab_parallel_logits=logits, target=labels)


def vocab_parallel_sum_pi_squared_from_logits(vocab_parallel_logits: torch.Tensor) -> torch.Tensor:
    """Compute sum(pi^2) when vocab logits are sharded over tensor-parallel ranks."""
    logits_max = vocab_parallel_logits.max(dim=-1, keepdim=True).values
    dist.all_reduce(logits_max, op=dist.ReduceOp.MAX, group=mpu.get_tensor_model_parallel_group())

    shifted_logits = (vocab_parallel_logits - logits_max).to(torch.float32)
    sum_exp = shifted_logits.exp().sum(dim=-1, keepdim=True)
    sum_exp_squared = (2.0 * shifted_logits).exp().sum(dim=-1, keepdim=True)
    dist.all_reduce(sum_exp, group=mpu.get_tensor_model_parallel_group())
    dist.all_reduce(sum_exp_squared, group=mpu.get_tensor_model_parallel_group())

    return (sum_exp_squared / sum_exp.square()).squeeze(dim=-1)


def _count_sketch_bucket_and_sign(ids: torch.Tensor, sketch_dim: int, seed: int) -> tuple[torch.Tensor, torch.Tensor]:
    ids = ids.to(torch.long)
    modulus = 2_147_483_647
    bucket_hash = torch.remainder(ids * 1_103_515_245 + 12_345 + int(seed), modulus)
    sign_hash = torch.remainder(ids * 134_775_813 + 1 + int(seed) * 17, 2)
    bucket = torch.remainder(bucket_hash, int(sketch_dim)).to(torch.long)
    sign = sign_hash.to(torch.float32).mul_(2.0).sub_(1.0)
    return bucket, sign


def vocab_parallel_score_sketch_from_logits(
    vocab_parallel_logits: torch.Tensor,
    labels: torch.Tensor,
    sketch_dim: int,
    seed: int = 17,
) -> torch.Tensor:
    """Sketch the categorical score vector ``e_y - pi`` under tensor-parallel logits.

    The returned tensor has shape ``logits.shape[:-1] + (sketch_dim,)`` and is
    an unbiased CountSketch-style low-dimensional proxy for the vocabulary-side
    score direction.
    """
    if sketch_dim <= 0:
        raise ValueError(f"sketch_dim must be positive, got {sketch_dim}")

    original_shape = vocab_parallel_logits.shape[:-1]
    local_vocab_size = int(vocab_parallel_logits.shape[-1])
    flat_logits = vocab_parallel_logits.reshape(-1, local_vocab_size)
    flat_labels = labels.reshape(-1).to(device=vocab_parallel_logits.device, dtype=torch.long)

    logits_max = flat_logits.max(dim=-1, keepdim=True).values
    dist.all_reduce(logits_max, op=dist.ReduceOp.MAX, group=mpu.get_tensor_model_parallel_group())
    shifted_logits = (flat_logits - logits_max).to(torch.float32)
    exp_logits = shifted_logits.exp()
    sum_exp = exp_logits.sum(dim=-1, keepdim=True)
    dist.all_reduce(sum_exp, group=mpu.get_tensor_model_parallel_group())
    probs = exp_logits / sum_exp.clamp_min(1e-20)

    tp_rank = mpu.get_tensor_model_parallel_rank()
    vocab_start = tp_rank * local_vocab_size
    local_vocab_ids = torch.arange(
        vocab_start,
        vocab_start + local_vocab_size,
        device=vocab_parallel_logits.device,
        dtype=torch.long,
    )
    local_bucket, local_sign = _count_sketch_bucket_and_sign(local_vocab_ids, sketch_dim, seed)
    local_weighted_probs = probs * local_sign.unsqueeze(0).to(probs.dtype)

    sketch_pi = torch.zeros(
        flat_logits.shape[0],
        int(sketch_dim),
        device=vocab_parallel_logits.device,
        dtype=torch.float32,
    )
    sketch_pi.scatter_add_(1, local_bucket.unsqueeze(0).expand(flat_logits.shape[0], -1), local_weighted_probs)
    dist.all_reduce(sketch_pi, group=mpu.get_tensor_model_parallel_group())

    label_bucket, label_sign = _count_sketch_bucket_and_sign(flat_labels, sketch_dim, seed)
    sketch_taken = torch.zeros_like(sketch_pi)
    sketch_taken.scatter_add_(1, label_bucket.unsqueeze(-1), label_sign.unsqueeze(-1).to(sketch_taken.dtype))

    score_sketch = sketch_taken - sketch_pi
    return score_sketch.reshape(*original_shape, int(sketch_dim))


def vocab_parallel_log_probs_from_logits_response_rmpad(input_ids, attention_mask, logits_rmpad, response_length):
    """Similar to log_probs_from_logits_response_rmpad, but the logits_rmpad is now spliited across tensor parallel
    region.
    This will further reduce the peak memory usage during training

    Args:
        input_ids: [batch_size, seqlen]
        attention_mask: [batch_size, seqlen]
        logits_rmpad: [total_nnz, vocab_size // tp_size]
        response_length: int

    """
    from flash_attn.bert_padding import pad_input, unpad_input

    batch_size, seqlen = input_ids.shape
    input_ids_rmpad, indices, *_ = unpad_input(input_ids.unsqueeze(-1), attention_mask=attention_mask)
    input_ids_rmpad = input_ids_rmpad.squeeze(-1)
    input_ids_rmpad_rolled = torch.roll(input_ids_rmpad, shifts=-1, dims=0)
    full_log_probs_rmpad = vocab_parallel_log_probs_from_logits(
        logits=logits_rmpad, labels=input_ids_rmpad_rolled
    )  # (total_nnz,)
    full_output = pad_input(
        hidden_states=full_log_probs_rmpad.unsqueeze(-1), indices=indices, batch=batch_size, seqlen=seqlen
    )
    output = full_output.squeeze(-1)[:, -response_length - 1 : -1]  # [batch_size, response_length]
    return output
