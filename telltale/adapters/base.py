from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import structlog

from telltale.config import Company

log = structlog.get_logger()


class SourceAdapter(ABC):
    adapter_type: ClassVar[str]

    def __init__(self, company: Company) -> None:
        self.company = company

    @abstractmethod
    def fetch(self) -> list[dict[str, Any]]:
        """Fetch raw records from the source. Returns list of raw dicts."""

    @abstractmethod
    def normalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize raw records into dicts ready for upsert_postings."""

    def run(self) -> list[dict[str, Any]]:
        """Fetch + normalize with error handling. Returns normalized posting dicts."""
        slug = self.company.slug
        log.info("adapter.run.start", company=slug, adapter=self.adapter_type)
        try:
            raw = self.fetch()
            log.info("adapter.fetch.done", company=slug, raw_count=len(raw))
            postings = self.normalize(raw)
            dropped = len(raw) - len(postings)
            if dropped > 0:
                log.warning(
                    "adapter.normalize.dropped_records",
                    company=slug,
                    raw_count=len(raw),
                    normalized_count=len(postings),
                    dropped=dropped,
                )
            log.info("adapter.normalize.done", company=slug, posting_count=len(postings))
            return postings
        except Exception:
            log.exception("adapter.run.error", company=slug, adapter=self.adapter_type)
            raise
