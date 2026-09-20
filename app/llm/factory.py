from app.config import get_settings
from app.llm.base import LLMClient


def get_llm_client() -> LLMClient:
    """FastAPI dependency; tests override it with scripted fakes."""
    s = get_settings()
    if s.llm_provider == "openai":
        from app.llm.openai_client import OpenAIClient
        return OpenAIClient(s.openai_api_key, s.openai_model, s.llm_timeout_seconds)
    from app.llm.mock_client import MockLLMClient
    return MockLLMClient()
