from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from telltale.adapters import get_adapter
from telltale.config import Company, Settings, load_companies
from telltale.storage.models import JobPosting
from telltale.storage.repository import (
    close_missing,
    record_run,
    seed_companies_from_yaml,
    upsert_postings,
)
from telltale.storage.session import checkpoint_wal, get_session

log = structlog.get_logger()


@dataclass
class CompanyResult:
    slug: str
    status: str = "pending"
    found: int = 0
    new: int = 0
    updated: int = 0
    closed: int = 0
    error: str | None = None


@dataclass
class IngestSummary:
    results: list[CompanyResult] = field(default_factory=list)

    @property
    def total_errors(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")


def _count_open(session: Session, slug: str) -> int:
    return session.execute(
        select(func.count())
        .select_from(JobPosting)
        .where(JobPosting.company_slug == slug, JobPosting.is_open.is_(True))
    ).scalar_one()


def _existing_external_ids(session: Session, slug: str) -> set[str]:
    rows = session.execute(
        select(JobPosting.external_id).where(JobPosting.company_slug == slug)
    ).scalars().all()
    return set(rows)


def ingest_company(
    session: Session,
    company: Company,
    close_threshold: float = 0.5,
) -> CompanyResult:
    result = CompanyResult(slug=company.slug)
    started_at = datetime.now(UTC)

    run = record_run(session, company.slug, "running", started_at)
    session.commit()

    try:
        adapter_cls = get_adapter(company.adapter_type)
        adapter = adapter_cls(company)
        postings = adapter.run()
        result.found = len(postings)

        prev_open = _count_open(session, company.slug)
        existing_ext_ids = _existing_external_ids(session, company.slug)

        seen_ids = upsert_postings(session, company.slug, postings)

        new_ext_ids = {p["external_id"] for p in postings} - existing_ext_ids
        result.new = len(new_ext_ids)
        result.updated = result.found - result.new

        if prev_open > 0 and result.found < close_threshold * prev_open:
            log.warning(
                "ingest.suspect_drop",
                company=company.slug,
                prev_open=prev_open,
                found=result.found,
                threshold=close_threshold,
            )
            result.status = "suspect"
            result.closed = 0
        else:
            result.closed = close_missing(session, company.slug, seen_ids)
            result.status = "success"

        run.status = result.status
        run.records_found = result.found
        run.finished_at = datetime.now(UTC)
        session.commit()

    except Exception as exc:
        session.rollback()
        result.status = "failed"
        result.error = str(exc)
        log.exception("ingest.company_failed", company=company.slug)
        run = record_run(
            session,
            company.slug,
            "failed",
            started_at,
            finished_at=datetime.now(UTC),
            error_text=str(exc),
        )
        session.commit()

    return result


def ingest_all(
    company_slugs: list[str] | None = None,
    close_threshold: float | None = None,
) -> IngestSummary:
    settings = Settings()
    if close_threshold is None:
        close_threshold = getattr(settings, "close_threshold", 0.5)

    companies = load_companies()
    companies = [c for c in companies if c.enabled]

    if company_slugs:
        slug_set = set(company_slugs)
        companies = [c for c in companies if c.slug in slug_set]

    summary = IngestSummary()

    session = get_session()
    try:
        seed_companies_from_yaml(session, Path("companies.yaml"))
        session.commit()

        for company in companies:
            log.info("ingest.company_start", company=company.slug)
            result = ingest_company(session, company, close_threshold)
            summary.results.append(result)
            log.info(
                "ingest.company_done",
                company=company.slug,
                status=result.status,
                found=result.found,
                new=result.new,
                updated=result.updated,
                closed=result.closed,
            )
    finally:
        session.close()
        checkpoint_wal()

    return summary


def dry_run(
    company_slugs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch and normalize without writing to DB. Returns per-company counts."""
    companies = load_companies()
    companies = [c for c in companies if c.enabled]

    if company_slugs:
        slug_set = set(company_slugs)
        companies = [c for c in companies if c.slug in slug_set]

    results = []
    for company in companies:
        entry: dict[str, Any] = {"slug": company.slug, "adapter": company.adapter_type}
        try:
            adapter_cls = get_adapter(company.adapter_type)
            adapter = adapter_cls(company)
            postings = adapter.run()
            entry["found"] = len(postings)
            entry["status"] = "ok"
        except Exception as exc:
            entry["found"] = 0
            entry["status"] = f"error: {exc}"
        results.append(entry)

    return results
