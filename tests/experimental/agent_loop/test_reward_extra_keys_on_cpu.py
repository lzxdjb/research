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

import numpy as np
import torch

from verl.experimental.agent_loop.agent_loop import AgentLoopManager
from verl.protocol import DataProto


def test_agent_loop_manager_normalizes_reward_extra_keys_before_concat():
    worker_0_output = DataProto.from_dict(
        tensors={"rm_scores": torch.ones(2, 3)},
        non_tensors={"acc": np.array([1.0, 0.0], dtype=object)},
        meta_info={"reward_extra_keys": ["acc"]},
    )
    worker_1_output = DataProto.from_dict(
        tensors={"rm_scores": torch.ones(2, 3)},
        non_tensors={"f1": np.array([0.5, 1.0], dtype=object)},
        meta_info={"reward_extra_keys": ["f1"]},
    )
    outputs = [worker_0_output, worker_1_output]

    AgentLoopManager._normalize_reward_extra_keys(outputs)
    result = DataProto.concat(outputs)

    assert result.meta_info["reward_extra_keys"] == ["acc", "f1"]
    assert result.non_tensor_batch["acc"].tolist() == [1.0, 0.0, None, None]
    assert result.non_tensor_batch["f1"].tolist() == [None, None, 0.5, 1.0]
