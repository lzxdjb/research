# Experimental Dual-Agent PPO/GRPO

This folder is intentionally independent from the normal `verl.trainer.main_ppo`
path. It prototypes one trainer process that owns two trainable actor-rollout
models:

- `actor_a_rollout_ref`
- `actor_b_rollout_ref`

The trainer allocates two Ray GPU pools, starts one hybrid rollout manager per
actor, generates A/B turns in the same rollout phase, updates both actors
concurrently, then synchronizes both rollout replicas before the next generation
phase.

## Current Contract

One training step is:

1. Start from a dataset `raw_prompt`.
2. Agent A generates a turn.
3. Agent B receives A's text and generates a turn.
4. Repeat for `dual_agent.rollout.max_turns`.
5. Build two `DataProto` views:
   - A view masks A tokens with `response_mask=1` and B tokens with `0`.
   - B view masks B tokens with `response_mask=1` and A tokens with `0`.
6. Compute reward separately for each view.
7. Move terminal reward to the last trainable token for that actor.
8. Compute advantages separately.
9. Run `update_actor` for A and B in parallel threads.
10. Call both checkpoint engines' `update_weights` before the next rollout.

## Run

```bash
python3 -m verl.experimental.dual_agent.main_ppo \
  data.train_files=/path/to/train.parquet \
  data.val_files=/path/to/val.parquet \
  actor_a_rollout_ref.model.path=/path/to/agent-a \
  actor_b_rollout_ref.model.path=/path/to/agent-b \
  dual_agent.actor_a.n_gpus_per_node=4 \
  dual_agent.actor_b.n_gpus_per_node=4
```

Use the same actor/rollout overrides you normally use, but prefix them with
`actor_a_rollout_ref` or `actor_b_rollout_ref`.

## Important Limits

This is a first experimental implementation, not a drop-in replacement for all
PPO modes.

- Designed for critic-free GRPO/REINFORCE-style training.
- Requires KL/ref-policy disabled for both actors in this first pass.
- Text-only turn exchange; tool/multimodal dual-agent rollouts need a custom
  dual rollout worker.
- Checkpoint saving/resume should be added after the training loop is validated
  on a small local job.
