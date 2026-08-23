from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from telltale.config import Company, load_companies
from telltale.storage.models import (
    CompanyRow,
    JobPosting,
    PostingClassification,
    ScrapeRun,
)


def compute_content_hash(title: str, description_text: str | None) -> str:
    payload = (title or "") + (description_text or "")
    return hashlib.sha256(payload.encode()).hexdigest()


def upsert_postings(
    session: Session,
    company_slug: str,
    postings: list[dict[str, Any]],
) -> list[int]:
    """Insert new postings or update existing ones. Returns list of posting IDs seen."""
    now = datetime.now(UTC)
    seen_ids: list[int] = []

    for p in postings:
        content_hash = compute_content_hash(p["title"], p.get("description_text"))
        external_id = str(p["external_id"])

        existing = session.execute(
            select(JobPosting).where(
                JobPosting.company_slug == company_slug,
                JobPosting.external_id == external_id,
            )
        ).scalar_one_or_none()

        if existing is None:
            row = JobPosting(
                company_slug=company_slug,
                external_id=external_id,
                title=p["title"],
                raw_department=p.get("raw_department"),
                location=p.get("location"),
                employment_type=p.get("employment_type"),
                description_text=p.get("description_text"),
                url=p.get("url"),
                content_hash=content_hash,
                first_seen_at=now,
                last_seen_at=now,
                is_open=True,
            )
            session.add(row)
            session.flush()
            seen_ids.append(row.id)
        else:
            existing.last_seen_at = now
            existing.is_open = True
            existing.title = p["title"]
            existing.raw_department = p.get("raw_department")
            existing.location = p.get("location")
            existing.employment_type = p.get("employment_type")
            existing.description_text = p.get("description_text")
            existing.url = p.get("url")
            existing.content_hash = content_hash
            seen_ids.append(existing.id)

    return seen_ids


def close_missing(
    session: Session,
    company_slug: str,
    seen_ids: list[int],
) -> int:
    """Mark postings not in seen_ids as closed. Returns count of closed postings."""
    stmt = (
        update(JobPosting)
        .where(
            JobPosting.company_slug == company_slug,
            JobPosting.is_open.is_(True),
            JobPosting.id.not_in(seen_ids) if seen_ids else True,
        )
        .values(is_open=False)
    )
    result = session.execute(stmt)
    return result.rowcount


def get_unclassified(
    session: Session,
    prompt_version: str,
) -> list[JobPosting]:
    """Return open postings that lack a classification at the given prompt_version."""
    classified_subq = (
        select(PostingClassification.posting_id)
        .where(PostingClassification.prompt_version == prompt_version)
        .correlate(JobPosting)
    )
    stmt = (
        select(JobPosting)
        .where(
            JobPosting.is_open.is_(True),
            JobPosting.id.not_in(classified_subq),
        )
        .order_by(JobPosting.id)
    )
    return list(session.execute(stmt).scalars().all())


def insert_classifications(
    session: Session,
    classifications: list[Any],
    prompt_version: str,
) -> int:
    """Insert classification rows. Returns the number inserted."""
    for c in classifications:
        session.add(
            PostingClassification(
                posting_id=c.posting_id,
                function_category=c.function_category,
                seniority_level=c.seniority_level,
                rationale=c.rationale,
                model_name=c.model_name,
                prompt_version=prompt_version,
            )
        )
    session.flush()
    return len(classifications)


def record_run(
    session: Session,
    company_slug: str,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    records_found: int | None = None,
    error_text: str | None = None,
) -> ScrapeRun:
    run = ScrapeRun(
        company_slug=company_slug,
        started_at=started_at,
        finished_at=finished_at or datetime.now(UTC),
        records_found=records_found,
        status=status,
        error_text=error_text,
    )
    session.add(run)
    session.flush()
    return run


def seed_companies_from_yaml(
    session: Session,
    path: Path = Path("companies.yaml"),
) -> int:
    """Sync companies.yaml into the companies table. Returns count of upserted rows."""
    companies: list[Company] = load_companies(path)
    count = 0
    for c in companies:
        existing = session.get(CompanyRow, c.slug)
        if existing is None:
            session.add(
                CompanyRow(
                    slug=c.slug,
                    name=c.name,
                    ats_platform=c.adapter_type,
                    endpoint_url=c.endpoint_url,
                    enabled=c.enabled,
                )
            )
        else:
            existing.name = c.name
            existing.ats_platform = c.adapter_type
            existing.endpoint_url = c.endpoint_url
            existing.enabled = c.enabled
        count += 1
    session.flush()
    return count
