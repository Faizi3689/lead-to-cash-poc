"""Provider-neutral LLM interface. The rest of the app never imports a vendor SDK directly."""
from dataclasses import dataclass
from typing import Protocol


class LLMUnavailable(Exception):
    """The LLM could not be reached or refused the call (timeout, network, rate limit, auth...)."""

    def __init__(self, message: str, *, kind: str = "error", retryable: bool = True):
        super().__init__(message)
        self.kind = kind            # 'timeout' | 'error'
        self.retryable = retryable  # False for e.g. bad API key: retrying cannot help


@dataclass
class LLMResponse:
    text: str
    model: str
    latency_ms: int


class LLMClient(Protocol):
    provider: str

    def complete_json(self, messages: list[dict[str, str]]) -> LLMResponse:
        """Send chat messages, return the raw text the model produced (expected to be JSON)."""
        ...
