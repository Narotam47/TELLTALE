from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

# 12 categories. "Payments Infrastructure" was merged into platform engineering
# after it stayed in low single digits across two prompts and two model families;
# see docs/taxonomy.md.
FUNCTION_CATEGORIES: frozenset[str] = frozenset({
    "Credit & Risk",
    "Compliance & Regulatory",
    "Data & ML",
    "Engineering - Platform & Payments",
    "Engineering - Mobile",
    "Product",
    "Design",
    "Growth & Marketing",
    "Operations",
    "Finance & Accounting",
    "People & HR",
    "Other",
})

SENIORITY_LEVELS: frozenset[str] = frozenset({
    "Intern",
    "Junior",
    "Mid",
    "Senior",
    "Staff/Principal",
    "Leadership",
})


@dataclass
class Classification:
    posting_id: int
    function_category: str
    seniority_level: str
    rationale: str
    model_name: str


class RateLimitError(Exception):
    """Raised by a backend when the provider reports a rate limit (HTTP 429).

    ``retry_after`` carries the provider's own advice in seconds when it gives any,
    so the client can wait exactly as long as told instead of guessing.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(Exception):
    """A retryable server-side hiccup (503 UNAVAILABLE, 5xx, model overloaded).

    Distinct from RateLimitError: nothing about our usage caused it and it usually
    clears in seconds, so it is retried on the same provider before any failover.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BackendUnavailable(Exception):
    """Raised when a backend cannot be constructed (missing dep or API key)."""


class LLMBackend(ABC):
    provider: ClassVar[str]

    @property
    @abstractmethod
    def model(self) -> str:
        """The concrete model id serving requests."""

    @property
    def model_name(self) -> str:
        """Stored on every classification as the provenance string."""
        return f"{self.provider}:{self.model}"

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """Send a single prompt, return raw text. Raise RateLimitError on 429."""

    def healthy(self) -> bool:
        """Whether this backend can currently serve requests.

        Checked before failing over: switching to an unreachable backend converts a
        recoverable rate limit into total loss of the run.
        """
        return True
