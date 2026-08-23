from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from telltale.config import LLMProvider, Settings
from telltale.llm.usage import DAILY_LIMITS, remaining_today, spent_today

_PROBE = "Reply with the single word: ok"


def _probe_text(tokens: int) -> str:
    """Roughly `tokens` tokens of filler at ~4 chars/token."""
    return "Reply with the single word: ok. " + ("context padding. " * (tokens // 4))


@dataclass
class ProviderHealth:
    provider: str
    configured: bool = False
    reachable: bool = False
    default_model: str = ""
    default_model_available: bool | None = None
    models: list[str] = field(default_factory=list)
    quota: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    note: str | None = None

    @property
    def status(self) -> str:
        if not self.configured:
            return "not configured"
        if not self.reachable:
            return "UNREACHABLE"
        if self.default_model_available is False:
            return "MODEL MISSING"
        if self.quota.get("exhausted") == "yes":
            return "QUOTA EXHAUSTED"
        return "ok"


def _short(exc: Exception, limit: int = 160) -> str:
    return str(exc).replace("\n", " ")[:limit]


def check_groq(settings: Settings, deep: bool = False) -> ProviderHealth:
    h = ProviderHealth(provider="groq", default_model=settings.groq_model)
    if not settings.groq_api_key:
        h.error = "GROQ_API_KEY not set"
        return h
    h.configured = True

    try:
        from groq import Groq
    except ImportError:
        h.error = "groq package not installed (uv sync --extra llm)"
        return h

    client = Groq(api_key=settings.groq_api_key)
    try:
        h.models = sorted(m.id for m in client.models.list().data)
        h.reachable = True
    except Exception as exc:
        h.error = _short(exc)
        return h

    h.default_model_available = h.default_model in h.models

    # Groq's headers expose the per-MINUTE window only. A daily (TPD) budget that
    # is spent stays invisible until a full-size request is rejected, which is how
    # a run can die minutes after doctor said "ok". --deep sends a probe the size
    # of one real classify batch so the verdict actually predicts a run.
    probe_tokens = (
        settings.llm_batch_size * (settings.llm_description_chars // 4) + 1800
        if deep
        else 250
    )
    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": h.default_model,
                "messages": [{"role": "user", "content": _probe_text(probe_tokens)}],
                "max_tokens": 5,
            },
            timeout=60,
        )
    except Exception as exc:
        h.quota["probe"] = f"failed: {_short(exc, 80)}"
        return h

    for key, label in (
        ("x-ratelimit-remaining-tokens", "tokens_left_this_min"),
        ("x-ratelimit-limit-tokens", "tokens_per_min"),
        ("x-ratelimit-remaining-requests", "requests_left"),
        ("x-ratelimit-reset-tokens", "token_reset"),
    ):
        if key in resp.headers:
            h.quota[label] = resp.headers[key]

    h.quota["probe_size"] = f"~{probe_tokens} tokens ({'batch-sized' if deep else 'light'})"
    if "groq" in DAILY_LIMITS:
        used = spent_today("groq")
        left = remaining_today("groq")
        h.quota["daily_limit"] = f"{DAILY_LIMITS['groq']:,}"
        h.quota["spent_today_local"] = f"{used:,}"
        h.quota["remaining_est"] = f"~{left:,}" if left is not None else "unknown"
    if not deep:
        h.note = (
            "per-minute headers only; daily (TPD) budget is not observable "
            "without --deep"
        )

    if resp.status_code == 429:
        h.quota["exhausted"] = "yes"
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            msg = resp.text[:200]
        h.quota["limit_hit"] = "per-day (TPD)" if "per day" in msg.lower() else "per-minute"
        h.error = msg[:200]
    elif resp.status_code >= 400:
        h.error = f"HTTP {resp.status_code}: {resp.text[:150]}"

    return h


def check_gemini(settings: Settings) -> ProviderHealth:
    h = ProviderHealth(provider="gemini", default_model=settings.gemini_model)
    if not settings.gemini_api_key:
        h.error = "GEMINI_API_KEY not set"
        return h
    h.configured = True

    try:
        from google import genai
    except ImportError:
        h.error = "google-genai not installed (uv sync --extra llm)"
        return h

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        names = []
        for m in client.models.list():
            name = getattr(m, "name", "") or ""
            names.append(name.removeprefix("models/"))
        h.models = sorted(n for n in names if n)
        h.reachable = True
    except Exception as exc:
        h.error = _short(exc)
        return h

    h.default_model_available = any(
        h.default_model == m or m.startswith(h.default_model) for m in h.models
    )
    return h


def check_ollama(settings: Settings) -> ProviderHealth:
    h = ProviderHealth(
        provider="ollama",
        configured=True,  # no key needed; it is configured by being installed
        default_model=settings.ollama_model,
    )
    host = settings.ollama_host.rstrip("/")
    try:
        resp = httpx.get(f"{host}/api/tags", timeout=5)
        resp.raise_for_status()
    except Exception as exc:
        h.configured = False
        h.error = f"no server at {host} ({_short(exc, 70)})"
        h.note = "install from https://ollama.com, then: ollama pull " + h.default_model
        return h

    h.reachable = True
    h.models = sorted(m.get("name", "") for m in resp.json().get("models", []))
    h.default_model_available = any(
        m == h.default_model or m.startswith(h.default_model.split(":")[0])
        for m in h.models
    )
    if not h.default_model_available:
        h.note = f"run: ollama pull {h.default_model}"
    h.quota["limit"] = "none (local)"
    return h


def run_all(
    settings: Settings | None = None, deep: bool = False
) -> list[ProviderHealth]:
    s = settings or Settings()
    return [check_groq(s, deep=deep), check_gemini(s), check_ollama(s)]


def active_provider(settings: Settings | None = None) -> LLMProvider:
    return (settings or Settings()).llm_provider
