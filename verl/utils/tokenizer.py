# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utils for tokenization."""

import json
import types
import warnings
from pathlib import Path

__all__ = ["hf_tokenizer", "hf_processor", "normalize_token_ids"]


def normalize_token_ids(tokenized_output) -> list[int]:
    """Normalize tokenizer outputs into a flat ``list[int]``.

    This handles Transformers 4/5 differences where ``apply_chat_template(tokenize=True)``
    may return either ``list[int]`` or a ``BatchEncoding``/mapping with ``input_ids``.
    """

    token_ids = tokenized_output
    if isinstance(tokenized_output, dict):
        if "input_ids" in tokenized_output:
            token_ids = tokenized_output["input_ids"]
    elif hasattr(tokenized_output, "input_ids"):
        token_ids = tokenized_output.input_ids

    if hasattr(token_ids, "tolist"):
        token_ids = token_ids.tolist()

    if isinstance(token_ids, tuple):
        token_ids = list(token_ids)

    if isinstance(token_ids, list) and len(token_ids) == 1 and isinstance(token_ids[0], list | tuple):
        token_ids = list(token_ids[0])

    if not isinstance(token_ids, list):
        raise TypeError(f"token_ids must be list-like token ids, got {type(token_ids).__name__}: {token_ids!r}")

    normalized_ids = []
    for idx, token_id in enumerate(token_ids):
        if hasattr(token_id, "item"):
            token_id = token_id.item()
        try:
            normalized_ids.append(int(token_id))
        except (TypeError, ValueError) as e:
            raise TypeError(f"token_id must be int-convertible, got {type(token_id).__name__}: {token_id!r}") from e
    return normalized_ids


def set_pad_token_id(tokenizer):
    """Set pad_token_id to eos_token_id if it is None.

    Args:
        tokenizer (transformers.PreTrainedTokenizer): The tokenizer to be set.

    """
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id
        warnings.warn(f"tokenizer.pad_token_id is None. Now set to {tokenizer.eos_token_id}", stacklevel=1)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        warnings.warn(f"tokenizer.pad_token is None. Now set to {tokenizer.eos_token}", stacklevel=1)


def _load_local_chat_template(name_or_path) -> str | None:
    if not isinstance(name_or_path, str):
        return None

    local_path = Path(name_or_path).expanduser()
    if not local_path.exists():
        return None

    candidates = []
    if local_path.is_dir():
        candidates.extend(
            [
                local_path / "chat_template.json",
                local_path / "chat_template.jinja",
                local_path / "chat_template.jinja2",
                local_path / "chat_template.txt",
                local_path / "tokenizer_config.json",
            ]
        )
    else:
        candidates.append(local_path)

    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue

        if candidate.name == "tokenizer_config.json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            template = payload.get("chat_template")
            if isinstance(template, str) and template.strip():
                return template
            continue

        if candidate.suffix == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return text
            if isinstance(payload, dict):
                template = payload.get("chat_template")
                if isinstance(template, str) and template.strip():
                    return template
                if len(payload) == 1:
                    maybe_template = next(iter(payload.values()))
                    if isinstance(maybe_template, str) and maybe_template.strip():
                        return maybe_template
            elif isinstance(payload, str) and payload.strip():
                return payload
            continue

        return text

    return None


def _attach_local_chat_template(processing_class, name_or_path) -> None:
    if getattr(processing_class, "chat_template", None):
        return
    chat_template = _load_local_chat_template(name_or_path)
    if chat_template:
        processing_class.chat_template = chat_template


_MM_NESTED_CONFIG_FIELDS = (
    "thinker_config",
    "talker_config",
    "text_config",
    "vision_config",
    "audio_config",
    "llm_config",
    "language_config",
    "decoder_config",
    "model_config",
)


def _get_attr(source, name: str):
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _iter_mm_sources(processor, config=None):
    if config is None:
        config = getattr(processor, "config", None)

    seen: set[int] = set()
    candidates = [config, processor, getattr(processor, "tokenizer", None)]

    while candidates:
        source = candidates.pop(0)
        if source is None:
            continue
        source_id = id(source)
        if source_id in seen:
            continue
        seen.add(source_id)
        yield source
        for nested_name in _MM_NESTED_CONFIG_FIELDS:
            nested_source = _get_attr(source, nested_name)
            if nested_source is not None and id(nested_source) not in seen:
                candidates.append(nested_source)


def resolve_mm_value(processor, attr_name: str, config=None):
    """Resolve a multimodal attribute from processor/config/tokenizer sources."""

    for source in _iter_mm_sources(processor, config=config):
        value = _get_attr(source, attr_name)
        if value is not None:
            return value
    return None


def resolve_mm_token_id(processor, id_attr_name: str, token_attr_names: tuple[str, ...] = (), config=None):
    """Resolve a multimodal token id from either id or token attributes."""

    tokenizer = getattr(processor, "tokenizer", None)

    value = resolve_mm_value(processor, id_attr_name, config=config)
    if value is not None:
        if hasattr(value, "item"):
            value = value.item()
        try:
            return int(value)
        except (TypeError, ValueError):
            pass

    for token_attr_name in token_attr_names:
        token = resolve_mm_value(processor, token_attr_name, config=config)
        if token is None:
            continue
        if hasattr(token, "item"):
            token = token.item()
        if isinstance(token, int):
            return int(token)
        if tokenizer is None:
            continue
        convert = getattr(tokenizer, "convert_tokens_to_ids", None)
        if callable(convert):
            token_id = convert(token)
            if hasattr(token_id, "item"):
                token_id = token_id.item()
            if token_id is not None:
                try:
                    return int(token_id)
                except (TypeError, ValueError):
                    continue

    return None


def ensure_multimodal_processor_compatibility(processor, config=None):
    """Populate legacy multimodal token attributes on processor/config objects."""

    if processor is None:
        return None

    if config is None:
        config = getattr(processor, "config", None)

    compat_fields = {
        "image_token_id": ("image_token",),
        "video_token_id": ("video_token",),
        "audio_token_id": ("audio_token",),
        "vision_start_token_id": ("vision_bos_token", "vision_start_token"),
        "audio_start_token_id": ("audio_bos_token", "audio_start_token"),
        "vision_end_token_id": ("vision_eos_token", "vision_end_token"),
        "audio_end_token_id": ("audio_eos_token", "audio_end_token"),
        "spatial_merge_size": (),
    }

    for attr_name, token_attr_names in compat_fields.items():
        value = resolve_mm_token_id(processor, attr_name, token_attr_names=token_attr_names, config=config)
        if value is None:
            continue
        setattr(processor, attr_name, value)
        if config is not None:
            setattr(config, attr_name, value)

    position_id_per_seconds = resolve_mm_value(processor, "position_id_per_seconds", config=config)
    if position_id_per_seconds is not None:
        try:
            position_id_per_seconds = int(position_id_per_seconds)
        except (TypeError, ValueError):
            pass
        setattr(processor, "position_id_per_seconds", position_id_per_seconds)
        if config is not None:
            setattr(config, "position_id_per_seconds", position_id_per_seconds)

    return processor


def hf_tokenizer(name_or_path, correct_pad_token=True, correct_gemma2=True, **kwargs):
    """Create a huggingface pretrained tokenizer which correctness handles eos and pad tokens.

    Args:

        name (str): The name of the tokenizer.
        correct_pad_token (bool): Whether to correct the pad token id.
        correct_gemma2 (bool): Whether to correct the gemma2 tokenizer.

    Returns:

        transformers.PreTrainedTokenizer: The pretrained tokenizer.

    """
    from transformers import AutoTokenizer

    if correct_gemma2 and isinstance(name_or_path, str) and "gemma-2-2b-it" in name_or_path:
        # the EOS token in gemma2 is ambiguious, which may worsen RL performance.
        # https://huggingface.co/google/gemma-2-2b-it/commit/17a01657f5c87135bcdd0ec7abb4b2dece04408a
        warnings.warn(
            "Found gemma-2-2b-it tokenizer. Set eos_token and eos_token_id to <end_of_turn> and 107.", stacklevel=1
        )
        kwargs["eos_token"] = "<end_of_turn>"
        kwargs["eos_token_id"] = 107
    tokenizer = AutoTokenizer.from_pretrained(name_or_path, **kwargs)
    if correct_pad_token:
        set_pad_token_id(tokenizer)
    _attach_local_chat_template(tokenizer, name_or_path)
    return tokenizer


def hf_processor(name_or_path, **kwargs):
    """Create a huggingface processor to process multimodal data.

    Args:
        name_or_path (str): The name of the processor.

    Returns:
        Optional[transformers.ProcessorMixin]: The pretrained multimodal processor.
        Returns ``None`` for text-only models (including AutoProcessor fallbacks to
        tokenizer backends such as ``TokenizersBackend``).
    """
    from transformers import AutoConfig, AutoProcessor, PreTrainedTokenizerBase

    try:
        processor = AutoProcessor.from_pretrained(name_or_path, **kwargs)
        # In newer transformers, AutoProcessor may legitimately fall back to a
        # tokenizer backend (e.g. TokenizersBackend) for text-only models.
        # Treat it as "no multimodal processor" and let callers use hf_tokenizer.
        if isinstance(processor, PreTrainedTokenizerBase):
            return None

        config = AutoConfig.from_pretrained(name_or_path, **kwargs)

        # Bind vlm model's get_rope_index method to processor
        processor.config = config
        model_class = None
        match processor.__class__.__name__:
            case "Qwen2VLProcessor":
                from transformers.models.qwen2_vl import Qwen2VLModel

                model_class = Qwen2VLModel
            case "Qwen2_5_VLProcessor":
                from transformers.models.qwen2_5_vl import Qwen2_5_VLModel

                model_class = Qwen2_5_VLModel
            case "Qwen3VLProcessor":
                from transformers.models.qwen3_vl import Qwen3VLModel

                model_class = Qwen3VLModel
            case "Glm4vImageProcessor":
                from transformers.models.glm4v import Glm4vModel

                model_class = Glm4vModel
            case "Qwen3OmniMoeProcessor":
                try:
                    from transformers.models.qwen3_omni_moe.modeling_qwen3_omni_moe import (
                        Qwen3OmniMoePreTrainedModelForConditionalGeneration,
                    )
                except Exception:
                    model_class = None
                else:
                    model_class = Qwen3OmniMoePreTrainedModelForConditionalGeneration
            case "MllamaProcessor":
                pass  # MllamaProcessor and MllamaModel doesn't have get_rope_index property
            case _:
                pass  # Keep the processor even when we do not have a special rope binding for it.

        if model_class is not None:
            processor.get_rope_index = types.MethodType(model_class.get_rope_index, processor)
            if hasattr(model_class, "get_llm_pos_ids_for_vision"):
                processor.get_llm_pos_ids_for_vision = types.MethodType(
                    model_class.get_llm_pos_ids_for_vision, processor
                )
            if hasattr(model_class, "get_vision_position_ids"):
                processor.get_vision_position_ids = types.MethodType(model_class.get_vision_position_ids, processor)
        ensure_multimodal_processor_compatibility(processor, config=config)
        _attach_local_chat_template(processor, name_or_path)
    except Exception as e:
        processor = None
        # TODO(haibin.lin): try-catch should be removed after adding transformer version req to setup.py to avoid
        # silent failure
        warnings.warn(f"Failed to create processor: {e}. This may affect multimodal processing", stacklevel=1)
    # Avoid load tokenizer, see:
    # https://github.com/huggingface/transformers/blob/v4.49.0/src/transformers/models/auto/processing_auto.py#L344
    if processor is not None and "Processor" not in processor.__class__.__name__:
        processor = None
    return processor
