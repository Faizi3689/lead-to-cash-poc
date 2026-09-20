import pytest

from app.llm.base import LLMUnavailable
from app.llm.openai_client import OpenAIClient


def test_missing_openai_key_is_non_retryable_outage():
    with pytest.raises(LLMUnavailable) as exc:
        OpenAIClient(api_key="", model="gpt-4o-mini", timeout_seconds=5)
    assert exc.value.retryable is False


def test_unreachable_openai_maps_to_retryable_outage():
    client = OpenAIClient(api_key="sk-test", model="gpt-4o-mini", timeout_seconds=2,
                          base_url="http://127.0.0.1:9/v1")
    with pytest.raises(LLMUnavailable) as exc:
        client.complete_json([{"role": "user", "content": "hi"}])
    assert exc.value.retryable is True
