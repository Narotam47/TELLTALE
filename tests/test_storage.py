from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from telltale.storage.models import Base, CompanyRow, JobPosting, PostingClassification
from telltale.storage.repository import (
    close_missing,
    compute_content_hash,
    get_unclassified,
    record_run,
    upsert_postings,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(CompanyRow(slug="acme", name="Acme Corp", ats_platform="greenhouse"))
    sess.commit()
    yield sess
    sess.close()


def _posting(ext_id: str = "ext-1", title: str = "Engineer", desc: str = "Build things"):
    return {
        "external_id": ext_id,
        "title": title,
        "description_text": desc,
        "location": "Bangalore",
    }


def test_upsert_creates_one_row_and_bumps_last_seen(session: Session):
    """Inserting the same posting twice creates one row and bumps last_seen_at."""
    ids_first = upsert_postings(session, "acme", [_posting()])
    session.commit()

    row = session.get(JobPosting, ids_first[0])
    first_seen_original = row.first_seen_at
    last_seen_first = row.last_seen_at

    ids_second = upsert_postings(session, "acme", [_posting()])
    session.commit()

    assert ids_first == ids_second
    row = session.get(JobPosting, ids_first[0])
    assert row.first_seen_at == first_seen_original
    assert row.last_seen_at >= last_seen_first

    count = session.query(JobPosting).filter_by(company_slug="acme").count()
    assert count == 1


def test_missing_posting_closed_on_successful_run(session: Session):
    """A posting missing from a second successful run gets is_open=False."""
    ids = upsert_postings(session, "acme", [_posting("ext-1"), _posting("ext-2", title="PM")])
    session.commit()

    second_ids = upsert_postings(session, "acme", [_posting("ext-1")])
    close_missing(session, "acme", second_ids)
    session.commit()

    open_row = session.get(JobPosting, ids[0])
    closed_row = session.get(JobPosting, ids[1])
    assert open_row.is_open is True
    assert closed_row.is_open is False


def test_missing_posting_stays_open_on_failed_run(session: Session):
    """A posting missing from a FAILED run stays is_open=True."""
    ids = upsert_postings(session, "acme", [_posting()])
    session.commit()

    now = datetime.now(UTC)
    record_run(
        session,
        company_slug="acme",
        status="error",
        started_at=now,
        finished_at=now,
        error_text="connection timeout",
    )
    session.commit()

    row = session.get(JobPosting, ids[0])
    assert row.is_open is True


def test_changed_content_hash_updates_and_needs_reclassification(session: Session):
    """A changed content_hash updates the row and makes it eligible for reclassification."""
    ids = upsert_postings(session, "acme", [_posting()])
    session.commit()
    original_hash = session.get(JobPosting, ids[0]).content_hash

    session.add(
        PostingClassification(
            posting_id=ids[0],
            function_category="engineering",
            seniority_level="mid",
            model_name="gemini-2.0-flash",
            prompt_version="v1",
        )
    )
    session.commit()

    assert get_unclassified(session, "v1") == []

    updated = _posting(desc="Build things differently")
    upsert_postings(session, "acme", [updated])
    session.commit()

    row = session.get(JobPosting, ids[0])
    assert row.content_hash != original_hash
    assert row.content_hash == compute_content_hash(updated["title"], updated["description_text"])


def test_get_unclassified_after_version_bump(session: Session):
    """get_unclassified returns postings after a prompt_version bump."""
    ids = upsert_postings(session, "acme", [_posting("a"), _posting("b", title="PM")])
    session.commit()

    unclassified_v1 = get_unclassified(session, "v1")
    assert len(unclassified_v1) == 2

    for pid in ids:
        session.add(
            PostingClassification(
                posting_id=pid,
                function_category="engineering",
                seniority_level="mid",
                model_name="gemini-2.0-flash",
                prompt_version="v1",
            )
        )
    session.commit()

    assert get_unclassified(session, "v1") == []
    unclassified_v2 = get_unclassified(session, "v2")
    assert len(unclassified_v2) == 2
