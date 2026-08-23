from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

from telltale.config import Settings
from telltale.llm.base import LLMBackend, RateLimitError
from telltale.llm.client import (
    ClassificationClient,
    load_prompt,
    parse_response,
    strip_fences,
)

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


@dataclass
class FakePosting:
    id: int
    title: str
    raw_department: str | None = None
    description_text: str | None = None


class ScriptedBackend(LLMBackend):
    """Returns queued responses in order; a queued Exception is raised instead."""

    provider: ClassVar[str] = "scripted"

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "scripted-1"

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("ScriptedBackend ran out of responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _valid_json(ids: list[int]) -> str:
    return json.dumps([
        {
            "id": i,
            "function_category": "Engineering - Platform & Payments",
            "seniority_level": "Senior",
            "rationale": "Backend platform work.",
        }
        for i in ids
    ])


def _postings(n: int, start: int = 1) -> list[FakePosting]:
    return [
        FakePosting(
            id=i,
            title=f"Engineer {i}",
            raw_department="Tech",
            description_text="x" * 5000,
        )
        for i in range(start, start + n)
    ]


def _client(backend, fallback=None, **kw) -> ClassificationClient:
    # Default to no rate-limit retries so tests never sleep; the retry path has
    # its own tests that stub time.sleep explicitly.
    kw.setdefault("rate_limit_retries", 1)
    kw.setdefault("rate_limit_backoff", 0)
    # Pacing off by default so the suite never sleeps for real; the pacing tests
    # opt in explicitly and stub time.sleep.
    kw.setdefault("tokens_per_minute", 0)
    kw.setdefault("requests_per_minute", 0)
    return ClassificationClient(
        settings=Settings(),
        backend=backend,
        fallback_backend=fallback,
        prompts_dir=PROMPTS,
        **kw,
    )


class UnhealthyBackend(ScriptedBackend):
    """Stands in for an Ollama that is not running."""

    provider: ClassVar[str] = "unhealthy"

    def healthy(self) -> bool:
        return False


class TestParsing:
    def test_strip_markdown_fences(self):
        raw = '```json\n[{"id": 1}]\n```'
        assert strip_fences(raw) == '[{"id": 1}]'

    def test_strip_surrounding_prose(self):
        raw = 'Here you go:\n[{"id": 1}]\nHope that helps!'
        assert json.loads(strip_fences(raw)) == [{"id": 1}]

    def test_parse_plain_array(self):
        assert parse_response('[{"id": 2}]') == [{"id": 2}]

    def test_parse_unwraps_object(self):
        raw = '{"classifications": [{"id": 3}]}'
        assert parse_response(raw) == [{"id": 3}]

    def test_parse_raises_on_garbage(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            parse_response("not json at all")


class TestPrompt:
    def test_prompt_file_loads(self):
        text = load_prompt("v1", PROMPTS)
        assert "{{POSTINGS}}" in text
        assert "Payments Infrastructure" in text
        assert "Staff/Principal" in text

    def test_missing_prompt_raises(self):
        with pytest.raises(FileNotFoundError):
            load_prompt("v99", PROMPTS)

    def test_prompt_is_not_inlined_in_code(self):
        source = (
            Path(__file__).resolve().parent.parent / "telltale" / "llm" / "client.py"
        ).read_text()
        assert "Payments Infrastructure" not in source


class TestBatching:
    def test_splits_into_batches_of_20(self):
        backend = ScriptedBackend([
            _valid_json(list(range(1, 21))),
            _valid_json(list(range(21, 26))),
        ])
        client = _client(backend, batch_size=20)
        results = client.classify_batch(_postings(25), "v1")

        assert len(backend.prompts) == 2
        assert len(results) == 25

    def test_default_batch_size_is_ten(self):
        """Pinned at 10: at 20, labels shift with batch composition (see README)."""
        backend = ScriptedBackend([_valid_json(list(range(1, 11)))])
        client = _client(backend)
        assert client.batch_size == 10

        client.classify_batch(_postings(10), "v1")
        assert len(backend.prompts) == 1

    def test_payload_truncates_description(self):
        backend = ScriptedBackend([_valid_json([1])])
        client = _client(backend, description_chars=600)
        client.classify_batch(_postings(1), "v1")

        prompt = backend.prompts[0]
        start = prompt.index("[", prompt.index("POSTINGS TO CLASSIFY"))
        payload = json.loads(prompt[start : prompt.rindex("]") + 1])
        assert len(payload[0]["description"]) == 600

    def test_payload_sends_only_expected_fields(self):
        backend = ScriptedBackend([_valid_json([1])])
        client = _client(backend)
        client.classify_batch(_postings(1), "v1")

        prompt = backend.prompts[0]
        start = prompt.index("[", prompt.index("POSTINGS TO CLASSIFY"))
        payload = json.loads(prompt[start : prompt.rindex("]") + 1])
        assert set(payload[0].keys()) == {"id", "title", "raw_department", "description"}


class TestRepairRetry:
    def test_retries_once_then_succeeds(self):
        backend = ScriptedBackend(["I cannot do that", _valid_json([1, 2])])
        client = _client(backend)
        results = client.classify_batch(_postings(2), "v1")

        assert len(results) == 2
        assert len(backend.prompts) == 2
        assert "was not valid JSON" in backend.prompts[1]
        assert "I cannot do that" in backend.prompts[1]

    def test_skips_batch_after_second_failure(self):
        backend = ScriptedBackend(["garbage", "still garbage"])
        client = _client(backend)
        results = client.classify_batch(_postings(2), "v1")

        assert results == []
        assert len(backend.prompts) == 2

    def test_bad_batch_does_not_abort_later_batches(self):
        backend = ScriptedBackend([
            "garbage",
            "still garbage",
            _valid_json(list(range(21, 24))),
        ])
        client = _client(backend, batch_size=20)
        results = client.classify_batch(_postings(23), "v1")

        assert len(results) == 3


class TestValidation:
    def test_rejects_invented_category(self):
        payload = json.dumps([
            {
                "id": 1,
                "function_category": "Blockchain Wizardry",
                "seniority_level": "Senior",
                "rationale": "made up",
            },
            {
                "id": 2,
                "function_category": "Product",
                "seniority_level": "Mid",
                "rationale": "fine",
            },
        ])
        backend = ScriptedBackend([payload])
        results = _client(backend).classify_batch(_postings(2), "v1")

        assert len(results) == 1
        assert results[0].posting_id == 2

    def test_rejects_invalid_seniority(self):
        payload = json.dumps([{
            "id": 1,
            "function_category": "Product",
            "seniority_level": "Godlike",
            "rationale": "nope",
        }])
        backend = ScriptedBackend([payload])
        assert _client(backend).classify_batch(_postings(1), "v1") == []

    def test_drops_ids_not_in_batch(self):
        payload = json.dumps([{
            "id": 999,
            "function_category": "Product",
            "seniority_level": "Mid",
            "rationale": "hallucinated id",
        }])
        backend = ScriptedBackend([payload])
        assert _client(backend).classify_batch(_postings(1), "v1") == []

    def test_model_name_recorded(self):
        backend = ScriptedBackend([_valid_json([1])])
        results = _client(backend).classify_batch(_postings(1), "v1")
        assert results[0].model_name == "scripted:scripted-1"


class TestFallback:
    def test_falls_back_on_rate_limit(self):
        primary = ScriptedBackend([RateLimitError("429 quota exceeded")])
        fallback = ScriptedBackend([_valid_json([1])])
        client = _client(primary, fallback=fallback)

        results = client.classify_batch(_postings(1), "v1")

        assert len(results) == 1
        assert client.backend is fallback
        assert results[0].model_name == "scripted:scripted-1"

    def test_falls_back_after_two_consecutive_failures(self):
        primary = ScriptedBackend([
            RuntimeError("boom 1"),
            RuntimeError("boom 2"),
        ])
        fallback = ScriptedBackend([
            _valid_json(list(range(21, 41))),
            _valid_json([41, 42, 43]),
        ])
        client = _client(primary, fallback=fallback, batch_size=20)

        results = client.classify_batch(_postings(43), "v1")

        assert client.backend is fallback
        assert len(primary.prompts) == 2
        assert len(results) == 23

    def test_single_failure_does_not_fall_back(self):
        primary = ScriptedBackend([
            RuntimeError("transient"),
            _valid_json(list(range(21, 23))),
        ])
        fallback = ScriptedBackend([])
        client = _client(primary, fallback=fallback, batch_size=20)

        results = client.classify_batch(_postings(22), "v1")

        assert client.backend is primary
        assert len(results) == 2

    def test_rate_limit_switch_is_sticky(self):
        primary = ScriptedBackend([RateLimitError("429")])
        fallback = ScriptedBackend([
            _valid_json(list(range(1, 21))),
            _valid_json(list(range(21, 26))),
        ])
        client = _client(primary, fallback=fallback, batch_size=20)

        results = client.classify_batch(_postings(25), "v1")

        assert len(results) == 25
        assert len(primary.prompts) == 1
        assert len(fallback.prompts) == 2


class TestRateLimitRetry:
    """A rolling-window rate limit clears on its own; wait rather than fail over."""

    def test_retries_same_provider_before_falling_back(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("telltale.llm.client.time.sleep", slept.append)

        primary = ScriptedBackend([
            RateLimitError("429 slow down", retry_after=7.0),
            _valid_json([1]),
        ])
        fallback = ScriptedBackend([])
        client = _client(
            primary, fallback=fallback, rate_limit_retries=3, rate_limit_backoff=20
        )

        results = client.classify_batch(_postings(1), "v1")

        assert len(results) == 1
        assert client.backend is primary, "should not fail over when a retry succeeds"
        assert fallback.prompts == []
        assert slept == [7.0], "should honour the provider's retry_after"

    def test_backoff_grows_when_no_retry_after_given(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("telltale.llm.client.time.sleep", slept.append)

        primary = ScriptedBackend([
            RateLimitError("429"),
            RateLimitError("429"),
            _valid_json([1]),
        ])
        client = _client(primary, rate_limit_retries=4, rate_limit_backoff=10)

        client.classify_batch(_postings(1), "v1")
        assert slept == [10, 20]

    def test_retry_wait_is_capped(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("telltale.llm.client.time.sleep", slept.append)

        primary = ScriptedBackend([
            RateLimitError("try again in 10m17.76s", retry_after=617.76),
            _valid_json([1]),
        ])
        client = _client(primary, rate_limit_retries=3, rate_limit_max_wait=90)

        client.classify_batch(_postings(1), "v1")
        assert slept == [90]

    def test_falls_back_only_after_retries_exhausted(self, monkeypatch):
        monkeypatch.setattr("telltale.llm.client.time.sleep", lambda _: None)

        primary = ScriptedBackend([RateLimitError("429")] * 3)
        fallback = ScriptedBackend([_valid_json([1])])
        client = _client(primary, fallback=fallback, rate_limit_retries=3)

        results = client.classify_batch(_postings(1), "v1")

        assert len(primary.prompts) == 3
        assert client.backend is fallback
        assert len(results) == 1

    def test_does_not_fail_over_to_unreachable_fallback(self, monkeypatch):
        """Regression: a dead Ollama once swallowed 227 postings after a 429."""
        monkeypatch.setattr("telltale.llm.client.time.sleep", lambda _: None)

        primary = ScriptedBackend([RateLimitError("429")] * 2)
        dead = UnhealthyBackend([_valid_json([1])])
        client = _client(primary, fallback=dead, rate_limit_retries=2)

        results = client.classify_batch(_postings(1), "v1")

        assert results == []
        assert client.backend is primary, "must not adopt an unreachable backend"
        assert dead.prompts == [], "must not send work to a dead backend"


class TestVersionThree:
    """v3 = v2 plus the Payments Infrastructure merge (see docs/taxonomy.md)."""

    def test_v3_drops_the_merged_category(self):
        text = load_prompt("v3", PROMPTS)
        assert "Payments Infrastructure" not in text
        assert "Engineering - Platform & Payments" in text

    def test_v3_lists_every_current_category(self):
        from telltale.llm.base import FUNCTION_CATEGORIES

        text = load_prompt("v3", PROMPTS)
        missing = [c for c in FUNCTION_CATEGORIES if c not in text]
        assert missing == []

    def test_v3_file_carries_a_maintainer_comment(self):
        raw = (PROMPTS / "classify_v3.txt").read_text()
        assert raw.lstrip().startswith("<!--")
        assert "taxonomy" in raw.split("-->")[0].lower()

    def test_comment_never_reaches_the_model(self):
        assert "<!--" not in load_prompt("v3", PROMPTS)

    def test_v3_profile_is_stripped_400_batch_10(self):
        from telltale.llm.client import VERSION_PROFILES

        assert VERSION_PROFILES["v3"] == {
            "strip_boilerplate": True,
            "description_chars": 400,
            "batch_size": 10,
        }

    def test_v2_profile_keeps_raw_window(self):
        from telltale.llm.client import VERSION_PROFILES

        assert VERSION_PROFILES["v2"]["strip_boilerplate"] is False
        assert VERSION_PROFILES["v2"]["description_chars"] == 600


class TestRequestPacing:
    """Gemini's free tier binds on requests/minute, not tokens."""

    def test_waits_once_rpm_ceiling_is_reached(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("telltale.llm.client.time.sleep", slept.append)

        backend = ScriptedBackend([_valid_json([i]) for i in range(1, 8)])
        client = _client(backend, batch_size=1, requests_per_minute=5,
                         tokens_per_minute=0)
        client.classify_batch(_postings(7), "v1")

        assert len(backend.prompts) == 7
        assert slept, "should pace once the per-minute request ceiling is hit"

    def test_no_pacing_below_the_ceiling(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("telltale.llm.client.time.sleep", slept.append)

        backend = ScriptedBackend([_valid_json([1, 2])])
        client = _client(backend, batch_size=2, requests_per_minute=5,
                         tokens_per_minute=0)
        client.classify_batch(_postings(2), "v1")
        assert slept == []

    def test_rpm_zero_disables_request_pacing(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("telltale.llm.client.time.sleep", slept.append)

        backend = ScriptedBackend([_valid_json([i]) for i in range(1, 7)])
        client = _client(backend, batch_size=1, requests_per_minute=0,
                         tokens_per_minute=0)
        client.classify_batch(_postings(6), "v1")
        assert slept == []


class TestTransientRetry:
    def test_retries_503_then_succeeds(self, monkeypatch):
        from telltale.llm.base import TransientError

        monkeypatch.setattr("telltale.llm.client.time.sleep", lambda _: None)
        backend = ScriptedBackend([
            TransientError("503 UNAVAILABLE high demand"),
            _valid_json([1]),
        ])
        client = _client(backend, rate_limit_retries=3)
        assert len(client.classify_batch(_postings(1), "v1")) == 1

    def test_transient_does_not_immediately_fail_over(self, monkeypatch):
        from telltale.llm.base import TransientError

        monkeypatch.setattr("telltale.llm.client.time.sleep", lambda _: None)
        backend = ScriptedBackend([TransientError("503"), _valid_json([1])])
        fallback = ScriptedBackend([])
        client = _client(backend, fallback=fallback, rate_limit_retries=3)
        client.classify_batch(_postings(1), "v1")
        assert client.backend is backend
        assert fallback.prompts == []


class TestTaxonomy:
    def test_twelve_categories(self):
        from telltale.llm.base import FUNCTION_CATEGORIES

        assert len(FUNCTION_CATEGORIES) == 12

    def test_merged_category_replaces_both_originals(self):
        from telltale.llm.base import FUNCTION_CATEGORIES

        assert "Engineering - Platform & Payments" in FUNCTION_CATEGORIES
        assert "Payments Infrastructure" not in FUNCTION_CATEGORIES
        assert "Engineering - Platform" not in FUNCTION_CATEGORIES

    def test_mobile_stays_separate(self):
        from telltale.llm.base import FUNCTION_CATEGORIES

        assert "Engineering - Mobile" in FUNCTION_CATEGORIES
