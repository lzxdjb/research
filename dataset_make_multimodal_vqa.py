"""
Download and convert OK-VQA / A-OKVQA / ScienceQA / MMMU / ViQuAE into RL format.

Loads datasets from HuggingFace Hub and outputs:
  - JSONL  : images replaced with <image> placeholder tag
  - Parquet: images as real PIL Image objects (stored in an 'images' column)

Note: ViQuAE rows contain image filenames plus source URLs, not decoded image
objects. The script downloads missing ViQuAE images into --viquae_images_dir
(default: <output_dir>/viquae_images), then resolves them via load_image_from_path().

Output files per dataset
------------------------
  <output_dir>/
    okvqa_train.jsonl / .parquet
    okvqa_val.jsonl   / .parquet
    aokvqa_train.jsonl / .parquet
    aokvqa_val.jsonl   / .parquet
    scienceqa_train.jsonl / .parquet
    scienceqa_val.jsonl   / .parquet
    mmmu_train.jsonl / .parquet
    mmmu_val.jsonl   / .parquet
    mmmu_test.jsonl     / .parquet
    mmmu_test_val.jsonl / .parquet  # compatibility alias for mmmu_test
    viquae_train.jsonl / .parquet
    viquae_val.jsonl   / .parquet
    viquae_test_val.jsonl / .parquet
    viquae_test.jsonl     / .parquet

Usage
-----
python dataset_make_multimodal_vqa.py \\
    --output_dir ./data/multimodal_vqa \\
    --val_ratio  0.05 \\
    --seed       42

# Add the new sources explicitly:
python dataset_make_multimodal_vqa.py \\
    --output_dir ./data/multimodal_vqa \\
    --datasets mmmu viquae \\
    --mmmu_train_splits dev validation test \\
    --mmmu_test_ratio 0.05 \\
    --viquae_images_dir ./data/viquae_images \\
    --viquae_download_delay 1.0
"""

import argparse
import ast
import hashlib
from io import BytesIO
import json
import os
import random
import re
import time
from collections import Counter
from functools import partial
from multiprocessing import Pool, cpu_count
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

import pandas as pd
from datasets import load_dataset
from PIL import Image


# ── System prompts ─────────────────────────────────────────────────────────────

OKVQA_SYSTEM_PROMPT = (
    "You are a visual question answering assistant with broad world knowledge. "
    "You will be shown an image and asked a question that may require outside knowledge "
    "beyond what is directly visible in the image.\n\n"
    "## Instructions\n"
    "1. Carefully observe the image.\n"
    "2. Use both what you see and your general world knowledge to answer the question.\n"
    "3. Output your final answer in exactly this format:\n\n"
    "<FINISHED>\n"
    "Answer: [your concise answer here]\n"
    "</FINISHED>\n\n"
    "The answer should be concise — typically one word or a short phrase."
)

AOKVQA_SYSTEM_PROMPT = (
    "You are a visual question answering assistant. "
    "You will be shown an image and a multiple-choice question. "
    "Select the single best answer from the given choices.\n\n"
    "## Instructions\n"
    "1. Carefully observe the image.\n"
    "2. Read the question and all choices.\n"
    "3. Use visual evidence and world knowledge to pick the best answer.\n"
    "4. Output your final answer in exactly this format:\n\n"
    "<FINISHED>\n"
    "Answer: [the exact text of your chosen option]\n"
    "</FINISHED>\n\n"
    "Do not include the option letter, only the exact choice text."
)

SCIENCEQA_SYSTEM_PROMPT = (
    "You are a science education assistant. "
    "You will be shown a question (sometimes with an accompanying image) and "
    "multiple answer choices. Select the single correct answer.\n\n"
    "## Instructions\n"
    "1. Read the question carefully. If an image is provided, examine it.\n"
    "2. Consider all answer choices.\n"
    "3. Apply scientific reasoning to identify the correct answer.\n"
    "4. Output your final answer in exactly this format:\n\n"
    "<FINISHED>\n"
    "Answer: [the exact text of the correct choice]\n"
    "</FINISHED>\n\n"
    "Do not include the option letter, only the exact choice text."
)

MMMU_SYSTEM_PROMPT = (
    "You are an expert multimodal reasoning assistant. "
    "You will be shown one or more images and a college-level question. "
    "Some questions include multiple-choice options and others require a short open answer.\n\n"
    "## Instructions\n"
    "1. Carefully inspect all provided images.\n"
    "2. Read the question and any answer choices.\n"
    "3. Use visual evidence and domain knowledge to solve the problem.\n"
    "4. If choices are provided, output the exact text of the best choice.\n"
    "5. Output your final answer in exactly this format:\n\n"
    "<FINISHED>\n"
    "Answer: [your concise answer here]\n"
    "</FINISHED>\n\n"
    "Do not include the option letter when answering multiple-choice questions."
)

VIQUAE_SYSTEM_PROMPT = (
    "You are a visual question answering assistant with broad encyclopedic knowledge. "
    "You will be shown an image and asked a question that may require recognizing the "
    "visual subject and using world knowledge.\n\n"
    "## Instructions\n"
    "1. Carefully observe the image.\n"
    "2. Use both the visual content and your general knowledge to answer.\n"
    "3. Output your final answer in exactly this format:\n\n"
    "<FINISHED>\n"
    "Answer: [your concise answer here]\n"
    "</FINISHED>\n\n"
    "The answer should be concise — typically one entity, date, place, or short phrase."
)


MMMU_CONFIGS = [
    "Accounting",
    "Agriculture",
    "Architecture_and_Engineering",
    "Art",
    "Art_Theory",
    "Basic_Medical_Science",
    "Biology",
    "Chemistry",
    "Clinical_Medicine",
    "Computer_Science",
    "Design",
    "Diagnostics_and_Laboratory_Medicine",
    "Economics",
    "Electronics",
    "Energy_and_Power",
    "Finance",
    "Geography",
    "History",
    "Literature",
    "Manage",
    "Marketing",
    "Materials",
    "Math",
    "Mechanical_Engineering",
    "Music",
    "Pharmacy",
    "Physics",
    "Psychology",
    "Public_Health",
    "Sociology",
]


# ── Image loader ───────────────────────────────────────────────────────────────

def load_image_from_path(image_path: str, images_dir: str) -> Image.Image | None:
    """Load a PIL Image from a file path, searching common locations."""
    candidates = [
        image_path,
        os.path.join(images_dir, os.path.basename(image_path)),
        os.path.join(images_dir, image_path),
    ]
    for p in candidates:
        if os.path.exists(p):
            return Image.open(p).copy()
    print(f"[WARN] Image not found: {image_path}")
    return None


# Matches: <image>, <image 1>, <image_1>, <image-1>, <image1>, <img 1>
IMAGE_TOKEN_RE = re.compile(r"<\s*(?:image|img)(?:[\s_-]*(\d+))?\s*>", re.IGNORECASE)
FINAL_IMAGE_TOKEN_RE = re.compile(r"<image>")

def pil_to_rgb(img):
    """Robustly convert HF/PIL image objects to RGB PIL.Image."""
    if img is None:
        return None

    if isinstance(img, Image.Image):
        return img.convert("RGB")

    # HuggingFace Image features may appear as dicts after parquet reload.
    if isinstance(img, dict):
        try:
            if img.get("bytes") is not None:
                return Image.open(BytesIO(img["bytes"])).convert("RGB")
            if img.get("path"):
                return Image.open(img["path"]).convert("RGB")
        except Exception:
            return None

    return None


def image_exists_for_path(image_path: str, images_dir: str) -> bool:
    """Check the same locations used by load_image_from_path without warning."""
    candidates = [
        image_path,
        os.path.join(images_dir, os.path.basename(image_path)),
        os.path.join(images_dir, image_path),
    ]
    return any(os.path.exists(p) for p in candidates)


def filename_from_url(image_url: str) -> str:
    parsed = urlparse(image_url)
    filename = os.path.basename(unquote(parsed.path))
    if filename:
        return filename
    digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()
    return f"viquae_{digest}.jpg"


def quote_url(image_url: str) -> str:
    parsed = urlparse(image_url)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            quote(unquote(parsed.path), safe="/%"),
            parsed.params,
            quote(unquote(parsed.query), safe="=&%"),
            parsed.fragment,
        )
    )


def https_url(image_url: str) -> str:
    parsed = urlparse(image_url)
    if parsed.scheme != "http":
        return image_url
    return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def original_url_from_thumb(image_url: str) -> str | None:
    parsed = urlparse(image_url)
    path_parts = unquote(parsed.path).split("/")
    if "thumb" not in path_parts or len(path_parts) < 2:
        return None
    thumb_idx = path_parts.index("thumb")
    original_parts = path_parts[:thumb_idx] + path_parts[thumb_idx + 1:-1]
    if len(original_parts) <= thumb_idx:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "/".join(original_parts), parsed.params, "", parsed.fragment))


def original_filename_from_image_path(image_path: str) -> str:
    filename = os.path.basename(image_path)
    return re.sub(r"^\d+px-", "", filename)


def special_filepath_url(image_path: str) -> str | None:
    filename = original_filename_from_image_path(image_path)
    if not filename:
        return None
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename)}"


def upload_original_url_from_image_path(image_path: str) -> str | None:
    filename = original_filename_from_image_path(image_path)
    if not filename:
        return None
    digest = hashlib.md5(filename.encode("utf-8")).hexdigest()
    quoted_filename = quote(filename)
    return f"https://upload.wikimedia.org/wikipedia/commons/{digest[0]}/{digest[:2]}/{quoted_filename}"


def viquae_url_candidates(image_url: str, image_path: str) -> list[str]:
    candidates = []
    for url in [
        image_url,
        https_url(image_url),
        original_url_from_thumb(image_url),
        upload_original_url_from_image_path(image_path),
    ]:
        if not url:
            continue
        candidates.append(url)
        candidates.append(https_url(url))
    special_url = special_filepath_url(image_path)
    if special_url:
        candidates.append(special_url)

    unique_candidates = []
    seen = set()
    for url in candidates:
        quoted = quote_url(url)
        if quoted and quoted not in seen:
            seen.add(quoted)
            unique_candidates.append(quoted)
    return unique_candidates


def acquire_file_lock(lock_path: Path, stale_seconds: float = 300.0, poll_seconds: float = 0.05) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_seconds:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(poll_seconds)


def release_file_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def wait_for_download_slot(target_dir: Path, min_delay: float) -> None:
    if min_delay <= 0:
        return

    stamp_path = target_dir / ".viquae_download_last_request"
    try:
        last_request = float(stamp_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        last_request = 0.0

    wait_seconds = last_request + min_delay - time.time()
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def mark_download_attempt(target_dir: Path) -> None:
    (target_dir / ".viquae_download_last_request").write_text(str(time.time()))


def retry_after_seconds(error: Exception) -> float | None:
    if not isinstance(error, HTTPError):
        return None
    retry_after = error.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


def try_download_candidate(
    candidate_url: str,
    target_path: Path,
    target_dir: Path,
    timeout: int,
    min_delay: float,
) -> tuple[bool, Exception | None]:
    lock_path = target_dir / ".viquae_download.lock"
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    acquire_file_lock(lock_path)
    try:
        if target_path.exists():
            return True, None
        wait_for_download_slot(target_dir, min_delay)
        request = Request(
            candidate_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; stock-rl-dataset-builder/1.0; contact: dataset-cache)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = response.read()
            with tmp_path.open("wb") as f:
                f.write(payload)
            os.replace(tmp_path, target_path)
            return True, None
        except Exception as e:
            if tmp_path.exists():
                tmp_path.unlink()
            return False, e
        finally:
            mark_download_attempt(target_dir)
    finally:
        release_file_lock(lock_path)


def download_image_from_url(
    image_url: str,
    image_path: str,
    images_dir: str,
    timeout: int = 30,
    min_delay: float = 1.0,
    max_retries: int = 3,
) -> str | None:
    """Download a URL image into images_dir and return the local filename."""
    if not image_url:
        return None

    filename = os.path.basename(image_path) if image_path else filename_from_url(image_url)
    if not filename:
        filename = filename_from_url(image_url)

    target_dir = Path(images_dir or ".")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    if target_path.exists():
        return filename

    candidates = viquae_url_candidates(image_url, image_path)
    last_error = None
    for attempt in range(max(1, max_retries)):
        for candidate_url in candidates:
            ok, error = try_download_candidate(candidate_url, target_path, target_dir, timeout, min_delay)
            if ok:
                return filename
            last_error = error

        if attempt < max_retries - 1:
            retry_after = retry_after_seconds(last_error)
            backoff = retry_after if retry_after is not None else min(60.0, min_delay * (2 ** attempt) * 4)
            time.sleep(backoff + random.uniform(0.0, min(1.0, min_delay)))

    print(f"[WARN] Image download failed: {image_url} ({last_error})")
    return None


def format_lettered_choices(choices: list[str]) -> str:
    return "\n".join(f"  {chr(65+i)}. {c}" for i, c in enumerate(choices))


def parse_options(options_raw) -> list[str]:
    if options_raw is None:
        return []
    if isinstance(options_raw, str):
        text = options_raw.strip()
        if not text:
            return []
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = text
    else:
        parsed = options_raw

    if isinstance(parsed, (list, tuple)):
        return [str(option).strip() for option in parsed if str(option).strip()]
    return [str(parsed).strip()] if str(parsed).strip() else []


def count_final_image_tokens(text: str) -> int:
    return len(FINAL_IMAGE_TOKEN_RE.findall(str(text or "")))


def normalize_mmmu_question_and_images(question: str, image_by_number: dict[int, Image.Image]):
    """
    Return (normalized_question, selected_images).

    Key behavior:
      - <image 3> uses image_3.
      - repeated <image 1> duplicates image_1 so VERL sees one image per token.
      - unnumbered <image> consumes images in image_1, image_2, ... order.
      - if a placeholder points to a missing image, drop the row.
      - if there are images but no placeholders, prepend all images.
    """
    text = str(question or "").strip()
    if not text:
        return None, []

    matches = list(IMAGE_TOKEN_RE.finditer(text))
    sorted_images = [image_by_number[k] for k in sorted(image_by_number)]

    # Text-only or image columns without explicit placeholders.
    if not matches:
        if sorted_images:
            return "\n".join(["<image>"] * len(sorted_images) + [text]), sorted_images
        return text, []

    selected_images = []
    unnumbered_offset = 0

    for match in matches:
        image_num = match.group(1)

        if image_num is not None:
            img = image_by_number.get(int(image_num))
        else:
            img = sorted_images[unnumbered_offset] if unnumbered_offset < len(sorted_images) else None
            unnumbered_offset += 1

        if img is None:
            return None, []

        selected_images.append(img)

    normalized = IMAGE_TOKEN_RE.sub("<image>", text).strip()

    if count_final_image_tokens(normalized) != len(selected_images):
        return None, []

    return normalized, selected_images


def answer_letter_to_choice(answer, choices: list[str]) -> str:
    answer_text = str(answer).strip()
    if len(answer_text) == 1 and choices:
        choice_idx = ord(answer_text.upper()) - ord("A")
        if 0 <= choice_idx < len(choices):
            return choices[choice_idx]
    return answer_text


def unique_texts(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    elif not isinstance(values, (list, tuple, set)):
        values = [values]

    seen = set()
    texts = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            texts.append(text)
    return texts


# ── Shared RL sample builder ───────────────────────────────────────────────────

def _make_sample(
    data_source: str,
    ability: str,
    reward_key: str,
    system_prompt: str,
    question_text: str,
    ground_truth: str,
    index: int,
    image: Image.Image | list[Image.Image] | None,
    extra: dict | None = None,
) -> dict:
    if image is None:
        images = []
    elif isinstance(image, list):
        images = [img for img in image if img is not None]
    else:
        images = [image]

    n_image_tokens = count_final_image_tokens(question_text)
    if n_image_tokens != len(images):
        raise ValueError(
            f"{data_source} idx={index}: prompt has {n_image_tokens} <image> tokens "
            f"but images has {len(images)} items"
        )

    return {
        "data_source": data_source,
        "agent_name": "hotpot_qa_agent",
        "prompt": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question_text},
        ],
        "images": images,
        "ability": ability,
        "reward_model": {
            "style": "rule",
            "ground_truth": ground_truth,
        },
        "extra_info": {
            "index": index,
            "ground_truth": ground_truth,
            "need_tools_kwargs": True,
            "tools_kwargs": {
                reward_key: {
                    "create_kwargs": {"ground_truth": ground_truth},
                }
            },
            "interaction_kwargs": {"ground_truth": ground_truth},
            "meta_json": json.dumps(extra or {}, ensure_ascii=False),
        },
    }

# ── Per-dataset processors ─────────────────────────────────────────────────────

def process_okvqa(idx_and_row):
    idx, row = idx_and_row
    try:
        question = row.get("question", "").strip()
        if not question:
            return None

        answers_raw = row.get("answers") or row.get("answers_original") or []
        if not answers_raw:
            return None

        if isinstance(answers_raw[0], dict):
            answer_texts = [a["answer"] for a in answers_raw if isinstance(a, dict) and a.get("answer")]
        else:
            answer_texts = [str(a).strip() for a in answers_raw if str(a).strip()]

        if not answer_texts:
            return None

        image = pil_to_rgb(row.get("image"))
        if image is None:
            return None

        # Avoid accidental extra placeholder from source question text.
        question = IMAGE_TOKEN_RE.sub("", question).strip()
        if not question:
            return None

        ground_truth = Counter(answer_texts).most_common(1)[0][0]
        question_text = f"<image>\n{question}"

        return _make_sample(
            data_source="okvqa",
            ability="visual_question_answering",
            reward_key="calc_vqa_reward",
            system_prompt=OKVQA_SYSTEM_PROMPT,
            question_text=question_text,
            ground_truth=ground_truth,
            index=idx,
            image=image,
            extra={
                "question_id": str(row.get("question_id", idx)),
                "answer_votes": Counter(answer_texts).most_common(),
            },
        )
    except Exception as e:
        print(f"[WARN] okvqa idx={idx} skipped: {e}")
        return None


def process_aokvqa(idx_and_row):
    idx, row = idx_and_row
    try:
        question = row.get("question", "").strip()
        choices  = row.get("choices", [])
        correct  = row.get("correct_choice_idx", None)
        if not question or not choices or correct is None:
            return None

        ground_truth = choices[int(correct)]

        # Format choices into the question
        lettered = "\n".join(f"  {chr(65+i)}. {c}" for i, c in enumerate(choices))
        question_text = f"<image>\n{question}\n\nChoices:\n{lettered}"

        image: Image.Image | None = pil_to_rgb(row.get("image"))

        return _make_sample(
            data_source  = "aokvqa",
            ability      = "visual_question_answering",
            reward_key   = "calc_vqa_reward",
            system_prompt= AOKVQA_SYSTEM_PROMPT,
            question_text= question_text,
            ground_truth = ground_truth,
            index        = idx,
            image        = image,
            extra        = {
                "question_id": str(row.get("question_id", idx)),
                "choices":     choices,
                "rationales":  row.get("rationales", []),
            },
        )
    except Exception as e:
        print(f"[WARN] aokvqa idx={idx} skipped: {e}")
        return None


def process_scienceqa(idx_and_row):
    idx, row = idx_and_row
    try:
        question = row.get("question", "").strip()
        choices  = row.get("choices", [])
        answer   = row.get("answer", None)   # integer index
        image    = row.get("image")          # PIL image or None

        if not question or not choices or answer is None:
            return None

        ground_truth = choices[int(answer)]

        lettered = "\n".join(f"  {chr(65+i)}. {c}" for i, c in enumerate(choices))
        if image is not None:
            question_text = f"<image>\n{question}\n\nChoices:\n{lettered}"
        else:
            question_text = f"{question}\n\nChoices:\n{lettered}"

        image = pil_to_rgb(image)

        return _make_sample(
            data_source  = "scienceqa",
            ability      = "science_question_answering",
            reward_key   = "calc_vqa_reward",
            system_prompt= SCIENCEQA_SYSTEM_PROMPT,
            question_text= question_text,
            ground_truth = ground_truth,
            index        = idx,
            image        = image,
            extra        = {
                "subject": row.get("subject", ""),
                "topic":   row.get("topic", ""),
                "lecture": row.get("lecture", ""),
            },
        )
    except Exception as e:
        print(f"[WARN] scienceqa idx={idx} skipped: {e}")
        return None


def process_mmmu(idx_and_row):
    idx, row = idx_and_row
    try:
        question = str(row.get("question", "")).strip()
        answer = row.get("answer", None)
        if not question or answer is None or not str(answer).strip():
            return None

        image_by_number = {}
        for image_idx in range(1, 8):
            image = pil_to_rgb(row.get(f"image_{image_idx}"))
            if image is not None:
                image_by_number[image_idx] = image

        choices = parse_options(row.get("options"))
        ground_truth = answer_letter_to_choice(answer, choices) if choices else str(answer).strip()
        if not ground_truth:
            return None

        question_text, images = normalize_mmmu_question_and_images(question, image_by_number)
        if question_text is None:
            return None
        image_columns = [f"image_{i}" for i in sorted(image_by_number)]
        used_image_count = len(images)
        if choices:
            question_text = f"{question_text}\n\nChoices:\n{format_lettered_choices(choices)}"

        return _make_sample(
            data_source  = "mmmu",
            ability      = "multimodal_reasoning",
            reward_key   = "calc_vqa_reward",
            system_prompt= MMMU_SYSTEM_PROMPT,
            question_text= question_text,
            ground_truth = ground_truth,
            index        = idx,
            image        = images,
            extra        = {
                "id": str(row.get("id", idx)),
                "config": row.get("_config", ""),
                "split": row.get("_split", ""),
                "question_type": row.get("question_type", ""),
                "answer": str(answer),
                "options": choices,
                "image_columns": image_columns,
                "used_image_count": used_image_count,
                "subfield": row.get("subfield", ""),
                "topic_difficulty": row.get("topic_difficulty", ""),
                "explanation": row.get("explanation", ""),
            },
        )
    except Exception as e:
        print(f"[WARN] mmmu idx={idx} skipped: {e}")
        return None


def viquae_answers(row) -> list[str]:
    output = row.get("output") or {}
    answers = []
    if isinstance(output, dict):
        answers.extend(unique_texts(output.get("original_answer")))
        answers.extend(unique_texts(output.get("answer")))
        answers.extend(unique_texts(output.get("answers")))
    elif isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                answers.extend(unique_texts(item.get("original_answer")))
                answers.extend(unique_texts(item.get("answer")))
            else:
                answers.extend(unique_texts(item))

    answers.extend(unique_texts(row.get("answer")))
    answers.extend(unique_texts(row.get("answers")))
    return unique_texts(answers)


def process_viquae(idx_and_row, images_dir: str = "", download_delay: float = 1.0, download_retries: int = 3):
    idx, row = idx_and_row
    try:
        question = str(
            row.get("input")
            or row.get("question")
            or row.get("original_question")
            or ""
        ).strip()
        answers = viquae_answers(row)
        if not question or not answers:
            return None

        image_path = str(
            row.get("image")
            or row.get("image_path")
            or row.get("filename")
            or ""
        ).strip()
        image_url = str(row.get("url") or "").strip()
        if not image_path and image_url:
            image_path = filename_from_url(image_url)
        if not image_path:
            return None

        if not image_exists_for_path(image_path, images_dir) and image_url:
            downloaded_path = download_image_from_url(
                image_url,
                image_path,
                images_dir,
                min_delay=download_delay,
                max_retries=download_retries,
            )
            if downloaded_path:
                image_path = downloaded_path

        if not image_exists_for_path(image_path, images_dir):
            return None

        image = pil_to_rgb(load_image_from_path(image_path, images_dir))
        if image is None:
            return None

        ground_truth = answers[0]
        question_text = f"<image>\n{question}"

        return _make_sample(
            data_source  = "viquae",
            ability      = "visual_question_answering",
            reward_key   = "calc_vqa_reward",
            system_prompt= VIQUAE_SYSTEM_PROMPT,
            question_text= question_text,
            ground_truth = ground_truth,
            index        = idx,
            image        = image,
            extra        = {
                "id": str(row.get("id", idx)),
                "kilt_id": row.get("kilt_id", ""),
                "wikidata_id": row.get("wikidata_id", ""),
                "image_path": image_path,
                "url": row.get("url", ""),
                "answer_aliases": answers,
                "original_question": row.get("original_question", ""),
                "meta": row.get("meta", {}),
            },
        )
    except Exception as e:
        print(f"[WARN] viquae idx={idx} skipped: {e}")
        return None


# ── Parallel builder ───────────────────────────────────────────────────────────

def build_records(rows, processor_fn, num_workers: int, max_samples: int | None = None) -> list[dict]:
    if max_samples is not None:
        rows = rows[:max_samples]
    args = list(enumerate(rows))
    with Pool(num_workers) as pool:
        results = list(pool.imap_unordered(processor_fn, args, chunksize=32))
    records = [r for r in results if r is not None]
    # Re-assign index in insertion order
    records.sort(key=lambda r: r["extra_info"]["index"])
    for i, r in enumerate(records):
        r["extra_info"]["index"] = i
    print(f"  built {len(records)} records from {len(rows)} raw samples")
    return records


# ── I/O helpers ────────────────────────────────────────────────────────────────

def write_jsonl(path: Path, records: list[dict]) -> None:
    """Write JSONL — replace PIL images with <image> string placeholder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            row = dict(r)
            row["images"] = ["<image>"] * len(r.get("images", []))
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  JSONL → {path}  ({len(records)} rows)")


def write_parquet(path: Path, records: list[dict]) -> None:
    """Write Parquet — keeps PIL Image objects as-is (datasets/arrow handles them)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # pandas can't serialize PIL images; use HuggingFace datasets for parquet
    import datasets as hf_datasets
    ds = hf_datasets.Dataset.from_list(records)
    ds.to_parquet(str(path))
    print(f"  Parquet → {path}  ({len(records)} rows)")


def write_split(stem: Path, records: list[dict], label: str) -> None:
    print(f"\n  [{label}] {len(records)} samples")
    write_jsonl(stem.with_suffix(".jsonl"), records)
    write_parquet(stem.with_suffix(".parquet"), records)


# ── Val split helper ───────────────────────────────────────────────────────────

def split_train_val(records: list[dict], val_ratio: float, seed: int):
    rng = random.Random(seed)
    shuffled = records[:]
    rng.shuffle(shuffled)
    val_size = max(1, int(len(shuffled) * val_ratio))
    return shuffled[val_size:], shuffled[:val_size]


def split_train_val_test(records: list[dict], val_ratio: float, test_ratio: float, seed: int):
    rng = random.Random(seed)
    shuffled = records[:]
    rng.shuffle(shuffled)

    test_size = int(len(shuffled) * test_ratio)
    if test_ratio > 0 and len(shuffled) >= 3:
        test_size = max(1, test_size)
    test_size = min(test_size, max(0, len(shuffled) - 2))

    test_recs = shuffled[:test_size]
    train_val_recs = shuffled[test_size:]

    val_size = int(len(train_val_recs) * val_ratio)
    if val_ratio > 0 and len(train_val_recs) >= 2:
        val_size = max(1, val_size)
    val_size = min(val_size, max(0, len(train_val_recs) - 1))

    val_recs = train_val_recs[:val_size]
    train_recs = train_val_recs[val_size:]
    return train_recs, val_recs, test_recs


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Build RL datasets for OK-VQA / A-OKVQA / ScienceQA / MMMU / ViQuAE"
    )
    p.add_argument("--output_dir",   required=True, help="Root output directory")
    p.add_argument("--val_ratio",    type=float, default=0.05,
                   help="Fraction of train held out as val (default 0.05)")
    p.add_argument("--seed",         type=int, default=42)
    p.add_argument("--num_workers",  type=int, default=min(cpu_count(), 16))
    p.add_argument("--max_per_source", type=int, default=None,
                   help="Max samples taken from each dataset source (default: all)")
    p.add_argument("--datasets",     nargs="+",
                   choices=["okvqa", "aokvqa", "scienceqa", "mmmu", "viquae"],
                   default=["okvqa", "aokvqa", "scienceqa"],
                   help="Which datasets to process (default: okvqa aokvqa scienceqa)")
    p.add_argument("--mmmu_configs", nargs="+", default=MMMU_CONFIGS,
                   help="MMMU subject configs to process (default: all MMMU configs)")
    p.add_argument("--mmmu_train_splits", nargs="+", choices=["dev", "validation", "test"],
                   default=["dev", "validation", "test"],
                   help="MMMU splits to mix before train/val/test splitting (default: dev validation test)")
    p.add_argument("--mmmu_test_ratio", type=float, default=0.05,
                   help="Held-out test fraction from mixed MMMU pool (default: 0.05)")
    p.add_argument("--viquae_images_dir", default="",
                   help="Directory used to cache/load ViQuAE URL images (default: <output_dir>/viquae_images)")
    p.add_argument("--viquae_download_delay", type=float, default=1.0,
                   help="Minimum seconds between ViQuAE URL requests across workers (default: 1.0)")
    p.add_argument("--viquae_download_retries", type=int, default=3,
                   help="Retry passes for ViQuAE URL downloads after trying all fallbacks (default: 3)")
    args = p.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    all_train_recs: list[dict] = []   # accumulates every source's train records

    # ── OK-VQA ──────────────────────────────────────────────────────────────
    if "okvqa" in args.datasets:
        print("\n========== OK-VQA ==========")
        print("  Loading Multimodal-Fatima/OK-VQA_train …")
        ds = load_dataset("Multimodal-Fatima/OK-VQA_train", trust_remote_code=True)
        # This dataset typically only has a 'train' split
        split_names = list(ds.keys())
        print(f"  Available splits: {split_names}")
        train_rows = list(ds[split_names[0]])
        print(f"  Raw samples: {len(train_rows)}")
        records = build_records(train_rows, process_okvqa, args.num_workers, args.max_per_source)
        train_recs, val_recs = split_train_val(records, args.val_ratio, args.seed)
        write_split(out / "okvqa_train", train_recs, "okvqa train")
        write_split(out / "okvqa_val",   val_recs,   "okvqa val")
        all_train_recs.extend(train_recs)

    # ── A-OKVQA ─────────────────────────────────────────────────────────────
    if "aokvqa" in args.datasets:
        print("\n========== A-OKVQA ==========")
        print("  Loading HuggingFaceM4/A-OKVQA …")
        ds = load_dataset("HuggingFaceM4/A-OKVQA", trust_remote_code=True)
        split_names = list(ds.keys())
        print(f"  Available splits: {split_names}")

        if "train" in ds:
            train_rows = list(ds["train"])
            print(f"  Raw train samples: {len(train_rows)}")
            records = build_records(train_rows, process_aokvqa, args.num_workers, args.max_per_source)
            train_recs, val_recs = split_train_val(records, args.val_ratio, args.seed)
            write_split(out / "aokvqa_train", train_recs, "aokvqa train")
            write_split(out / "aokvqa_val",   val_recs,   "aokvqa val")
            all_train_recs.extend(train_recs)

        if "validation" in ds:
            test_rows = list(ds["validation"])
            print(f"  Raw validation samples: {len(test_rows)}")
            test_recs = build_records(test_rows, process_aokvqa, args.num_workers, args.max_per_source)
            write_split(out / "aokvqa_test", test_recs, "aokvqa test (validation)")

    # ── ScienceQA ────────────────────────────────────────────────────────────
    if "scienceqa" in args.datasets:
        print("\n========== ScienceQA ==========")
        print("  Loading derek-thomas/ScienceQA …")
        ds = load_dataset("derek-thomas/ScienceQA", trust_remote_code=True)
        split_names = list(ds.keys())
        print(f"  Available splits: {split_names}")

        if "train" in ds:
            train_rows = list(ds["train"])
            print(f"  Raw train samples: {len(train_rows)}")
            records = build_records(train_rows, process_scienceqa, args.num_workers, args.max_per_source)
            train_recs, val_recs = split_train_val(records, args.val_ratio, args.seed)
            write_split(out / "scienceqa_train", train_recs, "scienceqa train")
            write_split(out / "scienceqa_val",   val_recs,   "scienceqa val")
            all_train_recs.extend(train_recs)

        if "validation" in ds:
            val_rows = list(ds["validation"])
            print(f"  Raw validation samples: {len(val_rows)}")
            val_recs = build_records(val_rows, process_scienceqa, args.num_workers, args.max_per_source)
            write_split(out / "scienceqa_test_val", val_recs, "scienceqa test (validation)")

        if "test" in ds:
            test_rows = list(ds["test"])
            print(f"  Raw test samples: {len(test_rows)}")
            test_recs = build_records(test_rows, process_scienceqa, args.num_workers, args.max_per_source)
            write_split(out / "scienceqa_test", test_recs, "scienceqa test")

    # ── MMMU ──────────────────────────────────────────────────────────────────
    if "mmmu" in args.datasets:
        print("\n========== MMMU ==========")
        rows_by_split: dict[str, list[dict]] = {"dev": [], "validation": [], "test": []}
        for config in args.mmmu_configs:
            print(f"  Loading MMMU/MMMU [{config}] …")
            ds = load_dataset("MMMU/MMMU", config)
            split_names = list(ds.keys())
            print(f"    Available splits: {split_names}")
            for split_name in rows_by_split:
                if split_name not in ds:
                    continue
                split_rows = []
                for row in ds[split_name]:
                    row = dict(row)
                    row["_config"] = config
                    row["_split"] = split_name
                    split_rows.append(row)
                rows_by_split[split_name].extend(split_rows)
                print(f"    {split_name}: +{len(split_rows)} samples")

        selected_rows = []
        for split_name in args.mmmu_train_splits:
            split_rows = rows_by_split.get(split_name, [])
            selected_rows.extend(split_rows)
            print(f"  MMMU mixed source {split_name}: {len(split_rows)} samples")

        if selected_rows:
            rng = random.Random(args.seed)
            rng.shuffle(selected_rows)
            print(f"  Raw mixed MMMU samples: {len(selected_rows)}")
            records = build_records(selected_rows, process_mmmu, args.num_workers, args.max_per_source)
            train_recs, val_recs, test_recs = split_train_val_test(
                records, args.val_ratio, args.mmmu_test_ratio, args.seed
            )
            write_split(out / "mmmu_train", train_recs, "mmmu train (mixed splits)")
            write_split(out / "mmmu_val",   val_recs,   "mmmu val (held-out mixed)")
            if test_recs:
                write_split(out / "mmmu_test", test_recs, "mmmu test (held-out mixed)")
                write_split(out / "mmmu_test_val", test_recs, "mmmu test_val alias (held-out mixed)")
            all_train_recs.extend(train_recs)
        else:
            print("  [WARN] No MMMU rows selected; check --mmmu_train_splits")

    # ── ViQuAE ─────────────────────────────────────────────────────────────────
    if "viquae" in args.datasets:
        print("\n========== ViQuAE ==========")
        viquae_images_dir = args.viquae_images_dir or str(out / "viquae_images")
        print(f"  ViQuAE image cache: {viquae_images_dir}")
        print("  Loading PaulLerner/viquae_dataset …")
        ds = load_dataset("PaulLerner/viquae_dataset")
        split_names = list(ds.keys())
        print(f"  Available splits: {split_names}")
        viquae_processor = partial(
            process_viquae,
            images_dir=viquae_images_dir,
            download_delay=args.viquae_download_delay,
            download_retries=args.viquae_download_retries,
        )

        if "train" in ds:
            train_rows = list(ds["train"])
            print(f"  Raw train samples: {len(train_rows)}")
            records = build_records(train_rows, viquae_processor, args.num_workers, args.max_per_source)
            if records:
                train_recs, val_recs = split_train_val(records, args.val_ratio, args.seed)
                write_split(out / "viquae_train", train_recs, "viquae train")
                write_split(out / "viquae_val",   val_recs,   "viquae val")
                all_train_recs.extend(train_recs)
            else:
                print("  [WARN] No ViQuAE train records built; check --viquae_images_dir")

        if "validation" in ds:
            val_rows = list(ds["validation"])
            print(f"  Raw validation samples: {len(val_rows)}")
            val_recs = build_records(val_rows, viquae_processor, args.num_workers, args.max_per_source)
            if val_recs:
                write_split(out / "viquae_test_val", val_recs, "viquae test (validation)")
            else:
                print("  [WARN] No ViQuAE validation records built; check --viquae_images_dir")

        if "test" in ds:
            test_rows = list(ds["test"])
            print(f"  Raw test samples: {len(test_rows)}")
            test_recs = build_records(test_rows, viquae_processor, args.num_workers, args.max_per_source)
            if test_recs:
                write_split(out / "viquae_test", test_recs, "viquae test")
            else:
                print("  [WARN] No ViQuAE test records built; check --viquae_images_dir")

    # ── Mixed train set (all sources combined) ───────────────────────────────
    if all_train_recs:
        print("\n========== Mixed train ==========")
        rng = random.Random(args.seed)
        rng.shuffle(all_train_recs)
        for i, r in enumerate(all_train_recs):
            r["extra_info"]["index"] = i
        write_split(out / "vqa_mix_train", all_train_recs, "vqa_mix train")

    print("\n\nAll done.")


if __name__ == "__main__":
    main()
