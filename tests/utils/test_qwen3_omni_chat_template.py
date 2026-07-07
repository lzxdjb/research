from pathlib import Path

import pytest

from verl.utils.tokenizer import hf_processor, hf_tokenizer


MODEL_DIR = Path(__file__).resolve().parents[2] / "data" / "Qwen3-Omini-30A3B"


@pytest.mark.skipif(not MODEL_DIR.exists(), reason="Qwen3 Omni local checkpoint is unavailable")
def test_qwen3_omni_local_chat_template_is_loaded():
    tokenizer = hf_tokenizer(str(MODEL_DIR), trust_remote_code=False)
    assert tokenizer.chat_template
    rendered = tokenizer.apply_chat_template(
        [{"role": "user", "content": "hello"}],
        add_generation_prompt=True,
        tokenize=False,
    )
    assert rendered.startswith("<|im_start|>user\n")

    processor = hf_processor(str(MODEL_DIR), trust_remote_code=False, use_fast=True)
    assert processor is not None
    assert processor.chat_template
    assert hasattr(processor, "get_rope_index")
    assert hasattr(processor, "get_llm_pos_ids_for_vision")
    rendered = processor.apply_chat_template(
        [{"role": "user", "content": "hello"}],
        add_generation_prompt=True,
        tokenize=False,
    )
    assert rendered.startswith("<|im_start|>user\n")
