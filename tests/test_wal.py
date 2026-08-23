from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from telltale.storage.models import Base, CompanyRow, JobPosting
from telltale.storage.session import checkpoint_wal


def _seed(session, n: int = 25) -> None:
    session.add(CompanyRow(
        slug="walco", name="WalCo", ats_platform="greenhouse",
        endpoint_url="https://example.com", enabled=True,
    ))
    session.flush()
    now = datetime.now(UTC)
    for i in range(n):
        session.add(JobPosting(
            company_slug="walco",
            external_id=f"job-{i}",
            title=f"Engineer {i}",
            content_hash=f"hash-{i}",
            first_seen_at=now,
            last_seen_at=now,
            is_open=True,
        ))
    session.commit()


@pytest.fixture()
def wal_db(tmp_path):
    db_path = tmp_path / "telltale.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield db_path, engine, session
    session.close()
    engine.dispose()


def test_wal_sidecar_is_active_before_checkpoint(wal_db):
    """Guards the premise: without a checkpoint, data really does live in -wal."""
    db_path, engine, session = wal_db
    _seed(session)
    session.close()

    wal = db_path.with_name(db_path.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "expected WAL mode to be active"


def test_db_file_is_self_contained_after_checkpoint(wal_db, tmp_path):
    """The .db file alone, with no -wal sidecar, must hold every committed row."""
    db_path, engine, session = wal_db
    _seed(session, n=25)
    session.close()

    assert checkpoint_wal(engine) is True

    # Copy ONLY the main db file, exactly as `cp telltale.db` or a CI commit would.
    isolated = tmp_path / "isolated.db"
    shutil.copyfile(db_path, isolated)
    for sidecar in ("-wal", "-shm"):
        assert not isolated.with_name(isolated.name + sidecar).exists()

    con = sqlite3.connect(isolated)
    try:
        count = con.execute("select count(*) from job_postings").fetchone()[0]
    finally:
        con.close()

    assert count == 25


def test_checkpoint_truncates_wal_file(wal_db):
    db_path, engine, session = wal_db
    _seed(session)
    session.close()

    checkpoint_wal(engine)

    wal = db_path.with_name(db_path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_checkpoint_accepts_session(wal_db):
    db_path, engine, session = wal_db
    _seed(session)
    assert checkpoint_wal(session) is True


def test_checkpoint_noop_on_memory_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert checkpoint_wal(engine) is False
