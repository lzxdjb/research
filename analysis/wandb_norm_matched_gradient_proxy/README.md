# W&B gradient-proxy analysis

Run: `zhengxinglei539-easynet/wiki_9B/s0mzclga`

Name: `vanilla_norm_matched_alpha_grpo_9B`

Snapshot: 393 paired training steps (`training/global_step=1..393`). The run was
still running when this snapshot was downloaded.

## What was logged

The run config has `calculate_sum_pi_squared=true`, and
`actor/intentional_used_sum_pi_squared` equals 1 at every paired step. The
logged token proxy is therefore the full categorical logit-score energy

    actor/intentional_score_norm_mean
      = mean_tokens[1 - 2*pi(y) + sum_v pi(v)^2]
      = mean_tokens[||e_y - pi||_2^2].

The trajectory aggregation is `variance_proxy/expected_w`, and the
advantage-weighted proxy is `variance_proxy/proxy2_total_power`.

The available reference is `actor/grad_norm`. In this Megatron worker it is
the mean of the PPO mini-batch parameter-gradient norms in one global step.
It is not a per-token score norm, a per-trajectory gradient norm, or the norm
of a single full-rollout-batch gradient vector.

## Results

The token score proxy is not a useful direct estimator of the logged actor
gradient norm in this run:

- `sqrt(actor/intentional_score_norm_mean)` versus `actor/grad_norm`:
  Pearson -0.001 (bootstrap 95% CI [-0.216, 0.110]), Spearman -0.136.
- First-difference Pearson: 0.082. Linear-step-detrended Pearson: -0.054.
- A positive scale calibrated on steps 1-196 and evaluated on steps 197-393
  gives MAE 0.0602, RMSE 0.1558, MAPE 32.6%, and NRMSE 98.0%.
- Dropping the largest 1% of gradient norms changes Pearson to -0.155; dropping
  the largest 5% changes it to -0.233. The lack of positive association is not
  caused only by the step-252 outlier (`actor/grad_norm=2.056`).

The more complete logged proxies do not improve the comparison:

- `sqrt(variance_proxy/expected_w)`: Pearson -0.145, MAPE 29.1%, NRMSE 100.4%.
- `sqrt(variance_proxy/proxy2_total_power)`: Pearson 0.071, MAPE 32.4%,
  NRMSE 98.6%. After dropping the top 1% gradient norms, Pearson is 0.130.

`variance_proxy/proxy1_signal_strength` equals `actor/grad_norm ** 2` exactly
(maximum absolute discrepancy 0), so it is a renamed reference value rather
than an independent proxy.

## Interpretation

The result does not falsify the paper's narrower claim. The paper proxy lives
in vocabulary-logit space, while the parameter gradient is

    grad_theta log pi = J_z(theta)^T (e_y - pi).

The omitted Jacobian metric, advantages, norm-matched alpha, token/batch loss
aggregation, PPO clipping, and cancellation between gradient vectors can all
change the parameter-space norm. SWPO explicitly says the proxy is intended to
capture relative score energy and need not estimate absolute parameter-gradient
scale. OTB's proportionality argument additionally assumes an approximately
constant Jacobian scale.

Consequently, the absolute per-token proxy-versus-oracle error requested in the
paper's sense is not identifiable from this W&B history. That requires logging
matched observations of `||e_y-pi||^2` and `||grad_theta log pi(y|s)||^2` for
the same sampled tokens or turns, before multiplying by advantages and before
batch aggregation.

`actor/intentional_norm_error` is around numerical zero by construction because
the algorithm chooses alpha to match a norm inside proxy movement space. It is
not evidence that the parameter-gradient norm is matched.

## Files

- `analysis.json`: complete statistics, correlations, metric inventory, and
  limitations.
- `paired_proxy_vs_grad.csv`: aligned token proxy, gradient norm, calibrated
  prediction, and relative error.
- `s0mzclga_history.csv`: downloaded W&B history snapshot.

The analysis can be refreshed while the run is active with
`tools/analyze_norm_matched_gradient_proxy.py`.
