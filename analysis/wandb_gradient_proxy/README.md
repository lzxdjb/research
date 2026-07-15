# W&B gradient-proxy analysis

Run: `zhengxinglei539-easynet/wiki/9z0osom2`

Name: `wiki_turn_ppo_geo_critic_turn_suffix_wi_turn_level_value`

The run logged 226 paired training steps before crashing. It did not log the
paper's single-turn `W_n` directly. All steps have
`mode_is_turn_suffix_score_norm=1` and `mode_is_turn_score_norm=0`. Therefore
`critic/value_loss_weight/raw_mean` is the turn-suffix weight
`S_i = sum_{j>=i} rho^(j-i) W_j`, aggregated after broadcasting it to tokens.
The true batch actor gradient norm is `actor/grad_norm`.

## Main result

The run's logged turn-suffix proxy is not an effective direct estimator of the
full actor gradient norm:

- Pearson correlation: -0.813 (bootstrap 95% CI: [-0.852, -0.767])
- Spearman correlation: -0.689
- Correlation after removing linear training-step trends: -0.590
- Correlation of first differences: -0.119
- Positive-scale fit on `sqrt(raw_mean)`: MAPE 23.9%, NRMSE 25.5%, R2 -0.373

An unconstrained affine fit reaches R2 0.669 and MAPE 10.4%, but its slope is
negative. That contradicts the intended interpretation that larger score
energy should imply a larger norm, so it is not a meaningful proxy calibration.

The logged proxy is strongly confounded with trajectory structure: its Pearson
correlation is 0.967 with mean response length and 0.983 with mean turn count.

## Interpretation limits

`raw_mean` is a token-weighted batch aggregation. The implementation computes
turn-suffix `S_i`, broadcasts it to every token in that turn, and then takes a
masked token mean. It is not a per-turn `W_n` observation. The history also
contains mean token `q`, but not the per-token/per-turn data needed to recover
every `W_n`.

`actor/grad_norm` is the pre-clipping parameter-gradient norm of the full PPO
batch objective. Besides policy score energy, it includes advantages, PPO
ratios and clipping, KL loss, model Jacobians, and cross-sample vector
cancellation. The paper's `W_n` only approximates a squared policy-score norm.

This run did not log the already-implemented `variance_proxy/expected_w` metric
or per-turn parameter-gradient norms. Therefore the absolute error between
`W_n` and the oracle per-turn score norm cannot be recovered from this W&B
history. A new instrumented run is required for that comparison.

## Files

- `analysis.json`: machine-readable statistics and limitations
- `paired_turn_proxy.csv`: aligned values and calibrated predictions
- `9z0osom2_history.csv`: complete downloaded W&B history
- `run_inventory.json`: run identity and metric inventory
