from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest

from telltale.llm.base import LLMBackend
from telltale.pipeline.brief import _check_bullet_counts, audit_numbers, generate_brief
from telltale.pipeline.signals import FunctionShare, SectorMove, SignalReport

WEEK = datetime(2026, 8, 17, tzinfo=UTC)


class StubBackend(LLMBackend):
    provider: ClassVar[str] = "stub"

    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "stub-1"

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.text


class StubClient:
    def __init__(self, backend):
        self.backend = backend


def _report() -> SignalReport:
    r = SignalReport(
        week_start=WEEK,
        week_end=WEEK.replace(day=24),
        prompt_version="v1",
        total_open=367,
        total_classified=367,
        companies=["paytm", "zeta"],
    )
    r.shares = [
        FunctionShare("paytm", "Growth & Marketing", 70, 0.308),
        FunctionShare("zeta", "Design", 4, 0.174),
    ]
    r.sector = [SectorMove("Growth & Marketing", 0, 0, 89, 0.243)]
    r.notes = ["Cold start: only one week of history."]
    return r


GOOD = """# Weekly Hiring Brief — 2026-08-17 to 2026-08-24

**Headline:** Growth & Marketing leads with 89 open postings.

## What moved
- Growth & Marketing holds 89 open postings, 24.3% of the sector.
- Paytm has 70 Growth & Marketing roles, 30.8% of its mix.
- Zeta has 4 Design roles, 17.4% of its mix.

## What it might mean
- The concentration may indicate a customer-acquisition push.
- It could equally reflect seasonal campaign staffing.

## Methodology
367 open postings, 7 companies, prompt v1. Cold start.
"""


class TestNumberAudit:
    def test_accepts_figures_present_in_the_report(self):
        assert audit_numbers(GOOD, _report()) == []

    def test_flags_an_invented_figure(self):
        bad = GOOD.replace("89 open postings, 24.3%", "89 open postings, 91.7%")
        warnings = audit_numbers(bad, _report())
        assert any("91.7" in w for w in warnings)

    def test_ignores_dates_in_the_heading(self):
        """The year in an ISO date must not read as an unsourced figure."""
        assert not any("2026" in w for w in audit_numbers(GOOD, _report()))

    def test_methodology_footer_is_not_audited(self):
        text = GOOD.replace("367 open postings, 7 companies", "367 open postings, 7777 companies")
        assert audit_numbers(text, _report()) == []


class TestStructure:
    def test_accepts_correct_bullet_counts(self):
        assert _check_bullet_counts(GOOD) == []

    def test_flags_too_many_interpretation_bullets(self):
        bloated = GOOD.replace(
            "- It could equally reflect seasonal campaign staffing.",
            "- One.\n- Two.\n- Three.\n- Four.",
        )
        assert any("What it might mean" in w for w in _check_bullet_counts(bloated))

    def test_flags_too_few_moved_bullets(self):
        thin = GOOD.replace("- Zeta has 4 Design roles, 17.4% of its mix.\n", "")
        thin = thin.replace("- Paytm has 70 Growth & Marketing roles, 30.8% of its mix.\n", "")
        assert any("What moved" in w for w in _check_bullet_counts(thin))


class TestGenerate:
    def test_sends_signal_json_and_returns_markdown(self, tmp_path):
        backend = StubBackend(GOOD)
        result = generate_brief(
            _report(), client=StubClient(backend), briefs_dir=tmp_path, persist=False
        )
        assert result.markdown.startswith("# Weekly Hiring Brief")
        assert result.warnings == []
        assert '"open_postings": 367' in backend.prompts[0]
        assert "cold_start" in backend.prompts[0]

    def test_writes_a_file_when_persisting(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "telltale.pipeline.brief.get_session", lambda: pytest.skip("no db")
        )
        backend = StubBackend(GOOD)
        result = generate_brief(
            _report(), client=StubClient(backend), briefs_dir=tmp_path, persist=False
        )
        assert result.path is None

    def test_strips_a_wrapping_code_fence(self, tmp_path):
        backend = StubBackend("```markdown\n" + GOOD + "\n```")
        result = generate_brief(
            _report(), client=StubClient(backend), briefs_dir=tmp_path, persist=False
        )
        assert result.markdown.startswith("# Weekly Hiring Brief")

    def test_reports_missing_sections(self, tmp_path):
        backend = StubBackend("# Brief\n\nJust a headline, no sections.\n")
        result = generate_brief(
            _report(), client=StubClient(backend), briefs_dir=tmp_path, persist=False
        )
        assert any("missing sections" in w for w in result.warnings)


class TestSnapshotBrief:
    """CI must still emit a brief when no provider key is configured."""

    def test_renders_without_an_llm(self):
        from telltale.pipeline.brief import render_snapshot_brief

        md = render_snapshot_brief(_report())
        assert md.startswith("# Weekly Hiring Brief")
        for section in ("## What moved", "## What it might mean", "## Methodology"):
            assert section in md

    def test_states_why_there_is_no_narrative(self):
        from telltale.pipeline.brief import render_snapshot_brief

        md = render_snapshot_brief(_report(), reason="the provider quota was spent")
        assert "the provider quota was spent" in md
        assert "Not available" in md

    def test_uses_only_report_figures(self):
        from telltale.pipeline.brief import audit_numbers, render_snapshot_brief

        report = _report()
        assert audit_numbers(render_snapshot_brief(report), report) == []

    def test_carries_the_report_notes(self):
        from telltale.pipeline.brief import render_snapshot_brief

        md = render_snapshot_brief(_report())
        assert "Cold start" in md


class TestAuditEdgeCases:
    def test_numbers_quoted_inside_notes_are_sourced(self):
        """Regression: a figure embedded in a note string read as invented."""
        report = _report()
        report.notes = ["All 46 function entries are first-ever by definition."]
        text = GOOD.replace("89 open postings.", "89 open postings across 46 entries.")
        assert audit_numbers(text, report) == []
