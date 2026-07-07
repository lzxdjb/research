# HDL Agent Recipe

This recipe adds a local HDL agent loop and reward path for RL training.

## Components

- `verl.experimental.agent_loop.hdl_agent_loop.HDLAgentLoop`: iterative
  generate, judge, revise rollout loop registered as `hdl_agent`.
- `recipe/hdl_agent/hdl_judge.py`: deterministic local judge using
  `hdl_env/env.sh` for Slang, Verilator, Yosys, Maven, and Chisel.
- `recipe/hdl_agent/reward_function.py`: VERL custom reward entry point.
- `recipe/hdl_agent/build_smoke_dataset.py`: tiny HDLBits-style dataset used
  to validate the training workflow.

## Build Data

```bash
python recipe/hdl_agent/build_smoke_dataset.py --output-dir data/hdl_agent_smoke
```

## Validate Judge

```bash
python - <<'PY'
import json
from pathlib import Path
from recipe.hdl_agent.hdl_judge import compute_hdl_score

row = json.loads(Path('data/hdl_agent_smoke/reference_solutions.jsonl').read_text().splitlines()[0])
print(compute_hdl_score(solution_str=row['solution'], ground_truth=row['ground_truth']))
PY
```

## Train

```bash
bash run_script/reflect_demo_hdl.sh
```

Important environment variables:

- `MODEL_PATH`: base model path.
- `HDL_ENV_SH`: isolated HDL environment, defaults to `hdl_env/env.sh`.
- `HDL_AGENT_MAX_ROUNDS`: number of generate/judge attempts per rollout.
- `HDL_AGENT_TIMEOUT`: per-tool judge timeout in seconds.
- `VERIFY_HDL_ENV=0`: skip the HDL toolchain smoke test at script startup.

## Public Benchmark Path

For a lightweight public scale-up benchmark, use VerilogEval first. It is a
better fit for automated local judging than HDLBits because tasks are already
packaged as benchmark data. HDLBits remains useful as an online source of small
human-written tasks, but it does not provide the same stable offline API.

