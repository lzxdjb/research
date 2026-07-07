#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

TRAJECTORIES="${TRAJECTORIES:-128}"
UPLOADS_PER_TRAJECTORY="${UPLOADS_PER_TRAJECTORY:-8}"
AUTH_WORKERS="${AUTH_WORKERS:-8}"
UPLOAD_MODE="${UPLOAD_MODE:-query-progress}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-5}"
READ_TIMEOUT="${READ_TIMEOUT:-30}"
API_SCRIPTS_DIR="${API_SCRIPTS_DIR:-$PROJECT_DIR/new-open-account/scripts}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/real_bank_upload_worker_sweep_$(date +%Y%m%d_%H%M%S)}"

# Override with WORKER_COUNTS="16 32 64 128 156" when you want an exact list.
if [[ -n "${WORKER_COUNTS:-}" ]]; then
  read -r -a WORKERS <<< "$WORKER_COUNTS"
else
  START_WORKERS="${START_WORKERS:-16}"
  END_WORKERS="${END_WORKERS:-156}"
  STEP_WORKERS="${STEP_WORKERS:-16}"
  WORKERS=()
  worker="$START_WORKERS"
  while (( worker <= END_WORKERS )); do
    WORKERS+=("$worker")
    worker=$((worker + STEP_WORKERS))
  done
  if (( ${#WORKERS[@]} == 0 || WORKERS[${#WORKERS[@]} - 1] != END_WORKERS )); then
    WORKERS+=("$END_WORKERS")
  fi
fi

mkdir -p "$OUTPUT_DIR"
SUMMARY_CSV="$OUTPUT_DIR/report.csv"
SUMMARY_MD="$OUTPUT_DIR/report.md"
printf 'upload_workers,exit_code,auth_ok,auth_count,upload_ok,upload_count,upload_error_count,min_s,mean_s,p50_s,p90_s,p95_s,p99_s,max_s,jsonl,log\n' > "$SUMMARY_CSV"

cd "$PROJECT_DIR"

echo "Output directory: $OUTPUT_DIR"
echo "Worker counts: ${WORKERS[*]}"

for upload_workers in "${WORKERS[@]}"; do
  jsonl="$OUTPUT_DIR/real_bank_${UPLOAD_MODE}_workers${upload_workers}.jsonl"
  log="$OUTPUT_DIR/real_bank_${UPLOAD_MODE}_workers${upload_workers}.log"

  echo
  echo "== upload-workers=${upload_workers} =="

  set +e
  DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR="$API_SCRIPTS_DIR" \
  DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT="$CONNECT_TIMEOUT" \
  DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT="$READ_TIMEOUT" \
  "$PYTHON_BIN" -m recipe.digital_onboarding.scripts.stress_real_bank_upload \
    --trajectories "$TRAJECTORIES" \
    --uploads-per-trajectory "$UPLOADS_PER_TRAJECTORY" \
    --upload-workers "$upload_workers" \
    --auth-workers "$AUTH_WORKERS" \
    --upload-mode "$UPLOAD_MODE" \
    --output-jsonl "$jsonl" \
    "$@" 2>&1 | tee "$log"
  exit_code="${PIPESTATUS[0]}"
  set -e

  "$PYTHON_BIN" - "$SUMMARY_CSV" "$upload_workers" "$exit_code" "$jsonl" "$log" <<'PY'
import csv
import json
import statistics
import sys
from pathlib import Path

summary_csv, upload_workers, exit_code, jsonl_path, log_path = sys.argv[1:]
records = []
path = Path(jsonl_path)
if path.exists():
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

auth = [row for row in records if row.get("phase") == "auth"]
uploads = [row for row in records if row.get("phase") == "upload"]
latencies = [
    float(row["upload_elapsed_s"])
    for row in uploads
    if row.get("upload_elapsed_s") is not None
]

def percentile(values, pct):
    if not values:
        return ""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return f"{ordered[index]:.6f}"

def fmt(value):
    return "" if value is None else f"{value:.6f}"

row = {
    "upload_workers": upload_workers,
    "exit_code": exit_code,
    "auth_ok": sum(1 for item in auth if item.get("ok")),
    "auth_count": len(auth),
    "upload_ok": sum(1 for item in uploads if item.get("ok")),
    "upload_count": len(uploads),
    "upload_error_count": sum(1 for item in uploads if not item.get("ok")),
    "min_s": fmt(min(latencies) if latencies else None),
    "mean_s": fmt(statistics.fmean(latencies) if latencies else None),
    "p50_s": percentile(latencies, 0.50),
    "p90_s": percentile(latencies, 0.90),
    "p95_s": percentile(latencies, 0.95),
    "p99_s": percentile(latencies, 0.99),
    "max_s": fmt(max(latencies) if latencies else None),
    "jsonl": jsonl_path,
    "log": log_path,
}

with open(summary_csv, "a", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(row))
    writer.writerow(row)
PY
done

"$PYTHON_BIN" - "$SUMMARY_CSV" "$SUMMARY_MD" <<'PY'
import csv
import sys
from pathlib import Path

csv_path = Path(sys.argv[1])
md_path = Path(sys.argv[2])
rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))

columns = [
    "upload_workers",
    "exit_code",
    "upload_ok",
    "upload_count",
    "upload_error_count",
    "mean_s",
    "p50_s",
    "p90_s",
    "p95_s",
    "p99_s",
    "max_s",
]

lines = ["# Real-bank upload worker sweep", ""]
lines.append("| " + " | ".join(columns) + " |")
lines.append("| " + " | ".join("---" for _ in columns) + " |")
for row in rows:
    lines.append("| " + " | ".join(row.get(column, "") for column in columns) + " |")
lines.append("")
lines.append(f"Raw CSV: `{csv_path}`")
lines.append(f"Output directory: `{csv_path.parent}`")
md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

print(md_path.read_text(encoding="utf-8"))
PY

echo "CSV report: $SUMMARY_CSV"
echo "Markdown report: $SUMMARY_MD"
