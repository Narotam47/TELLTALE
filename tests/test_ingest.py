from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from telltale.adapters.base import SourceAdapter
from telltale.config import Company
from telltale.pipeline.ingest import ingest_company
from telltale.storage.models import Base, CompanyRow, JobPosting, ScrapeRun
from telltale.storage.repository import upsert_postings


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", echo=False)

    @event.listens_for(Engine, "connect")
    def _set_pragma(conn, _):
        import sqlite3
        if isinstance(conn, sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_company(slug: str = "testco", adapter_type: str = "greenhouse", **kw) -> Company:
    defaults = {
        "name": slug.title(),
        "slug": slug,
        "careers_url": f"https://{slug}.com/careers",
        "adapter_type": adapter_type,
        "endpoint_url": f"https://api.example.com/{slug}",
    }
    defaults.update(kw)
    return Company(**defaults)


def _seed_company(session: Session, company: Company) -> None:
    session.add(CompanyRow(
        slug=company.slug,
        name=company.name,
        ats_platform=company.adapter_type,
        endpoint_url=company.endpoint_url,
        enabled=company.enabled,
    ))
    session.flush()


class FakeAdapter(SourceAdapter):
    adapter_type: ClassVar[str] = "fake"

    def __init__(self, company: Company, postings: list[dict[str, Any]] | None = None):
        super().__init__(company)
        self._postings = postings or []

    def fetch(self) -> list[dict[str, Any]]:
        return self._postings

    def normalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return raw


class FailingAdapter(SourceAdapter):
    adapter_type: ClassVar[str] = "failing"

    def fetch(self) -> list[dict[str, Any]]:
        raise ConnectionError("API unreachable")

    def normalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return raw


def _make_postings(n: int, prefix: str = "job") -> list[dict[str, Any]]:
    postings = []
    for i in range(n):
        title = f"{prefix}-{i}"
        from telltale.storage.repository import compute_content_hash
        postings.append({
            "external_id": f"{prefix}-{i}",
            "title": title,
            "content_hash": compute_content_hash(title, None),
        })
    return postings


class TestIngestCompany:
    def test_success_inserts_postings(self, db_session):
        company = _make_company("alpha")
        _seed_company(db_session, company)

        postings = _make_postings(3)
        adapter = FakeAdapter(company, postings)

        with patch("telltale.pipeline.ingest.get_adapter", return_value=lambda c: adapter):
            result = ingest_company(db_session, company)

        assert result.status == "success"
        assert result.found == 3
        assert result.new == 3
        assert result.updated == 0

    def test_failing_adapter_records_error(self, db_session):
        company = _make_company("beta")
        _seed_company(db_session, company)

        with patch("telltale.pipeline.ingest.get_adapter", return_value=FailingAdapter):
            result = ingest_company(db_session, company)

        assert result.status == "failed"
        assert "API unreachable" in result.error

        runs = db_session.execute(
            select(ScrapeRun).where(ScrapeRun.company_slug == "beta")
        ).scalars().all()
        failed_runs = [r for r in runs if r.status == "failed"]
        assert len(failed_runs) >= 1

    def test_failing_adapter_does_not_stop_others(self, db_session):
        good_co = _make_company("good")
        bad_co = _make_company("bad")
        _seed_company(db_session, good_co)
        _seed_company(db_session, bad_co)

        good_postings = _make_postings(2, "good")
        good_adapter = FakeAdapter(good_co, good_postings)

        def mock_get_adapter(adapter_type):
            def factory(company):
                if company.slug == "bad":
                    return FailingAdapter(company)
                return good_adapter
            return factory

        with patch("telltale.pipeline.ingest.get_adapter", side_effect=mock_get_adapter):
            r_bad = ingest_company(db_session, bad_co)
            r_good = ingest_company(db_session, good_co)

        assert r_bad.status == "failed"
        assert r_good.status == "success"
        assert r_good.found == 2


class TestGuardRail:
    def test_suspect_when_below_threshold(self, db_session):
        company = _make_company("guardrail")
        _seed_company(db_session, company)

        initial = _make_postings(10)
        upsert_postings(db_session, "guardrail", initial)
        db_session.commit()

        small_batch = _make_postings(3, "new")
        adapter = FakeAdapter(company, small_batch)

        with patch("telltale.pipeline.ingest.get_adapter", return_value=lambda c: adapter):
            result = ingest_company(db_session, company, close_threshold=0.5)

        assert result.status == "suspect"
        assert result.closed == 0

        still_open = db_session.execute(
            select(func.count())
            .select_from(JobPosting)
            .where(
                JobPosting.company_slug == "guardrail",
                JobPosting.is_open.is_(True),
            )
        ).scalar_one()
        assert still_open == 13

    def test_closes_normally_above_threshold(self, db_session):
        company = _make_company("normal")
        _seed_company(db_session, company)

        initial = _make_postings(10)
        upsert_postings(db_session, "normal", initial)
        db_session.commit()

        subset = _make_postings(8)
        adapter = FakeAdapter(company, subset)

        with patch("telltale.pipeline.ingest.get_adapter", return_value=lambda c: adapter):
            result = ingest_company(db_session, company, close_threshold=0.5)

        assert result.status == "success"
        assert result.closed == 2
        assert result.updated == 8

    def test_custom_threshold(self, db_session):
        company = _make_company("custom_th")
        _seed_company(db_session, company)

        initial = _make_postings(10)
        upsert_postings(db_session, "custom_th", initial)
        db_session.commit()

        seven = _make_postings(7)
        adapter = FakeAdapter(company, seven)

        with patch("telltale.pipeline.ingest.get_adapter", return_value=lambda c: adapter):
            result = ingest_company(db_session, company, close_threshold=0.8)

        assert result.status == "suspect"
        assert result.closed == 0


class TestSummaryCounts:
    def test_summary_counts_correct(self, db_session):
        company = _make_company("counts")
        _seed_company(db_session, company)

        initial = _make_postings(5)
        upsert_postings(db_session, "counts", initial)
        db_session.commit()

        updated_batch = _make_postings(3) + _make_postings(2, "brand_new")
        adapter = FakeAdapter(company, updated_batch)

        with patch("telltale.pipeline.ingest.get_adapter", return_value=lambda c: adapter):
            result = ingest_company(db_session, company)

        assert result.found == 5
        assert result.new == 2
        assert result.updated == 3
        assert result.closed == 2
        assert result.status == "success"
