from types import SimpleNamespace

import pytest

from verl.workers.rollout.utils import get_max_position_embeddings


def test_get_max_position_embeddings_from_top_level_config():
    hf_config = SimpleNamespace(max_position_embeddings=32768)
    assert get_max_position_embeddings(hf_config) == 32768


def test_get_max_position_embeddings_from_nested_omni_text_config():
    hf_config = SimpleNamespace(
        thinker_config=SimpleNamespace(text_config=SimpleNamespace(max_position_embeddings=65536))
    )
    assert get_max_position_embeddings(hf_config) == 65536


def test_get_max_position_embeddings_from_nested_dict_config():
    hf_config = {"llm_config": {"max_seq_len": "8192"}}
    assert get_max_position_embeddings(hf_config) == 8192


def test_get_max_position_embeddings_raises_when_missing():
    with pytest.raises(ValueError, match="max_position_embeddings not found"):
        get_max_position_embeddings(SimpleNamespace())
