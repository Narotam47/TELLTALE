from __future__ import annotations

import httpx

from telltale.config import Settings
from telltale.llm import doctor


class _Resp:
    def __init__(self, status_code=200, headers=None, payload=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def _settings(**kw) -> Settings:
    base = {
        "groq_api_key": "",
        "gemini_api_key": "",
        "ollama_host": "http://localhost:11434",
    }
    base.update(kw)
    return Settings(**base)


class TestOllamaCheck:
    def test_reports_unreachable_server(self, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(doctor.httpx, "get", boom)
        h = doctor.check_ollama(_settings())

        assert h.status == "UNREACHABLE" or h.status == "not configured"
        assert not h.reachable
        assert "no server" in h.error
        assert "ollama pull" in h.note

    def test_reports_models_when_serving(self, monkeypatch):
        payload = {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2:7b"}]}
        monkeypatch.setattr(doctor.httpx, "get", lambda *a, **k: _Resp(payload=payload))
        h = doctor.check_ollama(_settings(ollama_model="llama3.1:8b"))

        assert h.reachable
        assert h.status == "ok"
        assert h.default_model_available is True
        assert "llama3.1:8b" in h.models

    def test_flags_missing_model(self, monkeypatch):
        payload = {"models": [{"name": "mistral:7b"}]}
        monkeypatch.setattr(doctor.httpx, "get", lambda *a, **k: _Resp(payload=payload))
        h = doctor.check_ollama(_settings(ollama_model="llama3.1:8b"))

        assert h.reachable
        assert h.default_model_available is False
        assert h.status == "MODEL MISSING"
        assert "ollama pull llama3.1:8b" in h.note


class TestGroqCheck:
    def test_unconfigured_without_key(self):
        h = doctor.check_groq(_settings())
        assert h.status == "not configured"
        assert "GROQ_API_KEY" in h.error

    def test_flags_model_the_account_cannot_use(self, monkeypatch):
        """Regression: a dead default model silently failed over to a dead Ollama."""
        class _Models:
            def list(self):
                class R:
                    data = [type("M", (), {"id": "openai/gpt-oss-120b"})()]
                return R()

        class _Client:
            models = _Models()

        monkeypatch.setattr("groq.Groq", lambda api_key: _Client())
        monkeypatch.setattr(
            doctor.httpx, "post", lambda *a, **k: _Resp(headers={}, payload={})
        )
        h = doctor.check_groq(_settings(groq_api_key="k", groq_model="llama-3.3-70b-versatile"))

        assert h.reachable
        assert h.default_model_available is False
        assert h.status == "MODEL MISSING"

    def test_detects_daily_quota_exhaustion(self, monkeypatch):
        """Regression: a 1-token probe reports healthy while the day's budget is gone."""
        class _Models:
            def list(self):
                class R:
                    data = [type("M", (), {"id": "openai/gpt-oss-120b"})()]
                return R()

        class _Client:
            models = _Models()

        monkeypatch.setattr("groq.Groq", lambda api_key: _Client())
        monkeypatch.setattr(
            doctor.httpx,
            "post",
            lambda *a, **k: _Resp(
                status_code=429,
                payload={"error": {"message": "Rate limit reached ... on tokens per day (TPD): Limit 200000"}},
            ),
        )
        h = doctor.check_groq(_settings(groq_api_key="k", groq_model="openai/gpt-oss-120b"))

        assert h.status == "QUOTA EXHAUSTED"
        assert h.quota["limit_hit"] == "per-day (TPD)"


class TestGeminiCheck:
    def test_unconfigured_without_key(self):
        h = doctor.check_gemini(_settings())
        assert h.status == "not configured"
        assert "GEMINI_API_KEY" in h.error


def test_run_all_covers_every_provider():
    names = [h.provider for h in doctor.run_all(_settings())]
    assert names == ["groq", "gemini", "ollama"]
