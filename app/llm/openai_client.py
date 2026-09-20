"""OpenAI implementation. SDK retries are disabled: our service controls retries and records each one."""
import time

from app.llm.base import LLMResponse, LLMUnavailable


class OpenAIClient:
    provider = "openai"

    def __init__(self, api_key: str, model: str, timeout_seconds: int, base_url: str | None = None):
        if not api_key:
            raise LLMUnavailable("OPENAI_API_KEY is not configured", kind="error", retryable=False)
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0, base_url=base_url)
        self.model = model

    def complete_json(self, messages: list[dict[str, str]]) -> LLMResponse:
        import openai

        started = time.monotonic()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
        except openai.APITimeoutError as exc:
            raise LLMUnavailable(f"OpenAI timeout: {exc}", kind="timeout") from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.BadRequestError) as exc:
            raise LLMUnavailable(f"OpenAI rejected the request: {exc}", retryable=False) from exc
        except (openai.APIConnectionError, openai.RateLimitError, openai.APIStatusError) as exc:
            raise LLMUnavailable(f"OpenAI unavailable: {exc}") from exc

        latency = int((time.monotonic() - started) * 1000)
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        return LLMResponse(text=text, model=resp.model or self.model, latency_ms=latency)
