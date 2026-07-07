#!/usr/bin/env python3
"""Compute Qwen3.5-VL prompt lengths for a parquet RL dataset.

This script mirrors VERL's multimodal RLHFDataset filtering path:
1. Replace <image> placeholders in prompt messages with Qwen vision content.
2. Render with the Qwen3.5 chat template, including <|vision_start|><|image_pad|><|vision_end|>.
3. Run the Qwen processor with the real images so image-pad expansion is counted.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image
from transformers import AutoProcessor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Statistic Qwen3.5-VL rendered prompt token lengths.")
    parser.add_argument(
        "--parquet",
        type=Path,
        default=Path("/cpfs01/nlp/leizhengxing/stock-rl/data/mixed_new_train_2_600_gold_train/train.parquet"),
        help="Input parquet dataset.",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=Path("/cpfs01/nlp/leizhengxing/stock-rl/data/Qwen3.5-0.8B"),
        help="Qwen3.5-VL model/processor path.",
    )
    parser.add_argument(
        "--max-prompt-length",
        type=int,
        default=5000,
        help="Threshold used to count overlong prompts.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("/cpfs01/nlp/leizhengxing/stock-rl/data/mixed_new_train_2_600_gold_train/prompt_length_stats.json"),
        help="Where to save the full per-row statistics.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=20,
        help="Number of longest prompts to print.",
    )
    parser.add_argument(
        "--add-vision-id",
        action="store_true",
        help="Pass add_vision_id=True to the Qwen chat template.",
    )
    parser.add_argument(
        "--tool-config",
        type=Path,
        default=None,
        help="Path to tool config YAML (e.g. stock_tool_config.yaml). "
             "When provided, tool schemas are included in the rendered prompt, "
             "matching what the agent loop does at inference time.",
    )
    return parser.parse_args()


def to_plain(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def normalize_messages(prompt: Any) -> list[dict[str, Any]]:
    prompt = to_plain(prompt)
    if not isinstance(prompt, list):
        raise TypeError(f"prompt must be a list of messages, got {type(prompt).__name__}")
    return copy.deepcopy(prompt)


def normalize_images(images: Any) -> list[Any]:
    if images is None:
        return []
    images = to_plain(images)
    if images is None:
        return []
    if isinstance(images, float) and pd.isna(images):
        return []
    if isinstance(images, dict):
        return [images]
    if isinstance(images, list):
        return images
    raise TypeError(f"images must be list/dict/None, got {type(images).__name__}")


def image_to_pil(image: Any) -> Image.Image:
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    if isinstance(image, dict):
        if "bytes" in image:
            return Image.open(BytesIO(image["bytes"])).convert("RGB")
        if "image" in image and isinstance(image["image"], Image.Image):
            return image["image"].convert("RGB")
    raise TypeError(f"Unsupported image object: {type(image).__name__}")


def build_qwen_messages(prompt: Any, images: Any) -> tuple[list[dict[str, Any]], list[Image.Image]]:
    messages = normalize_messages(prompt)
    image_items = normalize_images(images)
    pil_images: list[Image.Image] = []
    image_offset = 0

    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue

        parts: list[dict[str, Any]] = []
        segments = [segment for segment in re.split(r"(<image>)", content) if segment]
        for segment in segments:
            if segment == "<image>":
                if image_offset >= len(image_items):
                    raise ValueError(
                        f"Prompt has more <image> placeholders than image objects: {image_offset + 1} > {len(image_items)}"
                    )
                pil_image = image_to_pil(image_items[image_offset])
                pil_images.append(pil_image)
                parts.append({"type": "image", "image": pil_image})
                image_offset += 1
            else:
                parts.append({"type": "text", "text": segment})
        message["content"] = parts

    if image_offset != len(image_items):
        raise ValueError(f"Used {image_offset} images from prompt placeholders, but row has {len(image_items)} images")
    return messages, pil_images


def row_id(row: pd.Series, index: int) -> Any:
    extra_info = row.get("extra_info")
    if isinstance(extra_info, dict) and "index" in extra_info:
        return extra_info["index"]
    return index


def image_path_value(row: pd.Series) -> Any:
    value = row.get("image_path")
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def count_prompt_tokens(processor: Any, messages: list[dict[str, Any]], pil_images: list[Image.Image], add_vision_id: bool, tool_schemas: Any = None) -> tuple[int, str]:
    apply_kwargs: dict[str, Any] = {"add_generation_prompt": True, "tokenize": False}
    if add_vision_id:
        apply_kwargs["add_vision_id"] = True
    if tool_schemas is not None:
        apply_kwargs["tools"] = tool_schemas
    raw_prompt = processor.apply_chat_template(messages, **apply_kwargs)
    inputs = processor(text=[raw_prompt], images=pil_images or None, videos=None)
    input_ids = inputs["input_ids"][0]
    return len(input_ids), raw_prompt


def percentile(lengths: list[int], q: float) -> float:
    return float(np.percentile(np.asarray(lengths), q)) if lengths else 0.0


def main() -> None:
    args = parse_args()
    df = pd.read_parquet(args.parquet)
    processor = AutoProcessor.from_pretrained(args.model)

    tool_schemas = None
    if args.tool_config is not None:
        from verl.tools.utils.tool_registry import initialize_tools_from_config
        tool_list = initialize_tools_from_config(str(args.tool_config))
        tool_schemas = [
            t.tool_schema.model_dump(exclude_unset=True, exclude_none=True) for t in tool_list
        ]

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, row in df.iterrows():
        try:
            messages, pil_images = build_qwen_messages(row["prompt"], row.get("images"))
            token_length, raw_prompt = count_prompt_tokens(processor, messages, pil_images, args.add_vision_id, tool_schemas)
            records.append(
                {
                    "row_index": int(index),
                    "dataset_index": row_id(row, int(index)),
                    "token_length": int(token_length),
                    "text_length": len(raw_prompt),
                    "image_count": len(pil_images),
                    "image_path": image_path_value(row),
                    "over_max_prompt_length": bool(token_length > args.max_prompt_length),
                    "rendered_prompt_preview": raw_prompt[:1000],
                }
            )
        except Exception as exc:
            errors.append({"row_index": int(index), "dataset_index": row_id(row, int(index)), "error": repr(exc)})

    if not records:
        raise RuntimeError(f"No rows processed successfully. errors={errors[:3]}")

    records.sort(key=lambda item: item["token_length"], reverse=True)
    lengths = [item["token_length"] for item in records]
    overlong = [item for item in records if item["over_max_prompt_length"]]

    summary = {
        "parquet": str(args.parquet),
        "model": str(args.model),
        "rows_total": int(len(df)),
        "rows_processed": int(len(records)),
        "rows_failed": int(len(errors)),
        "max_prompt_length_threshold": int(args.max_prompt_length),
        "overlong_count": int(len(overlong)),
        "min_token_length": int(min(lengths)),
        "max_token_length": int(max(lengths)),
        "mean_token_length": float(np.mean(lengths)),
        "median_token_length": float(np.median(lengths)),
        "p90_token_length": percentile(lengths, 90),
        "p95_token_length": percentile(lengths, 95),
        "p99_token_length": percentile(lengths, 99),
        "longest": records[0],
        "top_longest": records[: args.top_k],
        "errors": errors,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Parquet: {args.parquet}")
    print(f"Model/processor: {args.model}")
    print(f"Rows total / processed / failed: {len(df)} / {len(records)} / {len(errors)}")
    print(f"Prompt length threshold: {args.max_prompt_length}")
    print(f"Overlong prompts: {len(overlong)}")
    print(
        "Token length stats: "
        f"min={summary['min_token_length']} "
        f"median={summary['median_token_length']:.1f} "
        f"mean={summary['mean_token_length']:.1f} "
        f"p90={summary['p90_token_length']:.1f} "
        f"p95={summary['p95_token_length']:.1f} "
        f"p99={summary['p99_token_length']:.1f} "
        f"max={summary['max_token_length']}"
    )
    print("\nTop longest prompts:")
    for rank, item in enumerate(records[: args.top_k], start=1):
        print(
            f"{rank:02d}. row_index={item['row_index']} "
            f"dataset_index={item['dataset_index']} "
            f"tokens={item['token_length']} "
            f"images={item['image_count']} "
            f"image_path={item['image_path']}"
        )
    print(f"\nSaved full stats: {args.output_json}")


if __name__ == "__main__":
    main()
