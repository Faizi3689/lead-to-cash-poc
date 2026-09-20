"""Centralised configuration. All values come from environment / .env — no secrets in code."""
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_key: str = Field(default="change-me", repr=False)

    database_url: str = Field(default="sqlite:///./local.db", repr=False)

    llm_provider: Literal["mock", "openai"] = "mock"
    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 20
    llm_max_retries: int = 2
    llm_min_confidence: float = 0.75
    llm_retry_backoff_seconds: float = 1.0

    n8n_base_url: str = "http://localhost:5678"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3_driver(cls, v: str) -> str:
        # Supabase gives "postgresql://" or "postgres://"; we need the psycopg (v3) driver.
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
