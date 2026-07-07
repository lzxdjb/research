#!/usr/bin/env python3
"""
Process all val_responses_step*.csv files in a given folder:
1. Read the base xlsx file with uid information.
2. For each val_responses_step*.csv, extract answers from the 'output' column.
3. Add 'output' and 'answer_str' as new columns to the base xlsx (matched by uid).
4. Save as a new xlsx file in the val_log folder.
"""

import os
import re
import glob
import pandas as pd

# ─── Configuration ───────────────────────────────────────────────────────────

BASE_XLSX_PATH = "/cpfs01/nlp/leizhengxing/stock-rl/data/识股-无答案_uid.xlsx"
VAL_LOG_FOLDER = "/cpfs01/nlp/leizhengxing/stock-rl/val_log/stock_empo_big_learn_rate_fix_reward_dual_clip"
VAL_OUTPUT_FOLDER = "/cpfs01/nlp/leizhengxing/stock-rl/val_log/stock_empo_big_learn_rate_fix_reward_dual_clip_process"

# ─── Regex patterns ─────────────────────────────────────────────────────────

_SIX_DIGIT_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_FINISHED_STRICT_RE = re.compile(
    r"<FINISHED>.*?股票代码候选[：:]\s*(.+?)(?:</|$)",
    re.DOTALL | re.IGNORECASE,
)
_FINISHED_LOOSE_RE = re.compile(r"<FINISHED>(.*?)(?:</|$)", re.DOTALL)

_TOOL_RESPONSE_RE = re.compile(
    r"<tool_response>.*?</tool_response>",
    re.DOTALL | re.IGNORECASE,
)

def _strip_tool_responses(text: str) -> str:
    return _TOOL_RESPONSE_RE.sub("", text)


def extract_answers(predict_str: str) -> list:
    if not isinstance(predict_str, str):
        return []
    clean = _strip_tool_responses(predict_str)
    m = _FINISHED_STRICT_RE.search(clean)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))
    m = _FINISHED_LOOSE_RE.search(clean)
    if m:
        return _SIX_DIGIT_RE.findall(m.group(1))
    return []


def main():
    # ─── Read base xlsx ──────────────────────────────────────────────────────
    print(f"Reading base xlsx: {BASE_XLSX_PATH}")
    base_df = pd.read_excel(BASE_XLSX_PATH)
    print(f"  Base xlsx shape: {base_df.shape}")
    print(f"  Base xlsx columns: {list(base_df.columns)}")

    # ─── Find all val_responses_step*.csv files ─────────────────────────────
    pattern = os.path.join(VAL_LOG_FOLDER, "val_responses_step*.csv")
    all_files = glob.glob(pattern)
    csv_files = [f for f in all_files if "_proprocess" not in os.path.basename(f)]

    def get_step_number(filepath):
        m = re.search(r"val_responses_step(\d+)\.csv", os.path.basename(filepath))
        return int(m.group(1)) if m else 0

    csv_files.sort(key=get_step_number)

    print(f"\nFound {len(csv_files)} files to process:")
    for f in csv_files:
        print(f"  {os.path.basename(f)}")

    if not csv_files:
        print("No files found. Exiting.")
        return

    # ─── Process each file ───────────────────────────────────────────────────
    for csv_path in csv_files:
        basename = os.path.basename(csv_path)
        step_num = get_step_number(csv_path)
        print(f"\n{'='*70}")
        print(f"Processing: {basename}")

        val_df = pd.read_csv(csv_path)

        if 'output' not in val_df.columns or 'uid' not in val_df.columns:
            print(f"  WARNING: missing 'output' or 'uid' column. Skipping.")
            continue

        # Extract answers
        val_df['answer_str'] = val_df['output'].apply(
    lambda x: ', '.join(extract_answers(x)) if isinstance(x, str) else ''
)

        total = len(val_df)
        has_answer = sum(1 for a in val_df['answer_str'] if a != '')
        print(f"  Total: {total}, With answer: {has_answer}, Without: {total - has_answer}")

        # Only keep uid, output, answer_str from val_df
        val_subset = val_df[['uid', 'output', 'answer_str']].copy()

        # Merge into base_df: left join so base_df structure is preserved
        merged_df = base_df.merge(val_subset, on='uid', how='left')
        print(f"  Merged shape: {merged_df.shape}")

        # Save as xlsx
        out_name = f"val_responses_step{step_num}_proprocess.xlsx"
        out_path = os.path.join(VAL_OUTPUT_FOLDER, out_name)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        merged_df.to_excel(out_path, index=False, engine='openpyxl')
        print(f"  Saved to: {out_path}")

    print(f"\n{'='*70}")
    print("All done!")


if __name__ == "__main__":
    main()