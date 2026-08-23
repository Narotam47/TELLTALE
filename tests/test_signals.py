from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from telltale.pipeline.signals import build_signal_report
from telltale.storage.models import (
    Base,
    CompanyRow,
    JobPosting,
    PostingClassification,
)

WEEK = datetime(2026, 8, 17, tzinfo=UTC)
PV = "v1"


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _add(session, company, title, function, seniority="Mid", *, is_open=True,
         first_seen=None, last_seen=None):
    if session.get(CompanyRow, company) is None:
        session.add(CompanyRow(
            slug=company, name=company.title(), ats_platform="greenhouse",
            endpoint_url="https://x", enabled=True,
        ))
        session.flush()
    first_seen = first_seen or WEEK + timedelta(days=1)
    p = JobPosting(
        company_slug=company, external_id=f"{company}-{title}", title=title,
        content_hash=f"h-{company}-{title}", is_open=is_open,
        first_seen_at=first_seen.replace(tzinfo=None),
        last_seen_at=(last_seen or first_seen).replace(tzinfo=None),
    )
    session.add(p)
    session.flush()
    session.add(PostingClassification(
        posting_id=p.id, function_category=function, seniority_level=seniority,
        rationale="test", model_name="test:1", prompt_version=PV,
    ))
    session.flush()
    return p


class TestEmptyAndColdStart:
    def test_no_classifications_returns_empty_report(self, session):
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        assert r.total_classified == 0
        assert r.is_cold_start
        assert any("No classifications" in n for n in r.notes)

    def test_cold_start_suppresses_comparative_signals(self, session):
        _add(session, "paytm", "Engineer", "Engineering - Platform & Payments")
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)

        assert r.is_cold_start is True
        assert r.prior_week_available is False
        assert r.fastest_growing_function is None
        assert any("two weeks" in n for n in r.notes)

    def test_cold_start_flags_first_entries_as_definitional(self, session):
        _add(session, "paytm", "Engineer", "Engineering - Platform & Payments")
        _add(session, "zeta", "Designer", "Design")
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)

        assert len(r.first_entries) == 2
        assert any("first-ever by definition" in n for n in r.notes)


class TestShares:
    def test_share_is_relative_not_absolute(self, session):
        """A large company must not dominate: shares are within-company."""
        for i in range(20):
            _add(session, "paytm", f"Sales {i}", "Growth & Marketing")
        _add(session, "groww", "Sales", "Growth & Marketing")
        _add(session, "groww", "Engineer", "Engineering - Platform & Payments")
        session.commit()

        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        paytm = {s.function: s.share for s in r.company_share("paytm")}
        groww = {s.function: s.share for s in r.company_share("groww")}

        assert paytm["Growth & Marketing"] == pytest.approx(1.0)
        assert groww["Growth & Marketing"] == pytest.approx(0.5)
        assert groww["Engineering - Platform & Payments"] == pytest.approx(0.5)

    def test_shares_sum_to_one_per_company(self, session):
        _add(session, "zeta", "A", "Product")
        _add(session, "zeta", "B", "Design")
        _add(session, "zeta", "C", "Design")
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        assert sum(s.share for s in r.company_share("zeta")) == pytest.approx(1.0)

    def test_closed_postings_excluded_from_share(self, session):
        _add(session, "zeta", "Open", "Product")
        _add(session, "zeta", "Gone", "Design", is_open=False)
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        funcs = {s.function for s in r.company_share("zeta")}
        assert funcs == {"Product"}
        assert r.total_open == 1


class TestMoves:
    def test_counts_opens_and_closes_in_window(self, session):
        _add(session, "paytm", "New", "Product", first_seen=WEEK + timedelta(days=2))
        _add(session, "paytm", "Gone", "Product", is_open=False,
             first_seen=WEEK - timedelta(days=40),
             last_seen=WEEK + timedelta(days=3))
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        mv = next(m for m in r.moves if m.company == "paytm" and m.function == "Product")
        assert mv.opened == 1
        assert mv.closed == 1
        assert mv.net == 0

    def test_activity_outside_the_window_is_ignored(self, session):
        _add(session, "paytm", "Old", "Product", first_seen=WEEK - timedelta(days=30))
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        assert all(m.opened == 0 for m in r.moves)


class TestSeniorityAndSector:
    def test_seniority_mix_per_company(self, session):
        _add(session, "zeta", "A", "Product", "Senior")
        _add(session, "zeta", "B", "Product", "Senior")
        _add(session, "zeta", "C", "Product", "Junior")
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        assert r.seniority_mix["zeta"] == {"Junior": 1, "Senior": 2}

    def test_sector_shares_sum_to_one(self, session):
        _add(session, "a", "1", "Product")
        _add(session, "b", "2", "Design")
        _add(session, "c", "3", "Design")
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        assert sum(s.share_of_sector for s in r.sector) == pytest.approx(1.0)


class TestSerialisation:
    def test_to_dict_is_json_safe(self, session):
        import json

        _add(session, "paytm", "Engineer", "Engineering - Platform & Payments")
        session.commit()
        r = build_signal_report(session, week_start=WEEK, prompt_version=PV)
        text = json.dumps(r.to_dict())
        assert "paytm" in text

    def test_to_dict_marks_missing_comparisons_as_null(self, session):
        _add(session, "paytm", "Engineer", "Product")
        session.commit()
        d = build_signal_report(session, week_start=WEEK, prompt_version=PV).to_dict()
        assert d["fastest_growing_function"] is None
        assert d["cold_start"] is True
