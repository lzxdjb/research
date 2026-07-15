#!/usr/bin/env python3
"""Analyze the logit-score proxy against the logged actor gradient norm."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import wandb


STEP = "training/global_step"
GRAD = "actor/grad_norm"
PROXY = "actor/intentional_score_norm_mean"
USED_FULL_PROXY = "actor/intentional_used_sum_pi_squared"

CONTEXT_KEYS = [
    "response_length/mean",
    "num_turns/mean",
    "critic/advantages/mean",
    "critic/advantages/max",
    "actor/intentional_alpha_scale",
    "actor/intentional_projection_cos",
    "actor/intentional_target_error",
    "actor/intentional_norm_error",
    "variance_proxy/expected_w",
    "variance_proxy/proxy1_signal_strength",
    "variance_proxy/proxy2_total_power",
]


def safe_corr(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 2 or np.std(x) == 0 or np.std(y) == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def error_metrics(y: np.ndarray, pred: np.ndarray) -> dict[str, float]:
    error = pred - y
    return {
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error**2))),
        "nrmse_by_mean_grad": float(np.sqrt(np.mean(error**2)) / np.mean(np.abs(y))),
        "mape": float(np.mean(np.abs(error) / np.maximum(np.abs(y), 1e-12))),
        "median_absolute_percentage_error": float(
            np.median(np.abs(error) / np.maximum(np.abs(y), 1e-12))
        ),
        "bias": float(np.mean(error)),
    }


def bootstrap_corr_ci(
    x: np.ndarray, y: np.ndarray, rng: np.random.Generator, samples: int = 10_000
) -> list[float] | None:
    if len(x) < 3:
        return None
    estimates = []
    for _ in range(samples):
        idx = rng.integers(0, len(x), len(x))
        value = safe_corr(x[idx], y[idx])
        if value is not None:
            estimates.append(value)
    if not estimates:
        return None
    return [float(v) for v in np.percentile(estimates, [2.5, 97.5])]


def analyze_pair(frame: pd.DataFrame, proxy_key: str) -> tuple[dict, pd.DataFrame]:
    paired = frame[[STEP, GRAD, proxy_key]].dropna().sort_values(STEP).copy()
    if len(paired) < 4:
        raise ValueError(f"Only {len(paired)} paired rows for {proxy_key}")

    step = paired[STEP].to_numpy(float)
    y = paired[GRAD].to_numpy(float)
    raw = paired[proxy_key].to_numpy(float)
    if np.any(raw < 0):
        raise ValueError(f"Negative values found in squared-norm proxy {proxy_key}")
    x = np.sqrt(raw)

    split = len(paired) // 2
    train_x, test_x = x[:split], x[split:]
    train_y, test_y = y[:split], y[split:]
    positive_scale = float(train_x @ train_y / max(train_x @ train_x, 1e-30))
    test_pred = positive_scale * test_x

    design = np.column_stack([np.ones(split), train_x])
    intercept, slope = np.linalg.lstsq(design, train_y, rcond=None)[0]
    affine_test_pred = intercept + slope * test_x

    step_residual_x = x - np.polyval(np.polyfit(step, x, 1), step)
    step_residual_y = y - np.polyval(np.polyfit(step, y, 1), step)
    rng = np.random.default_rng(42)
    trimmed = {}
    for quantile in (0.95, 0.99):
        cutoff = float(np.quantile(y, quantile))
        keep = y <= cutoff
        trimmed[f"drop_top_{int((1 - quantile) * 100)}pct_grad_norm"] = {
            "rows": int(keep.sum()),
            "grad_norm_cutoff": cutoff,
            "pearson_sqrt_proxy_vs_grad": safe_corr(x[keep], y[keep]),
        }

    paired["sqrt_proxy"] = x
    paired["positive_scale_prediction"] = positive_scale * x
    paired["split"] = np.where(np.arange(len(paired)) < split, "calibration", "evaluation")
    paired["relative_error"] = (paired["positive_scale_prediction"] - y) / np.maximum(np.abs(y), 1e-12)

    result = {
        "proxy_key": proxy_key,
        "paired_steps": int(len(paired)),
        "step_range": [int(step.min()), int(step.max())],
        "pearson_sqrt_proxy_vs_grad": safe_corr(x, y),
        "pearson_95pct_bootstrap_ci": bootstrap_corr_ci(x, y, rng),
        "spearman_sqrt_proxy_vs_grad": float(
            pd.Series(x).rank(method="average").corr(pd.Series(y).rank(method="average"))
        ),
        "pearson_first_differences": safe_corr(np.diff(x), np.diff(y)),
        "pearson_after_linear_step_detrending": safe_corr(step_residual_x, step_residual_y),
        "trimmed_sensitivity": trimmed,
        "temporal_calibration": {
            "calibration_rows": int(split),
            "evaluation_rows": int(len(paired) - split),
            "positive_scale": positive_scale,
            "evaluation_errors": error_metrics(test_y, test_pred),
            "unconstrained_affine_intercept": float(intercept),
            "unconstrained_affine_slope": float(slope),
            "unconstrained_affine_evaluation_errors": error_metrics(test_y, affine_test_pred),
        },
        "ranges": {
            "proxy": [float(raw.min()), float(raw.max())],
            "sqrt_proxy": [float(x.min()), float(x.max())],
            "grad_norm": [float(y.min()), float(y.max())],
        },
    }
    return result, paired


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entity", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    api = wandb.Api(timeout=60)
    runs = list(
        api.runs(
            f"{args.entity}/{args.project}",
            filters={"display_name": args.run_name},
        )
    )
    if len(runs) != 1:
        raise SystemExit(f"Expected one matching run, found {len(runs)}")
    run = runs[0]

    records = list(run.scan_history(page_size=1000))
    frame = pd.DataFrame(records)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    history_path = args.output_dir / f"{run.id}_history.csv"
    frame.to_csv(history_path, index=False)

    available_keys = sorted(frame.columns)
    candidate_keys = [
        key
        for key in available_keys
        if any(term in key.lower() for term in ("grad", "score_norm", "proxy", "intentional"))
    ]
    proxy_result, paired = analyze_pair(frame, PROXY)
    paired.to_csv(args.output_dir / "paired_proxy_vs_grad.csv", index=False)

    extra_proxy_results = {}
    for key in ("variance_proxy/expected_w", "variance_proxy/proxy2_total_power"):
        if key in frame and frame[key].notna().sum() >= 4:
            extra_proxy_results[key] = analyze_pair(frame, key)[0]

    context_columns = [key for key in [PROXY, GRAD, *CONTEXT_KEYS] if key in frame]
    context_corr = frame[context_columns].corr(numeric_only=True).to_dict()
    full_proxy_values = (
        sorted(frame[USED_FULL_PROXY].dropna().astype(float).unique().tolist())
        if USED_FULL_PROXY in frame
        else []
    )
    signal_key = "variance_proxy/proxy1_signal_strength"
    signal_identity = None
    if signal_key in frame:
        identity = frame[[GRAD, signal_key]].dropna()
        difference = (identity[signal_key] - identity[GRAD].pow(2)).abs()
        signal_identity = {
            "paired_rows": int(len(identity)),
            "max_absolute_error_vs_grad_norm_squared": float(difference.max()),
        }
    top_grad_steps = (
        frame.nlargest(5, GRAD)[[STEP, GRAD, PROXY]].to_dict(orient="records")
        if GRAD in frame
        else []
    )

    report = {
        "run": {
            "id": run.id,
            "name": run.name,
            "state": run.state,
            "url": run.url,
            "created_at": run.created_at,
            "history_rows": int(len(frame)),
            "last_global_step": int(frame[STEP].dropna().max()),
        },
        "proxy_formula": "1 - 2*pi(y) + sum_v pi(v)^2 = ||e_y - pi||_2^2",
        "full_sum_pi_squared_proxy_values": full_proxy_values,
        "signal_strength_identity_check": signal_identity,
        "largest_grad_norm_steps": top_grad_steps,
        "score_norm_proxy": proxy_result,
        "extra_proxy_results": extra_proxy_results,
        "context_pearson_correlations": context_corr,
        "candidate_metric_keys": candidate_keys,
        "interpretation": [
            "The score proxy is a squared norm in vocabulary-logit space, while actor/grad_norm is a norm in parameter space after model Jacobians and batch-vector aggregation.",
            "A positive scalar calibration is diagnostic only; its error is not an absolute oracle approximation error because the two logged metrics are not the same mathematical quantity.",
            "actor/intentional_norm_error is near zero by construction in proxy movement space and does not validate parameter-gradient accuracy.",
            "actor/grad_norm is reduced as the mean of PPO mini-batch gradient norms within a global step; it is not the norm of one full-rollout-batch gradient vector.",
            "Per-token or per-sample parameter-gradient norms were not logged, so the paper's direct proxy-versus-oracle error is not identifiable from this run.",
        ],
    }
    (args.output_dir / "analysis.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
