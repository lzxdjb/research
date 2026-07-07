#!/usr/bin/env python3
"""Use a local large model to diversify simulator SFT data without external APIs.

The expected backend is an OpenAI-compatible local server, for example vLLM or
SGLang serving your 122B/10B-active teacher model. The script reads JSONL rows
with a ``messages`` field and rewrites only the final assistant JSON while
preserving the hidden facts.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SYSTEM = """You augment simulator training data for brokerage onboarding.
Rewrite the final assistant JSON so that:
- It remains valid JSON.
- It keeps the same keys: current_answer, thought, response.
- The response is a natural human reply consistent with the hidden scenario.
- No new facts are invented.
- Hidden scenario JSON is never revealed."""


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _post(endpoint: str, payload: dict[str, Any], timeout: float, retries: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if attempt >= retries:
                raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except Exception:
            if attempt >= retries:
                raise
        time.sleep(min(2**attempt, 8))
    raise RuntimeError("unreachable")


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("teacher output is not a JSON object")
    return value


def _augment_row(row: dict[str, Any], args) -> dict[str, Any]:
    source_messages = row["messages"]
    payload = {
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "Original training row:\n"
                    + json.dumps({"messages": source_messages}, ensure_ascii=False)
                    + "\n\nReturn only the rewritten final assistant JSON."
                ),
            },
        ],
    }
    response = _post(args.endpoint, payload, args.timeout, args.retries)
    text = response["choices"][0]["message"]["content"]
    final_json = _extract_json(text)

    out = dict(row)
    messages = [dict(m) for m in source_messages]
    messages[-1] = {"role": "assistant", "content": json.dumps(final_json, ensure_ascii=False)}
    out["messages"] = messages
    out["augmented_by"] = args.model
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/digital_onboarding/sim_user_sft.jsonl")
    parser.add_argument("--output", default="data/digital_onboarding/sim_user_sft_teacher_aug.jsonl")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000/v1/chat/completions")
    parser.add_argument("--model", required=True, help="Local teacher model name served by your backend.")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--limit", type=int, default=-1)
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    with output_path.open("w", encoding="utf-8") as f:
        for row in _iter_jsonl(input_path):
            if args.limit >= 0 and count >= args.limit:
                break
            try:
                out = _augment_row(row, args)
            except Exception as exc:
                out = dict(row)
                out["augmentation_error"] = str(exc)
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            count += 1
    print(f"Wrote {count} rows to {output_path}")


if __name__ == "__main__":
    main()
