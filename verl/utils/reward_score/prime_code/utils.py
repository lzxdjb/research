# Copyright 2024 PRIME team and/or its affiliates
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

# Borrowed from: https://huggingface.co/spaces/codeparrot/apps_metric/blob/main/utils.py

import multiprocessing
import os
import sys
import time
import traceback
from typing import Optional

from .testing_util import run_test


def _temp_run(sample, generation, debug, result_conn, timeout):
    with open(os.devnull, "w") as devnull:
        sys.stdout = devnull
        sys.stderr = devnull
        try:
            res, metadata = run_test(in_outs=sample, test=generation, debug=debug, timeout=timeout)
        except Exception:
            # print(e) # some tracebacks are extremely long.
            traceback.print_exc(10)
            res = [-1 for i in range(len(sample["inputs"]))]
            metadata = {}
        result_conn.send((res, [metadata]))
        result_conn.close()


def check_correctness(in_outs: Optional[dict], generation, timeout=10, debug=True):
    """Check correctness of code generation with a global timeout.
    The global timeout is to catch some extreme/rare cases not handled by the timeouts
    inside `run_test`"""

    result_conn, child_conn = multiprocessing.Pipe(duplex=False)
    p = multiprocessing.Process(target=_temp_run, args=(in_outs, generation, debug, child_conn, timeout))
    p.start()
    child_conn.close()
    result = None
    metadata_list = None
    deadline = time.monotonic() + timeout + 1
    while time.monotonic() < deadline:
        if result_conn.poll(0.05):
            result, metadata_list = result_conn.recv()
            break
        if not p.is_alive():
            p.join()
            break

    if p.is_alive():
        p.kill()
        p.join()
    elif result is not None:
        p.join()

    if result is None and result_conn.poll():
        result, metadata_list = result_conn.recv()

    if result is None:
        # consider that all tests failed
        result = [-1 for i in range(len(in_outs["inputs"]))]
        metadata_list = [{}]
        if debug:
            print("global timeout")
    result_conn.close()
    return result, metadata_list
