from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(StrEnum):
    GEMINI = "gemini"
    GROQ = "groq"
    OLLAMA = "ollama"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "sqlite:///telltale.db"
    llm_provider: LLMProvider = LLMProvider.GEMINI
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    close_threshold: float = 0.5
    # 10, not 20: at batch size 20 labels are unstable against batch composition.
    # See README "Batch size and classification stability".
    llm_batch_size: int = 10
    llm_description_chars: int = 600
    # Provider tokens-per-minute ceiling used for proactive pacing (0 disables).
    llm_tokens_per_minute: int = 8000
    # Gemini free tier is request-rate limited, not token limited:
    # GenerateRequestsPerMinutePerProjectPerModel-FreeTier = 5.
    gemini_requests_per_minute: int = 5
    groq_requests_per_minute: int = 0  # 0 = unlimited; groq binds on tokens
    # Token ceilings are per-provider too: Groq's 8k/min must not be applied to
    # Gemini, which publishes no comparable per-minute token limit.
    groq_tokens_per_minute: int = 8000
    gemini_tokens_per_minute: int = 0


class Company(BaseModel):
    name: str
    slug: str
    careers_url: str
    adapter_type: str
    endpoint_url: str | None = None
    keka_identifier: str | None = None
    enabled: bool = True


def load_companies(path: Path = Path("companies.yaml")) -> list[Company]:
    data = yaml.safe_load(path.read_text())
    return [Company(**c) for c in data["companies"]]


settings = Settings()
