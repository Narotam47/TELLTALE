from __future__ import annotations

import re
from typing import ClassVar

import httpx

from telltale.config import LLMProvider, Settings
from telltale.llm.base import (
    BackendUnavailable,
    LLMBackend,
    RateLimitError,
    TransientError,
)

# e.g. "Please try again in 10m17.76s" / "try again in 3.5s"
_RETRY_RE = re.compile(
    r"try again in\s+(?:(\d+)m)?([\d.]+)s", re.IGNORECASE
)


def _looks_like_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    return "429" in text or "rate limit" in text or "resource_exhausted" in text


def _looks_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (500, 502, 503, 504):
        return True
    return (
        "unavailable" in text
        or "overloaded" in text
        or "high demand" in text
        or "internal error" in text
        or "503" in text
    )


def _parse_retry_after(exc: Exception) -> float | None:
    """Pull the provider's suggested wait out of the error, if it gave one."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        header = getattr(resp, "headers", {}) or {}
        raw = header.get("retry-after")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass

    match = _RETRY_RE.search(str(exc))
    if match:
        minutes = float(match.group(1) or 0)
        seconds = float(match.group(2))
        return minutes * 60 + seconds
    return None


class GeminiBackend(LLMBackend):
    provider: ClassVar[str] = "gemini"

    def __init__(self, settings: Settings) -> None:
        self.tokens_used = 0
        self.calls = 0
        # No key-shape validation: keys are opaque strings (legacy "AIza", newer
        # "AQ."), and only the API can say whether one is valid.
        if not settings.gemini_api_key:
            raise BackendUnavailable("GEMINI_API_KEY is not set")
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - import guard
            raise BackendUnavailable(
                "google-genai is not installed; run: uv sync --extra llm"
            ) from exc
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            # Without this the SDK can hang indefinitely on a stalled request.
            http_options={"timeout": 180_000},  # milliseconds
        )
        self._model = settings.gemini_model

    @property
    def model(self) -> str:
        return self._model

    def complete(self, prompt: str) -> str:
        try:
            resp = self._client.models.generate_content(
                model=self._model,
                contents=prompt,
            )
        except Exception as exc:
            if _looks_like_rate_limit(exc):
                raise RateLimitError(str(exc), _parse_retry_after(exc)) from exc
            if _looks_transient(exc):
                raise TransientError(str(exc), _parse_retry_after(exc)) from exc
            raise
        usage = getattr(resp, "usage_metadata", None)
        if usage is not None:
            self.tokens_used += getattr(usage, "total_token_count", 0) or 0
        self.calls += 1
        return resp.text or ""


class GroqBackend(LLMBackend):
    provider: ClassVar[str] = "groq"

    def __init__(self, settings: Settings) -> None:
        self.tokens_used = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        if not settings.groq_api_key:
            raise BackendUnavailable("GROQ_API_KEY is not set")
        try:
            from groq import Groq
        except ImportError as exc:  # pragma: no cover - import guard
            raise BackendUnavailable(
                "groq is not installed; run: uv sync --extra llm"
            ) from exc
        self._client = Groq(api_key=settings.groq_api_key, timeout=180.0, max_retries=2)
        self._model = settings.groq_model

    @property
    def model(self) -> str:
        return self._model

    def complete(self, prompt: str) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
        except Exception as exc:
            if _looks_like_rate_limit(exc):
                raise RateLimitError(str(exc), _parse_retry_after(exc)) from exc
            if _looks_transient(exc):
                raise TransientError(str(exc), _parse_retry_after(exc)) from exc
            raise
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            self.tokens_used += getattr(usage, "total_tokens", 0) or 0
        self.calls += 1
        return resp.choices[0].message.content or ""


class OllamaBackend(LLMBackend):
    provider: ClassVar[str] = "ollama"

    def __init__(self, settings: Settings) -> None:
        self._host = settings.ollama_host.rstrip("/")
        self._model = settings.ollama_model

    @property
    def model(self) -> str:
        return self._model

    def healthy(self) -> bool:
        try:
            resp = httpx.get(f"{self._host}/api/tags", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def complete(self, prompt: str) -> str:
        try:
            resp = httpx.post(
                f"{self._host}/api/generate",
                json={"model": self._model, "prompt": prompt, "stream": False},
                timeout=300,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise RateLimitError(str(exc), _parse_retry_after(exc)) from exc
            raise
        return resp.json().get("response", "")


_BACKENDS: dict[LLMProvider, type[LLMBackend]] = {
    LLMProvider.GEMINI: GeminiBackend,
    LLMProvider.GROQ: GroqBackend,
    LLMProvider.OLLAMA: OllamaBackend,
}


def build_backend(provider: LLMProvider, settings: Settings) -> LLMBackend:
    cls = _BACKENDS.get(provider)
    if cls is None:
        raise BackendUnavailable(f"Unknown LLM provider: {provider}")
    return cls(settings)
