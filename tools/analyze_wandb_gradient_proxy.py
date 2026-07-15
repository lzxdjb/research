#!/usr/bin/env python3
"""Download a W&B run and compare SWPO's score proxy with gradient norms."""

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd
import wandb


STEP = "training/global_step"
GRAD = "actor/grad_norm"
TURN_PROXY = "critic/value_loss_weight/raw_mean"
TOKEN_PROXY = "critic/value_loss_weight/token_score_norm_mean"


def correlation_ci(x: np.ndarray, y: np.ndarray, rng: np.random.Generator) -> list[float]:
    values = []
    for _ in range(10_000):
        idx = rng.integers(0, len(x), len(x))
        if np.std(x[idx]) > 0 and np.std(y[idx]) > 0:
            values.append(np.corrcoef(x[idx], y[idx])[0, 1])
    return [float(v) for v in np.percentile(values, [2.5, 97.5])]


def compare_proxy(frame: pd.DataFrame, proxy_key: str) -> tuple[dict, pd.DataFrame]:
    clean = frame[[STEP, GRAD, proxy_key]].dropna().sort_values(STEP).copy()
    step = clean[STEP].to_numpy(float)
    x = clean[proxy_key].to_numpy(float)
    y = clean[GRAD].to_numpy(float)
    feature = np.sqrt(x)

    scale = float(feature @ y / (feature @ feature))
    pred_scale = scale * feature
    design = np.column_stack([np.ones(len(feature)), feature])
    intercept, slope = np.linalg.lstsq(design, y, rcond=None)[0]
    pred_affine = intercept + slope * feature
    residual_x = x - np.polyval(np.polyfit(step, x, 1), step)
    residual_y = y - np.polyval(np.polyfit(step, y, 1), step)

    def errors(pred: np.ndarray) -> dict[str, float]:
        err = pred - y
        return {
            "mae": float(np.mean(np.abs(err))),
            "rmse": float(np.sqrt(np.mean(err**2))),
            "nrmse_by_mean_grad": float(np.sqrt(np.mean(err**2)) / np.mean(y)),
            "mape": float(np.mean(np.abs(err) / np.maximum(np.abs(y), 1e-12))),
            "median_absolute_percentage_error": float(np.median(np.abs(err) / np.maximum(np.abs(y), 1e-12))),
            "r2": float(1.0 - np.sum(err**2) / np.sum((y - y.mean()) ** 2)),
        }

    rng = np.random.default_rng(42)
    pearson = float(np.corrcoef(x, y)[0, 1])
    clean["scaled_sqrt_proxy"] = pred_scale
    clean["affine_sqrt_proxy"] = pred_affine
    clean["scaled_relative_error"] = (pred_scale - y) / y
    result = {
        "proxy_key": proxy_key,
        "paired_steps": len(clean),
        "pearson_proxy_vs_grad": pearson,
        "pearson_95pct_bootstrap_ci": correlation_ci(x, y, rng),
        "spearman_proxy_vs_grad": float(pd.Series(x).corr(pd.Series(y), method="spearman")),
        "pearson_first_differences": float(np.corrcoef(np.diff(x), np.diff(y))[0, 1]),
        "pearson_after_linear_step_detrending": float(np.corrcoef(residual_x, residual_y)[0, 1]),
        "positive_scale_on_sqrt_proxy": scale,
        "positive_scale_errors": errors(pred_scale),
        "affine_intercept": float(intercept),
        "affine_slope_on_sqrt_proxy": float(slope),
        "affine_errors": errors(pred_affine),
    }
    return result, clean


def write_analysis(frame: pd.DataFrame, output_dir: Path) -> dict:
    turn_result, paired = compare_proxy(frame, TURN_PROXY)
    token_result, _ = compare_proxy(frame, TOKEN_PROXY)
    correlations = frame[[TURN_PROXY, TOKEN_PROXY, GRAD, "response_length/mean", "num_turns/mean"]].corr()
    report = {
        "turn_proxy": turn_result,
        "token_proxy": token_result,
        "context_pearson_correlations": correlations.to_dict(),
        "limitations": [
            "This run used turn_suffix_score_norm, not turn_score_norm. Its raw_mean is a token-weighted aggregation after broadcasting each turn-suffix S_i to tokens; it is not a per-turn W_n series.",
            "actor/grad_norm is the pre-clipping parameter-gradient norm of the full batch PPO objective, which also depends on advantages, PPO ratios, KL loss, model Jacobians, and batch aggregation.",
            "The run did not log variance_proxy/expected_w or per-sample/per-turn parameter-gradient norms, so absolute oracle-proxy error is not identifiable from this history.",
        ],
    }
    (output_dir / "analysis.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    paired.to_csv(output_dir / "paired_turn_proxy.csv", index=False)
    return report


def write_visualization(paired: pd.DataFrame, path: Path) -> None:
    points = [
        {"step": int(row[STEP]), "grad": round(float(row[GRAD]), 5), "proxy": round(float(row[TURN_PROXY]), 5)}
        for _, row in paired.iterrows()
    ]
    data = json.dumps(points, separators=(",", ":"))
    fragment = f'''<div id="gradient-proxy-diagnostic" aria-label="Gradient proxy diagnostic plots">
<style>
#gradient-proxy-diagnostic {{ color: var(--foreground); width: 100%; }}
#gradient-proxy-diagnostic .plots {{ display: grid; grid-template-columns: 1.45fr 1fr; gap: 20px; }}
#gradient-proxy-diagnostic svg {{ display: block; width: 100%; height: auto; overflow: visible; }}
#gradient-proxy-diagnostic .axis text, #gradient-proxy-diagnostic .label {{ fill: var(--muted-foreground); }}
#gradient-proxy-diagnostic .axis path, #gradient-proxy-diagnostic .axis line {{ stroke: var(--border); }}
#gradient-proxy-diagnostic .grid line {{ stroke: var(--border); stroke-opacity: .55; }}
#gradient-proxy-diagnostic .legend {{ display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 8px; color: var(--muted-foreground); }}
#gradient-proxy-diagnostic .swatch {{ display: inline-block; width: 16px; height: 3px; margin-right: 6px; vertical-align: middle; }}
@media (max-width: 600px) {{ #gradient-proxy-diagnostic .plots {{ grid-template-columns: 1fr; }} }}
</style>
<div class="legend"><span><i class="swatch" style="background:var(--viz-series-1)"></i>Actor grad norm</span><span><i class="swatch" style="background:var(--viz-series-2)"></i>Cheap turn proxy</span></div>
<div class="plots"><svg class="timeline" role="img" aria-label="Normalized gradient norm and proxy over training steps"></svg><svg class="scatter" role="img" aria-label="Scatter plot of cheap turn proxy against actor gradient norm"></svg></div>
<script type="module">
import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7/+esm";
const root = document.getElementById("gradient-proxy-diagnostic");
const data = {data};
const color = name => getComputedStyle(root).getPropertyValue(name).trim();
const margin = {{top: 18, right: 14, bottom: 42, left: 50}};
function base(selector, width, height) {{
  const svg = d3.select(root).select(selector).attr("viewBox", `0 0 ${{width}} ${{height}}`);
  return {{svg, width: width-margin.left-margin.right, height: height-margin.top-margin.bottom, g: svg.append("g").attr("transform", `translate(${{margin.left}},${{margin.top}})`)}};
}}
const t = base(".timeline", 620, 330);
const x = d3.scaleLinear(d3.extent(data,d=>d.step),[0,t.width]);
const norm = key => {{ const e=d3.extent(data,d=>d[key]); return d3.scaleLinear(e,[0,1]); }};
const ng=norm("grad"), np=norm("proxy"), y=d3.scaleLinear([0,1],[t.height,0]);
t.g.append("g").attr("class","grid").call(d3.axisLeft(y).ticks(5).tickSize(-t.width).tickFormat(""));
t.g.append("g").attr("class","axis").attr("transform",`translate(0,${{t.height}})`).call(d3.axisBottom(x).ticks(6));
t.g.append("g").attr("class","axis").call(d3.axisLeft(y).ticks(5).tickFormat(d3.format(".1f")));
const line = key => d3.line().x(d=>x(d.step)).y(d=>y(key==="grad"?ng(d.grad):np(d.proxy)));
t.g.append("path").datum(data).attr("fill","none").attr("stroke",color("--viz-series-1")).attr("stroke-width",2).attr("d",line("grad"));
t.g.append("path").datum(data).attr("fill","none").attr("stroke",color("--viz-series-2")).attr("stroke-width",2).attr("d",line("proxy"));
t.svg.append("text").attr("class","label").attr("x",margin.left+t.width/2).attr("y",322).attr("text-anchor","middle").text("Training step");
t.svg.append("text").attr("class","label").attr("transform","rotate(-90)").attr("x",-(margin.top+t.height/2)).attr("y",14).attr("text-anchor","middle").text("Own-range normalized value");
const s = base(".scatter", 430, 330);
const sx=d3.scaleLinear(d3.extent(data,d=>d.proxy)).nice().range([0,s.width]), sy=d3.scaleLinear(d3.extent(data,d=>d.grad)).nice().range([s.height,0]);
s.g.append("g").attr("class","grid").call(d3.axisLeft(sy).ticks(5).tickSize(-s.width).tickFormat(""));
s.g.append("g").attr("class","axis").attr("transform",`translate(0,${{s.height}})`).call(d3.axisBottom(sx).ticks(5));
s.g.append("g").attr("class","axis").call(d3.axisLeft(sy).ticks(5));
s.g.selectAll("circle").data(data).join("circle").attr("cx",d=>sx(d.proxy)).attr("cy",d=>sy(d.grad)).attr("r",2.6).attr("fill",color("--viz-series-1")).attr("opacity",.62);
const xm=d3.mean(data,d=>d.proxy), ym=d3.mean(data,d=>d.grad), slope=d3.sum(data,d=>(d.proxy-xm)*(d.grad-ym))/d3.sum(data,d=>(d.proxy-xm)**2), intercept=ym-slope*xm, xe=d3.extent(data,d=>d.proxy);
s.g.append("line").attr("x1",sx(xe[0])).attr("y1",sy(intercept+slope*xe[0])).attr("x2",sx(xe[1])).attr("y2",sy(intercept+slope*xe[1])).attr("stroke",color("--viz-series-2")).attr("stroke-width",2);
s.svg.append("text").attr("class","label").attr("x",margin.left+s.width/2).attr("y",322).attr("text-anchor","middle").text("Cheap turn proxy raw_mean");
s.svg.append("text").attr("class","label").attr("transform","rotate(-90)").attr("x",-(margin.top+s.height/2)).attr("y",14).attr("text-anchor","middle").text("Actor grad norm");
</script>
</div>'''
    path.write_text(fragment, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="wiki")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--entity")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--visualization", type=Path)
    args = parser.parse_args()

    api = wandb.Api(timeout=60)
    entity = args.entity or api.viewer.entity
    filters = {"display_name": args.run_name}
    runs = list(api.runs(f"{entity}/{args.project}", filters=filters))
    if not runs:
        raise SystemExit(f"No run named {args.run_name!r} in {entity}/{args.project}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    inventory = []
    for run in runs:
        records = list(run.scan_history(page_size=1000))
        all_keys = sorted({key for record in records for key in record})
        keys = sorted(set(run.summary.keys()) | set(all_keys))
        candidate_keys = [
            key for key in keys
            if any(term in key.lower() for term in ("grad", "score_norm", "weight", "proxy", "length"))
        ]
        history_path = args.output_dir / f"{run.id}_history.csv"
        with history_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=all_keys)
            writer.writeheader()
            writer.writerows(records)
        report = write_analysis(pd.DataFrame(records), args.output_dir)
        if args.visualization:
            paired = pd.read_csv(args.output_dir / "paired_turn_proxy.csv")
            write_visualization(paired, args.visualization)
        inventory.append({
            "id": run.id,
            "name": run.name,
            "state": run.state,
            "url": run.url,
            "created_at": run.created_at,
            "candidate_summary_keys": candidate_keys,
            "history_rows": len(records),
            "history_path": str(history_path),
            "analysis_path": str(args.output_dir / "analysis.json"),
        })

    (args.output_dir / "run_inventory.json").write_text(
        json.dumps(inventory, indent=2), encoding="utf-8"
    )
    print(json.dumps({"entity": entity, "runs": inventory, "analysis": report}, indent=2))


if __name__ == "__main__":
    main()
