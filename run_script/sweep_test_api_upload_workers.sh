#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

CASES="${CASES:-128}"
AREA_CODE="${AREA_CODE:-+1}"
VERIFICATION_CODE="${VERIFICATION_CODE:-123456}"
UPLOAD_FILE="${UPLOAD_FILE:-$PROJECT_DIR/new-open-account/scripts/test.png}"
OUTPUT_DIR="${OUTPUT_DIR:-/tmp/parallel_test_api_upload_worker_sweep_$(date +%Y%m%d_%H%M%S)}"
PHONE_PREFIX="${PHONE_PREFIX:-$(date +%Y%m)}"
PHONE_HISTORY="${PHONE_HISTORY:-/tmp/parallel_test_api_upload_used_phones.txt}"

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
REPORT_CSV="$OUTPUT_DIR/report.csv"
REPORT_MD="$OUTPUT_DIR/report.md"
printf 'workers,exit_code,case_ok,case_count,case_error_count,upload_ok,upload_count,min_s,mean_s,p50_s,p90_s,p95_s,p99_s,max_s,failed_phase_counts,error_counts,run_dir\n' > "$REPORT_CSV"

cd "$PROJECT_DIR"

echo "Output directory: $OUTPUT_DIR"
echo "Worker counts: ${WORKERS[*]}"
echo "Upload file: $UPLOAD_FILE"
echo "Phone prefix: $PHONE_PREFIX"
echo "Phone history: $PHONE_HISTORY"

for workers in "${WORKERS[@]}"; do
  run_dir="$OUTPUT_DIR/workers_${workers}"
  log="$OUTPUT_DIR/workers_${workers}.log"

  echo
  echo "== workers=${workers} =="
  set +e
  "$PYTHON_BIN" new-open-account/scripts/parallel_test_api_upload.py \
    --file "$UPLOAD_FILE" \
    --cases "$CASES" \
    --workers "$workers" \
    --area-code "$AREA_CODE" \
    --verification-code "$VERIFICATION_CODE" \
    --phone-prefix "$PHONE_PREFIX" \
    --phone-history "$PHONE_HISTORY" \
    --output-dir "$run_dir" \
    "$@" 2>&1 | tee "$log"
  exit_code="${PIPESTATUS[0]}"
  set -e

  "$PYTHON_BIN" - "$REPORT_CSV" "$workers" "$exit_code" "$run_dir" <<'PY'
import csv
import json
import sys
from pathlib import Path

report_csv, workers, exit_code, run_dir = sys.argv[1:]
summary_files = sorted(Path(run_dir).glob("parallel_test_api_upload_*/summary.json"))
summary = json.loads(summary_files[-1].read_text(encoding="utf-8")) if summary_files else {}
latency = summary.get("upload_latency_s", {})
row = {
    "workers": workers,
    "exit_code": exit_code,
    "case_ok": summary.get("case_ok", ""),
    "case_count": summary.get("case_count", ""),
    "case_error_count": summary.get("case_error_count", ""),
    "upload_ok": summary.get("upload_ok", ""),
    "upload_count": summary.get("upload_count", ""),
    "min_s": latency.get("min", ""),
    "mean_s": latency.get("mean", ""),
    "p50_s": latency.get("p50", ""),
    "p90_s": latency.get("p90", ""),
    "p95_s": latency.get("p95", ""),
    "p99_s": latency.get("p99", ""),
    "max_s": latency.get("max", ""),
    "failed_phase_counts": json.dumps(summary.get("failed_phase_counts", {}), ensure_ascii=False, sort_keys=True),
    "error_counts": json.dumps(summary.get("error_counts", {}), ensure_ascii=False, sort_keys=True),
    "run_dir": run_dir,
}
with open(report_csv, "a", newline="", encoding="utf-8") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(row))
    writer.writerow(row)
PY
done

"$PYTHON_BIN" - "$REPORT_CSV" "$REPORT_MD" <<'PY'
import csv
import sys
from pathlib import Path

csv_path = Path(sys.argv[1])
md_path = Path(sys.argv[2])
rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
columns = [
    "workers",
    "exit_code",
    "case_ok",
    "case_count",
    "case_error_count",
    "upload_ok",
    "upload_count",
    "mean_s",
    "p95_s",
    "error_counts",
]

lines = ["# test_api upload worker sweep", ""]
lines.append("| " + " | ".join(columns) + " |")
lines.append("| " + " | ".join("---" for _ in columns) + " |")
for row in rows:
    lines.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
lines.append("")
lines.append(f"Raw CSV: `{csv_path}`")
lines.append(f"Output directory: `{csv_path.parent}`")
md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(md_path.read_text(encoding="utf-8"))
PY

echo "CSV report: $REPORT_CSV"
echo "Markdown report: $REPORT_MD"
