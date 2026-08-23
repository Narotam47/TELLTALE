from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import structlog

from telltale.config import LLMProvider, Settings
from telltale.llm.backends import build_backend
from telltale.llm.base import (
    FUNCTION_CATEGORIES,
    SENIORITY_LEVELS,
    BackendUnavailable,
    Classification,
    LLMBackend,
    RateLimitError,
    TransientError,
)
from telltale.llm.preprocess import build_window

log = structlog.get_logger()

PROMPTS_DIR = Path("prompts")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

REPAIR_TEMPLATE = """Your previous response was not valid JSON and could not be parsed.

The error was: {error}

Here is what you returned:
---
{bad_output}
---

Return the SAME classifications again, but as a single valid JSON array and nothing
else. No markdown fences, no prose. Each element must have exactly the keys:
"id", "function_category", "seniority_level", "rationale".
"""


_COMMENT_RE = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)

# Window/batch settings each prompt version was run with. Keeping these in code
# rather than in the caller is what makes a version reproducible after the fact.
VERSION_PROFILES: dict[str, dict[str, object]] = {
    "v1": {"strip_boilerplate": False, "description_chars": 600, "batch_size": 20},
    "v2": {"strip_boilerplate": False, "description_chars": 600, "batch_size": 10},
    "v3": {"strip_boilerplate": True, "description_chars": 400, "batch_size": 10},
    # Gemini free tier allows 20 generate_content requests per day per model, so a
    # 367-posting pass must fit in <=20 requests: batch 20 -> 19 requests. Both
    # gemini variants use the same batch size, so each batch holds the identical
    # postings in both passes and composition noise cancels out of the comparison.
    "v2-gemini": {"strip_boilerplate": False, "description_chars": 600, "batch_size": 20},
    "v3-gemini": {"strip_boilerplate": True, "description_chars": 400, "batch_size": 20},
}


def base_version(prompt_version: str) -> str:
    """Strip a provider suffix: "v3-gemini" -> "v3".

    Lets the same prompt and window profile be run on a second provider and stored
    under its own label, without duplicating prompt files or profiles.
    """
    return prompt_version.split("-", 1)[0]


def load_prompt(prompt_version: str, prompts_dir: Path = PROMPTS_DIR) -> str:
    """Load a prompt template, stripping any leading <!-- --> maintainer comment.

    The comment is documentation for humans and must not reach the model, so that
    two versions can be byte-identical in what they actually send.
    """
    path = prompts_dir / f"classify_{base_version(prompt_version)}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return _COMMENT_RE.sub("", path.read_text())


def _chunk(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def strip_fences(text: str) -> str:
    """Remove markdown code fences and surrounding prose around a JSON array."""
    cleaned = _FENCE_RE.sub("", text.strip())
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def parse_response(text: str) -> list[dict[str, Any]]:
    """Parse an LLM response into a list of raw classification dicts."""
    payload = json.loads(strip_fences(text))
    if isinstance(payload, dict):
        for key in ("classifications", "results", "postings", "data"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise ValueError(f"Expected a JSON array, got {type(payload).__name__}")
    return payload


class ClassificationClient:
    """Provider-agnostic classifier with automatic fallback to local Ollama."""

    def __init__(
        self,
        settings: Settings | None = None,
        backend: LLMBackend | None = None,
        fallback_backend: LLMBackend | None = None,
        prompts_dir: Path = PROMPTS_DIR,
        batch_size: int | None = None,
        description_chars: int | None = None,
        strip_boilerplate: bool = True,
        tokens_per_minute: int | None = None,
        requests_per_minute: int | None = None,
        rate_limit_retries: int = 4,
        rate_limit_backoff: float = 20.0,
        rate_limit_max_wait: float = 90.0,
    ) -> None:
        self.settings = settings or Settings()
        self.prompts_dir = prompts_dir
        self.batch_size = batch_size or self.settings.llm_batch_size
        self.description_chars = description_chars or self.settings.llm_description_chars
        self.strip_boilerplate = strip_boilerplate
        _prov = getattr(backend, "provider", None) or str(self.settings.llm_provider)
        if tokens_per_minute is not None:
            self.tokens_per_minute = tokens_per_minute
        else:
            self.tokens_per_minute = getattr(
                self.settings,
                f"{_prov}_tokens_per_minute",
                getattr(self.settings, "llm_tokens_per_minute", 0),
            )
        if requests_per_minute is not None:
            self.requests_per_minute = requests_per_minute
        else:
            # `backend` (the parameter) is used here, not self.backend, which is
            # not assigned until further down this constructor.
            prov = getattr(backend, "provider", None) or str(self.settings.llm_provider)
            self.requests_per_minute = getattr(
                self.settings, f"{prov}_requests_per_minute", 0
            )
        self._call_log: list[tuple[float, int]] = []
        self.rate_limit_retries = rate_limit_retries
        self.rate_limit_backoff = rate_limit_backoff
        self.rate_limit_max_wait = rate_limit_max_wait
        self._explicit_fallback = fallback_backend
        self._consecutive_failures = 0
        self._fell_back = False

        if backend is not None:
            self.backend = backend
        else:
            provider = self.settings.llm_provider
            try:
                self.backend = build_backend(provider, self.settings)
            except BackendUnavailable as exc:
                log.warning(
                    "llm.primary_unavailable",
                    provider=str(provider),
                    reason=str(exc),
                )
                self.backend = self._make_fallback()
                self._fell_back = True

    def _make_fallback(self) -> LLMBackend:
        if self._explicit_fallback is not None:
            return self._explicit_fallback
        return build_backend(LLMProvider.OLLAMA, self.settings)

    def _switch_to_fallback(self, reason: str) -> bool:
        """Fail over to the fallback backend. Returns False if it is unusable.

        Switching to an unreachable backend turns a recoverable rate limit into a
        total loss of the run, so the fallback is health-checked before adoption.
        """
        if self._fell_back:
            return True
        try:
            fallback = self._make_fallback()
        except Exception as exc:
            log.error("llm.fallback_unavailable", reason=str(exc)[:200])
            return False

        if not fallback.healthy():
            log.error(
                "llm.fallback_unhealthy",
                to_provider=fallback.provider,
                detail="fallback did not respond; staying on primary",
            )
            return False

        log.warning(
            "llm.fallback_engaged",
            from_provider=self.backend.provider,
            to_provider=fallback.provider,
            reason=reason,
        )
        self.backend = fallback
        self._fell_back = True
        self._consecutive_failures = 0
        return True

    def _build_payload(self, postings: Sequence[Any]) -> list[dict[str, Any]]:
        payload = []
        for p in postings:
            window = build_window(
                p.description_text,
                self.description_chars,
                strip=self.strip_boilerplate,
            )
            payload.append({
                "id": p.id,
                "title": p.title,
                "raw_department": p.raw_department,
                "description": window.text,
            })
        return payload

    def _validate(
        self,
        raw_items: list[dict[str, Any]],
        valid_ids: set[int],
        model_name: str,
    ) -> list[Classification]:
        results: list[Classification] = []
        seen: set[int] = set()

        for item in raw_items:
            if not isinstance(item, dict):
                log.warning("llm.item_not_object", item=repr(item)[:120])
                continue

            try:
                posting_id = int(item.get("id"))
            except (TypeError, ValueError):
                log.warning("llm.item_bad_id", item=repr(item)[:120])
                continue

            if posting_id not in valid_ids:
                log.warning("llm.item_unknown_id", posting_id=posting_id)
                continue
            if posting_id in seen:
                log.warning("llm.item_duplicate_id", posting_id=posting_id)
                continue

            category = (item.get("function_category") or "").strip()
            seniority = (item.get("seniority_level") or "").strip()

            if category not in FUNCTION_CATEGORIES:
                log.warning(
                    "llm.invalid_category",
                    posting_id=posting_id,
                    value=category[:60],
                )
                continue
            if seniority not in SENIORITY_LEVELS:
                log.warning(
                    "llm.invalid_seniority",
                    posting_id=posting_id,
                    value=seniority[:60],
                )
                continue

            seen.add(posting_id)
            results.append(
                Classification(
                    posting_id=posting_id,
                    function_category=category,
                    seniority_level=seniority,
                    rationale=(item.get("rationale") or "").strip(),
                    model_name=model_name[:64],
                )
            )

        return results

    def _run_batch(self, postings: Sequence[Any], template: str) -> list[Classification]:
        payload = self._build_payload(postings)
        prompt = template.replace("{{POSTINGS}}", json.dumps(payload, indent=2))
        valid_ids = {p.id for p in postings}

        self._pace(prompt)
        before = getattr(self.backend, "tokens_used", 0)
        raw_text = self.backend.complete(prompt)
        self._record_usage(before, prompt)
        model_name = self.backend.model_name

        try:
            items = parse_response(raw_text)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "llm.parse_failed_retrying",
                provider=self.backend.provider,
                error=str(exc)[:200],
            )
            repair = REPAIR_TEMPLATE.format(error=str(exc), bad_output=raw_text[:4000])
            self._pace(repair)
            raw_text = self.backend.complete(repair)
            model_name = self.backend.model_name
            try:
                items = parse_response(raw_text)
            except (json.JSONDecodeError, ValueError) as exc2:
                log.error(
                    "llm.parse_failed_skipping_batch",
                    provider=self.backend.provider,
                    error=str(exc2)[:200],
                    batch_size=len(postings),
                )
                return []

        results = self._validate(items, valid_ids, model_name)
        missing = len(postings) - len(results)
        if missing > 0:
            log.warning(
                "llm.batch_incomplete",
                provider=self.backend.provider,
                requested=len(postings),
                returned=len(results),
                missing=missing,
            )
        return results

    def _pace(self, prompt: str) -> None:
        """Wait until this request fits inside the provider's per-minute budget.

        Reactive 429-retry alone loses batches: one ~5k-token request nearly fills
        an 8k/min ceiling, so the next burst fails and burns its retries. Pacing
        ahead of the call keeps the run inside the window instead.
        """
        now = time.monotonic()
        self._call_log = [(t, n) for t, n in self._call_log if now - t < 60]

        # Request-rate ceiling (Gemini free tier binds here, not on tokens).
        rpm = getattr(self, "requests_per_minute", 0)
        if rpm and len(self._call_log) >= rpm:
            oldest = min(t for t, _ in self._call_log)
            wait = max(60 - (now - oldest) + 1, 0)
            if wait > 0:
                log.info(
                    "llm.pacing_requests",
                    provider=self.backend.provider,
                    calls_last_min=len(self._call_log),
                    rpm_limit=rpm,
                    wait_seconds=round(wait, 1),
                )
                time.sleep(wait)
                now = time.monotonic()
                self._call_log = [(t, n) for t, n in self._call_log if now - t < 60]

        budget = self.tokens_per_minute
        if not budget:
            self._call_log.append((time.monotonic(), 0))
            return
        # ~3.1 chars/token measured against real Groq usage, not the usual 4:
        # estimating at chars/4 under-counts by ~30%, which sends one batch too
        # many into an 8k/min window and earns a 429 the pacer was meant to avoid.
        estimate = int(len(prompt) / 3.1) + 90 * self.batch_size
        used = sum(n for _, n in self._call_log)
        if used + estimate <= budget * 0.9:
            self._call_log.append((time.monotonic(), estimate))
            return
        oldest = min((t for t, _ in self._call_log), default=now)
        wait = max(60 - (now - oldest) + 1, 0)
        if wait > 0:
            log.info(
                "llm.pacing",
                provider=self.backend.provider,
                used_last_min=used,
                estimate=estimate,
                budget=budget,
                wait_seconds=round(wait, 1),
            )
            time.sleep(wait)
            now = time.monotonic()
            # Prune by time rather than clearing: the log also carries the
            # request-rate history that the RPM ceiling depends on.
            self._call_log = [(t, n) for t, n in self._call_log if now - t < 60]
        self._call_log.append((time.monotonic(), estimate))

    def _record_usage(self, before: int, prompt: str) -> None:
        """Replace the attempt's estimate with actual tokens, keeping its slot.

        The slot was appended by _pace before the call so that failed attempts
        still count against the request-rate budget.
        """
        after = getattr(self.backend, "tokens_used", 0)
        delta = after - before if after > before else len(prompt) // 4
        if self._call_log:
            ts, _ = self._call_log[-1]
            self._call_log[-1] = (ts, delta)
        else:
            self._call_log.append((time.monotonic(), delta))

    def _run_batch_with_ratelimit_retry(
        self,
        batch: Sequence[Any],
        template: str,
        idx: int,
    ) -> list[Classification]:
        """Run a batch, waiting out rate limits on the current provider.

        Free tiers rate-limit on a rolling window, so the limit clears on its own.
        Waiting is almost always better than failing over to a weaker local model,
        and it is the only option when no fallback is reachable.
        """
        for attempt in range(1, self.rate_limit_retries + 1):
            try:
                return self._run_batch(batch, template)
            except (RateLimitError, TransientError) as exc:
                if attempt == self.rate_limit_retries:
                    raise
                wait = exc.retry_after or (self.rate_limit_backoff * attempt)
                wait = min(wait, self.rate_limit_max_wait)
                log.warning(
                    "llm.transient_retry"
                    if isinstance(exc, TransientError)
                    else "llm.rate_limited_waiting",
                    provider=self.backend.provider,
                    batch=idx,
                    attempt=attempt,
                    wait_seconds=round(wait, 1),
                )
                time.sleep(wait)
        raise RuntimeError("unreachable")

    def classify_batch(
        self,
        postings: Sequence[Any],
        prompt_version: str,
        on_batch: Callable[[list[Classification]], None] | None = None,
    ) -> list[Classification]:
        """Classify postings in chunks. Returns every classification produced."""
        if not postings:
            return []

        template = load_prompt(prompt_version, self.prompts_dir)
        all_results: list[Classification] = []
        batches = list(_chunk(list(postings), self.batch_size))

        for idx, batch in enumerate(batches, start=1):
            try:
                results = self._run_batch_with_ratelimit_retry(batch, template, idx)
            except (RateLimitError, TransientError) as exc:
                log.warning(
                    "llm.retries_exhausted",
                    provider=self.backend.provider,
                    error=str(exc)[:200],
                )
                if not self._switch_to_fallback("rate_limit"):
                    log.error("llm.batch_abandoned_no_fallback", batch=idx)
                    continue
                try:
                    results = self._run_batch(batch, template)
                except Exception:
                    log.exception("llm.batch_failed_after_fallback", batch=idx)
                    continue
            except Exception:
                self._consecutive_failures += 1
                log.exception(
                    "llm.batch_failed",
                    provider=self.backend.provider,
                    batch=idx,
                    consecutive_failures=self._consecutive_failures,
                )
                if self._consecutive_failures >= 2:
                    if not self._switch_to_fallback("two_consecutive_failures"):
                        continue
                    try:
                        results = self._run_batch(batch, template)
                    except Exception:
                        log.exception("llm.batch_failed_after_fallback", batch=idx)
                        continue
                else:
                    continue
            else:
                self._consecutive_failures = 0

            log.info(
                "llm.batch_served",
                provider=self.backend.provider,
                model=self.backend.model,
                batch=idx,
                of=len(batches),
                requested=len(batch),
                classified=len(results),
            )
            all_results.extend(results)
            if on_batch is not None and results:
                # Hand each batch to the caller so it can persist incrementally;
                # otherwise a kill or a quota wall discards everything done so far.
                try:
                    on_batch(results)
                except Exception:
                    log.exception("llm.on_batch_failed", batch=idx)

        return all_results
