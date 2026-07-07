import argparse
import base64
import json
import os
import re
import uuid
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


# ── helpers ──────────────────────────────────────────────────────────────────

def extract_image_path(raw_url: str) -> str | None:
    """
    Extract the first full image URL from a messy imageDataUrl field.

    Handles all observed formats:
      - bare URL:        https://host/path/file.png
      - markdown link:   [text](https://host/path/file.png "title")
      - markdown image:  ![](https://host/path/file.png)
      - HTML img tag:    <img src="https://host/path/file.png" ... />

    Returns the complete URL including https://, e.g.:
      "https://ai.iwencai.com/userinfo-model-image-q-a/abc123.png"

    Supports .png / .jpg / .jpeg (case-insensitive).
    If multiple images are present, returns the first one.
    """
    if not isinstance(raw_url, str):
        return None

    m = re.search(r'https?://[^\s\'")\]<>]+\.(?:png|jpg|jpeg)', raw_url, re.IGNORECASE)
    return m.group(0) if m else None


def extract_stock_code(image_path: str) -> str | None:
    """
    Pull the stock code from paths like 'kline/000927_2026-02-02_2026-04-07.png'
    → '000927'
    """
    if not image_path:
        return None
    fname = Path(image_path).stem          # '000927_2026-02-02_2026-04-07'
    parts = fname.split('_')
    return parts[0] if parts else None


def extract_ground_truth(raw: str) -> str | None:
    """
    Extract ground truth from the '标准答案' column.

    If raw contains a 6-digit serial number (e.g. '安孚科技（603031）'),
    return just the 6-digit code ('603031').
    Otherwise return the whole trimmed string as-is.
    Returns None if raw is empty or not a string.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    m = re.search(r'\b(\d{6})\b', raw)
    if m:
        return m.group(1)
    return raw


def image_to_base64(image_source) -> str | None:
    """
    Return base64-encoded image bytes from either:
      - a remote URL string (https://...)  -> downloaded via requests
      - a local Path object or path string -> read from disk
    Returns None on any failure.
    """
    if isinstance(image_source, str) and image_source.startswith('http'):
        try:
            import requests
            resp = requests.get(image_source, timeout=15)
            resp.raise_for_status()
            return base64.b64encode(resp.content).decode('utf-8')
        except Exception as e:
            print(f"[warn] Failed to download {image_source}: {e}")
            return None
    else:
        p = Path(image_source)
        if not p.exists():
            return None
        with open(p, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')

from PIL import Image
import io

def load_image_as_pil(image_source) -> Image.Image | None:
    """
    Return a PIL.Image from either a remote URL or a local path.
    Returns None on any failure.
    """
    if isinstance(image_source, str) and image_source.startswith('http'):
        try:
            import requests
            resp = requests.get(image_source, timeout=15)
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content)).copy()
        except Exception as e:
            print(f"[warn] Failed to download {image_source}: {e}")
            return None
    else:
        p = Path(image_source)
        if not p.exists():
            return None
        return Image.open(p).copy()

# ── Job 1: stamp UIDs ─────────────────────────────────────────────────────────

def stamp_uids(input_xlsx: str, output_xlsx: str):
    print(f"[stamp] Reading {input_xlsx} ...")
    df = pd.read_excel(input_xlsx, dtype=str)

    if 'uid' not in df.columns:
        df.insert(0, 'uid', [str(uuid.uuid4()) for _ in range(len(df))])
        print(f"[stamp] Created {len(df)} new UIDs.")
    else:
        # Only fill missing ones (idempotent)
        mask = df['uid'].isna() | (df['uid'].str.strip() == '')
        df.loc[mask, 'uid'] = [str(uuid.uuid4()) for _ in range(mask.sum())]
        print(f"[stamp] uid column already present; filled {mask.sum()} missing UIDs.")

    df.to_excel(output_xlsx, index=False)
    print(f"[stamp] Saved stamped xlsx → {output_xlsx}")
    return df


# ── Job 2: build RL dataset ───────────────────────────────────────────────────

USER_PROMPT_TEMPLATE = "{query}<image>"    # <image> token consumed by the model


def load_system_prompt(path: str) -> str:
    """Read the system prompt from a markdown (or plain text) file."""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read().strip()


def build_rl_dataset(
    df: pd.DataFrame,
    system_prompt: str,
    images_dir: str,
    save_dir: str,
    output_jsonl: str,
):
    os.makedirs(save_dir, exist_ok=True)

    stem = Path(output_jsonl).stem
    jsonl_path   = os.path.join(save_dir, output_jsonl)
    parquet_path = os.path.join(save_dir, stem + ".parquet")

    records = []   # accumulate for parquet; jsonl written line-by-line
    written = 0
    skipped = 0

    with open(jsonl_path, 'w', encoding='utf-8') as fout:
        for idx, row in df.iterrows():
            uid = str(row.get('uid', '')).strip()
            query = str(row.get('query', '')).strip()
            raw_url = row.get('imageDataUrl', '')

            # ── ground truth from '标准答案' column ──────────────────────────
            raw_answer = str(row.get('标准答案', '')).strip()
            ground_truth = extract_ground_truth(raw_answer)
            if not ground_truth:
                print(f"[skip] row {idx}: empty or unparseable '标准答案': {raw_answer!r}")
                skipped += 1
                continue
            print(ground_truth)

            # ── image ────────────────────────────────────────────────────────
            image_path_str = extract_image_path(raw_url)
            if not image_path_str:
                print(f"[skip] row {idx}: cannot parse image path from imageDataUrl")
                skipped += 1
                continue

            if image_path_str.startswith('http'):
                img_pil = load_image_as_pil(image_path_str)
                if img_pil is None:
                    print(f"[skip] row {idx}: could not download {image_path_str}")
                    skipped += 1
                    continue
            else:
                image_full_path = Path(images_dir) / image_path_str
                img_pil = load_image_as_pil(image_full_path)
                if img_pil is None:
                    print(f"[skip] row {idx}: image not found at {image_full_path}")
                    skipped += 1
                    continue

            sample = {
                "uid": uid,                          # ← join key with xlsx & val CSV
                "data_source": "hithink_stock_candlestick",
                "agent_name": "stock_chart_agent",
                "prompt": [
                    {"content": system_prompt, "role": "system"},
                    {"content": USER_PROMPT_TEMPLATE.format(query=query), "role": "user"},
                ],
                "images": [img_pil],
                "image_path": image_path_str,
                "ability": "stock_identification",
                "reward_model": {
                    "ground_truth": ground_truth,
                    "style": "rule",
                },
                "extra_info": {
                    "uid": uid,                      # duplicated for easy access
                    "ground_truth": ground_truth,
                    "image_path": image_path_str,
                    "index": idx,
                    "interaction_kwargs": {
                        "ground_truth": ground_truth,
                        "image_path": image_path_str,
                    },
                    "need_tools_kwargs": True,
                    "tools_kwargs": {
                        "calc_stock_reward": {
                            "create_kwargs": {
                                "ground_truth": ground_truth,
                            }
                        }
                    },
                },
            }

            # jsonl: replace image bytes with <image> placeholder (human-readable)
            fake_sampe = sample.copy()
            sample_for_jsonl = {**fake_sampe, "images": ["<image>"]}
            fout.write(json.dumps(sample_for_jsonl, ensure_ascii=False) + '\n')

            # parquet: keep full nested structure natively
            records.append(sample)
            written += 1

    # ── write parquet ────────────────────────────────────────────────────────
    if records:
        import datasets as hf_datasets

        ds = hf_datasets.Dataset.from_list(records)
        ds.to_parquet(parquet_path)
        print(f"[dataset] Written {written} records, skipped {skipped}")
        print(f"          jsonl   → {jsonl_path}")
        print(f"          parquet → {parquet_path}")
    else:
        print(f"[dataset] No records written (all {skipped} rows skipped).")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--stamp-xlsx', action='store_true',
                   help='Add/fill uid column in the source xlsx.')
    p.add_argument('--make-dataset', action='store_true',
                   help='Build the RL .jsonl + .parquet dataset from the xlsx.')
    p.add_argument('--input-xlsx', required=True)
    p.add_argument('--output-xlsx', default=None,
                   help='Where to save the uid-stamped xlsx (default: overwrite input).')
    p.add_argument('--system-prompt-path', default=None,
                   help='Path to a .md / .txt file containing the system prompt '
                        '(required for --make-dataset).')
    p.add_argument('--images-dir', default='.',
                   help='Root directory where image files live.')
    p.add_argument('--save-dir', default='.',
                   help='Directory to write the output files.')
    p.add_argument('--output-jsonl', default='rl_dataset.jsonl',
                   help='Filename for the jsonl output; .parquet uses the same stem.')
    return p.parse_args()


def main():
    args = parse_args()

    if not args.stamp_xlsx and not args.make_dataset:
        print("Nothing to do — pass --stamp-xlsx and/or --make-dataset.")
        return

    df = None

    if args.stamp_xlsx:
        out_xlsx = args.output_xlsx or args.input_xlsx
        df = stamp_uids(args.input_xlsx, out_xlsx)
        read_from = out_xlsx
    else:
        read_from = args.input_xlsx

    if args.make_dataset:
        if not args.system_prompt_path:
            raise ValueError("--system-prompt-path is required for --make-dataset")
        system_prompt = load_system_prompt(args.system_prompt_path)
        print(f"[dataset] Loaded system prompt from {args.system_prompt_path} "
              f"({len(system_prompt)} chars)")

        if df is None:
            print(f"[dataset] Reading {read_from} ...")
            df = pd.read_excel(read_from, dtype=str)
            if 'uid' not in df.columns:
                print("[warn] No 'uid' column found — run --stamp-xlsx first for join support.")
                df.insert(0, 'uid', [str(uuid.uuid4()) for _ in range(len(df))])

        build_rl_dataset(
            df=df,
            system_prompt=system_prompt,
            images_dir=args.images_dir,
            save_dir=args.save_dir,
            output_jsonl=args.output_jsonl,
        )


if __name__ == '__main__':
    main()
