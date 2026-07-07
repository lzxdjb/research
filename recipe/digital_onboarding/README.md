# Digital Onboarding Service RL

Current goal: train **only the service model**.

The customer simulator and reward judge are frozen OpenAI-compatible servers. In the simplest setup, one server plays both roles because each request sends a different system prompt.

## Procedure

1. Build seed account-opening questions/scenarios.
2. Start or point to the customer/reward server.
3. Run multi-turn GRPO for the service model.

## Machine Layout

Recommended layout:

- Server machine: runs frozen vLLM and exposes an HTTP endpoint.
- Training machine: runs VERL/Megatron and trains only the service model.
- By default, each script uses **all GPUs visible on its own machine**.
- To restrict GPUs, set `SERVER_GPUS` on the server script or `TRAIN_GPUS` on the training script.

The server must expose an OpenAI-compatible address, for example:

```text
http://127.0.0.1:8002/v1/chat/completions
```

If the server runs on another machine, use that machine's IP:

```text
http://<server-ip>:8002/v1/chat/completions
```

## Quick Test Server

Use this for a cheap smoke test. It defaults to `/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B`.

Run this on the **server machine**. The script is standalone; it does not need the training process.

```bash
TEACHER_HOST=0.0.0.0 \
TEACHER_PORT=8002 \
PUBLIC_HOST=<server-ip-visible-from-training-machine> \
TEACHER_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
TEACHER_MODEL_NAME=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
bash run_script/start_digital_onboarding_4b_multirole_server.sh
```

After vLLM is ready, the script prints the exact `CLIENT_REWARD_ENDPOINT` and `CLIENT_REWARD_MODEL_NAME` to paste into the training command.

## Quick Test Training

Use a tiny seed set and one training epoch:

```bash
cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect

CLIENT_REWARD_ENDPOINT=http://<server-ip>:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
bash run_script/run_digital_onboarding_service_quick_test.sh
```

## Real Bank Tool Backend

By default, `tools.py` uses the synthetic onboarding environment. To call the
real `open-account/scripts/api.py` mock-bank backend during rollouts, set:

```bash
DIGITAL_ONBOARDING_TOOL_BACKEND=real_bank
```

The data builder will stamp each row with `real_bank` tool/interaction metadata,
and the tool runtime derives a unique per-rollout trajectory id from
`scenario_id + request_id`. That trajectory id is used for the local
open-account session file, so concurrent trajectories do not overwrite each
other's local auth/token state. The generated hidden profile also receives a
unique fake phone, email, and SSN. The remote mock backend can still share state
if it maps different fake contacts to the same account internally; for strict
isolation, provide unique test accounts or a backend reset endpoint.

Example quick run:

```bash
cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect

DIGITAL_ONBOARDING_TOOL_BACKEND=real_bank \
DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES=1 \
DIGITAL_ONBOARDING_BANK_REWARD_WEIGHT=0.5 \
DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES=0 \
REWARD_MAX_TOKENS=4096 \
CUSTOMER_MAX_TOKENS=2048 \
MAX_RESPONSE_LENGTH=20000 \
CLIENT_REWARD_ENDPOINT=http://10.244.200.11:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
bash run_script/run_digital_onboarding_service_quick_test.sh
```

The model reward is still used, but `model_reward_function.py` now also blends
in bank-return signals from tool responses. For finishable real-bank runs, the
bank-rule signal is binary and only rewards a trajectory when the current
submission attempt succeeds. Tune the blend with:

```bash
DIGITAL_ONBOARDING_BANK_REWARD_ENABLED=1
DIGITAL_ONBOARDING_BANK_REWARD_WEIGHT=0.5
```

Smoke test the real-bank branch without network:

```bash
python3 -m recipe.digital_onboarding.scripts.test_real_bank_tool_backend
```

To call the actual mock-bank API in the smoke test:

```bash
DIGITAL_ONBOARDING_TOOL_BACKEND=real_bank \
python3 -m recipe.digital_onboarding.scripts.test_real_bank_tool_backend --live
```

## Formal Server

Use this for the real customer/reward server, normally your 122B-A10B model.

Run this on the **server machine**. The script is standalone; it does not need the training process.

```bash
TEACHER_HOST=0.0.0.0 \
TEACHER_PORT=8002 \
PUBLIC_HOST=<server-ip-visible-from-training-machine> \
TEACHER_MODEL_PATH=/path/to/your/122B-A10B \
TEACHER_MODEL_NAME=/path/to/your/122B-A10B \
bash run_script/start_digital_onboarding_122b_multirole_server.sh
```

After vLLM is ready, the script prints the exact `CLIENT_REWARD_ENDPOINT` and `CLIENT_REWARD_MODEL_NAME` to paste into the training command.

Optional subset:

```bash
SERVER_GPUS=0,1,2,3 bash run_script/start_digital_onboarding_122b_multirole_server.sh
```

This one server can act as both:

- customer simulator: called by `interactions.py` with the customer system prompt;
- reward judge: called by `model_reward_function.py` with the reward system prompt.

The model knows the role from the request's system prompt, not from the server. The server script prints the endpoint to use from the training machine:

```bash
CLIENT_REWARD_ENDPOINT=http://<server-ip>:8002/v1/chat/completions
CLIENT_REWARD_MODEL_NAME=/path/to/your/122B-A10B
```

## Formal Training

Use larger seed data and longer service-model training:

```bash
cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect

CLIENT_REWARD_ENDPOINT=http://<server-ip>:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/path/to/your/122B-A10B \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
bash run_script/run_digital_onboarding_service_formal_train.sh
```

Useful overrides:

```bash
TRAIN_SIZE=1024
VAL_SIZE=128
TRAIN_BATCH_SIZE=32
ROLLOUT_N=2
TOTAL_EPOCHS=5
MAX_RESPONSE_LENGTH=8192
CUSTOMER_MAX_TOKENS=768
```

There is no default service/customer turn cap in the quick or formal training
scripts. The rollout stops when `MAX_RESPONSE_LENGTH` is exhausted, or when the
customer simulator emits the termination signal. If you need an emergency
customer-side guard for debugging, set `CUSTOMER_MAX_USER_TURNS=40`.

Optional subset:

```bash
TRAIN_GPUS=0,1,2,3 bash run_script/run_digital_onboarding_service_formal_train.sh
```

## Debug Logs

Each training script prints and writes these files under `rollout_log/$JOB_NAME/` by default:

- `debug_trace/*.csv`: per-trajectory debug CSV files with service-model raw output/thinking, parsed tool calls, tool responses, simulator raw output/thinking and final user response, and reward-judge raw output/parsed score.
- `*.jsonl`: VERL rollout trajectory logs.
- `customer_turns.jsonl`: simulator-user request details in JSONL form.
- `reward_judge.jsonl`: reward-model judge details in JSONL form.

The default tool parser is `hermes`, matching the project's `<tool_call>{"name": ..., "arguments": ...}</tool_call>` format.

Debug trace switch:

```bash
# quick script default: on
DEBUG_TRACE=0 bash run_script/run_digital_onboarding_service_quick_test.sh

# formal script default: off
DEBUG_TRACE=1 bash run_script/run_digital_onboarding_service_formal_train.sh
```

Stdout verbosity switches:

```bash
# default: off; enables full per-turn prompt/response/tool dumps
VERL_AGENT_LOOP_DEBUG_STDOUT=1 bash run_script/run_digital_onboarding_service_quick_test.sh

# default: off; echoes every shell command in the launch script
DIGITAL_ONBOARDING_SHELL_TRACE=1 bash run_script/run_digital_onboarding_service_quick_test.sh
```

## Trajectory Prefix RL Data

You can also mine completed service/customer trajectories into more RL prompts.
For a trajectory with service turns 1, 2, and 3, the builder creates prompts at
each assistant boundary:

- prefix before turn 1;
- prefix before turn 2, with turn 1 service/customer/tool messages in context;
- prefix before turn 3, with turns 1 and 2 in context.

Previous service messages are part of the prompt, so VERL masks them naturally.
The row also carries:

- `extra_info.reward_prefix`, so the reward model judges prefix + new continuation;
- `tools_kwargs.__onboarding_state__`, so tool state resumes consistently from a mid-dialog prefix;
- a selection reason and source score for auditing.

Build prefix data from rollout logs:

```bash
python3 -m recipe.digital_onboarding.scripts.build_service_prefix_rl_data \
  --input rollout_log/digital_onboarding_service_quick_test \
  --output-dir data/digital_onboarding/service_prefix_rl \
  --selection-policy useful \
  --min-score 0.0 \
  --max-prefix-turn 3
```

Useful selection modes:

- `useful`: keep meaningful, non-repeated prefixes whose trajectory score is at least `--min-score`.
- `successful`: keep prefixes only from trajectories above `--success-score`.
- `recoverable`: keep harder prefixes with scores between `--min-score` and `--success-score`.
- `all`: keep every parsed assistant boundary.

For long trajectories, prefer `--max-prefix-turn 3` to start with short
prefixes; increase `MAX_PROMPT_LENGTH` in the training script if you mine deeper
prefixes.

Train from the selected prefix data by overriding the train and val files:

```bash
CLIENT_REWARD_ENDPOINT=http://<server-ip>:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/path/to/your/122B-A10B \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
TRAIN_FILE=data/digital_onboarding/service_prefix_rl/service_prefix_rl_train.parquet \
VAL_FILE=data/digital_onboarding/service_prefix_rl/service_prefix_rl_val.parquet \
bash run_script/run_digital_onboarding_service_formal_train.sh
```

One frozen vLLM server is enough for the **customer simulator + reward judge**
because each request sends a different system prompt.  The service model still
needs its own rollout/training process.  If you want to collect trajectories
with a separately served fixed service model, use two model endpoints or two
machines, then point this builder at the resulting JSONL logs.

## Separate Customer And Reward Servers

If later you want two different frozen servers, set separate endpoints:

```bash
CLIENT_ENDPOINT=http://127.0.0.1:8000/v1/chat/completions \
CLIENT_MODEL=/path/to/customer-model \
REWARD_ENDPOINT=http://127.0.0.1:8001/v1/chat/completions \
REWARD_MODEL=/path/to/reward-model \
bash run_script/run_digital_onboarding_service_formal_train.sh
```

## Files

- `scripts/build_data.py`: creates seed service scenarios.
- `scripts/build_service_prefix_rl_data.py`: mines rollout trajectories into selected service-prefix RL prompts.
- `interactions.py`: calls the customer simulator endpoint.
- `model_reward_function.py`: calls the reward judge endpoint.
- `tools.py`: tool environment and account-opening backend state.
- `run_script/start_digital_onboarding_4b_multirole_server.sh`: quick-test customer/reward server.
- `run_script/run_digital_onboarding_service_quick_test.sh`: quick-test service training.
- `run_script/start_digital_onboarding_122b_multirole_server.sh`: formal customer/reward server.
- `run_script/run_digital_onboarding_service_formal_train.sh`: formal service training.
