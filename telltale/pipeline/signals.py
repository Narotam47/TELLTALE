from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from telltale.storage.models import JobPosting, PostingClassification
from telltale.storage.session import get_session

log = structlog.get_logger()

DEFAULT_PROMPT_VERSION = "v1"


@dataclass
class FunctionMove:
    company: str
    function: str
    opened: int = 0
    closed: int = 0

    @property
    def net(self) -> int:
        return self.opened - self.closed


@dataclass
class FunctionShare:
    company: str
    function: str
    open_count: int
    share: float
    prior_share: float | None = None

    @property
    def share_delta(self) -> float | None:
        if self.prior_share is None:
            return None
        return self.share - self.prior_share


@dataclass
class FirstEntry:
    """First time a company has ever posted in a function."""

    company: str
    function: str
    title: str
    first_seen: datetime


@dataclass
class SectorMove:
    function: str
    opened: int
    closed: int
    open_now: int
    share_of_sector: float

    @property
    def net(self) -> int:
        return self.opened - self.closed


@dataclass
class SignalReport:
    week_start: datetime
    week_end: datetime
    prompt_version: str

    total_open: int = 0
    total_classified: int = 0
    companies: list[str] = field(default_factory=list)

    moves: list[FunctionMove] = field(default_factory=list)
    shares: list[FunctionShare] = field(default_factory=list)
    first_entries: list[FirstEntry] = field(default_factory=list)
    seniority_mix: dict[str, dict[str, int]] = field(default_factory=dict)
    sector: list[SectorMove] = field(default_factory=list)

    is_cold_start: bool = True
    prior_week_available: bool = False
    fastest_growing_function: str | None = None
    notes: list[str] = field(default_factory=list)

    def company_share(self, company: str) -> list[FunctionShare]:
        rows = [s for s in self.shares if s.company == company]
        return sorted(rows, key=lambda s: -s.share)

    def to_dict(self) -> dict:
        """Plain dict for the LLM and for persistence. Only real numbers."""
        return {
            "week_start": self.week_start.date().isoformat(),
            "week_end": self.week_end.date().isoformat(),
            "prompt_version": self.prompt_version,
            "cold_start": self.is_cold_start,
            "prior_week_available": self.prior_week_available,
            "totals": {
                "open_postings": self.total_open,
                "classified_postings": self.total_classified,
                "companies_tracked": len(self.companies),
            },
            "companies": self.companies,
            "function_mix_by_company": {
                c: {
                    s.function: {
                        "open": s.open_count,
                        "share_pct": round(s.share * 100, 1),
                        "share_delta_pct": (
                            None
                            if s.share_delta is None
                            else round(s.share_delta * 100, 1)
                        ),
                    }
                    for s in self.company_share(c)
                }
                for c in self.companies
            },
            "moves_this_week": [
                {
                    "company": m.company,
                    "function": m.function,
                    "opened": m.opened,
                    "closed": m.closed,
                    "net": m.net,
                }
                for m in self.moves
                if m.opened or m.closed
            ],
            "first_ever_entries": [
                {
                    "company": f.company,
                    "function": f.function,
                    "example_title": f.title,
                    "first_seen": f.first_seen.date().isoformat(),
                }
                for f in self.first_entries
            ],
            "seniority_mix_by_company": self.seniority_mix,
            "sector_wide": [
                {
                    "function": s.function,
                    "open_now": s.open_now,
                    "opened": s.opened,
                    "closed": s.closed,
                    "net": s.net,
                    "share_of_sector_pct": round(s.share_of_sector * 100, 1),
                }
                for s in self.sector
            ],
            "fastest_growing_function": self.fastest_growing_function,
            "notes": self.notes,
        }


def _week_bounds(week_start: datetime | None) -> tuple[datetime, datetime]:
    if week_start is None:
        now = datetime.now(UTC)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return week_start, week_start + timedelta(days=7)


def build_signal_report(
    session: Session | None = None,
    week_start: datetime | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
) -> SignalReport:
    """Compute weekly hiring signals from stored postings and classifications."""
    own = session is None
    session = session or get_session()
    start, end = _week_bounds(week_start)
    report = SignalReport(week_start=start, week_end=end, prompt_version=prompt_version)

    try:
        rows = session.execute(
            select(
                JobPosting.company_slug,
                JobPosting.title,
                JobPosting.first_seen_at,
                JobPosting.last_seen_at,
                JobPosting.is_open,
                PostingClassification.function_category,
                PostingClassification.seniority_level,
            )
            .join(
                PostingClassification,
                PostingClassification.posting_id == JobPosting.id,
            )
            .where(PostingClassification.prompt_version == prompt_version)
        ).all()

        if not rows:
            report.notes.append(
                f"No classifications at prompt_version={prompt_version!r}."
            )
            return report

        report.total_classified = len(rows)
        report.companies = sorted({r.company_slug for r in rows})
        open_rows = [r for r in rows if r.is_open]
        report.total_open = len(open_rows)

        # --- how many distinct scrape weeks exist? drives cold start ---
        run_weeks = session.execute(
            select(func.count(func.distinct(func.strftime("%Y-%W", JobPosting.first_seen_at))))
        ).scalar_one()
        report.prior_week_available = bool(run_weeks and run_weeks > 1)
        report.is_cold_start = not report.prior_week_available
        if report.is_cold_start:
            report.notes.append(
                "Cold start: only one week of history, so opened/closed counts are "
                "the initial snapshot and week-over-week comparisons are unavailable."
            )

        def _in_week(ts: datetime | None) -> bool:
            if ts is None:
                return False
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return start <= ts < end

        # --- opened / closed per company per function ---
        moves: dict[tuple[str, str], FunctionMove] = {}
        for r in rows:
            key = (r.company_slug, r.function_category)
            mv = moves.setdefault(key, FunctionMove(*key))
            if _in_week(r.first_seen_at):
                mv.opened += 1
            if not r.is_open and _in_week(r.last_seen_at):
                mv.closed += 1
        report.moves = sorted(
            moves.values(), key=lambda m: (-abs(m.net), m.company, m.function)
        )

        # --- share of each company's OPEN roles by function ---
        per_company_total: dict[str, int] = {}
        per_company_function: dict[tuple[str, str], int] = {}
        for r in open_rows:
            per_company_total[r.company_slug] = per_company_total.get(r.company_slug, 0) + 1
            k = (r.company_slug, r.function_category)
            per_company_function[k] = per_company_function.get(k, 0) + 1
        report.shares = [
            FunctionShare(
                company=co,
                function=fn,
                open_count=n,
                share=n / per_company_total[co] if per_company_total[co] else 0.0,
            )
            for (co, fn), n in sorted(per_company_function.items())
        ]

        # --- first-ever entry into a function, per company ---
        for co, fn in per_company_function:
            matching = [
                r for r in rows
                if r.company_slug == co and r.function_category == fn
            ]
            earliest = min(matching, key=lambda r: r.first_seen_at)
            if _in_week(earliest.first_seen_at) and len(matching) >= 1:
                report.first_entries.append(
                    FirstEntry(
                        company=co,
                        function=fn,
                        title=earliest.title,
                        first_seen=earliest.first_seen_at,
                    )
                )
        report.first_entries.sort(key=lambda f: (f.company, f.function))
        if report.is_cold_start and report.first_entries:
            report.notes.append(
                f"All {len(report.first_entries)} function entries are first-ever by "
                "definition on a cold start; they are not new strategic moves."
            )

        # --- seniority mix per company ---
        mix: dict[str, dict[str, int]] = {}
        for r in open_rows:
            mix.setdefault(r.company_slug, {})
            mix[r.company_slug][r.seniority_level] = (
                mix[r.company_slug].get(r.seniority_level, 0) + 1
            )
        report.seniority_mix = {c: dict(sorted(v.items())) for c, v in sorted(mix.items())}

        # --- sector-wide ---
        sector_open: dict[str, int] = {}
        sector_opened: dict[str, int] = {}
        sector_closed: dict[str, int] = {}
        for r in rows:
            fn = r.function_category
            if r.is_open:
                sector_open[fn] = sector_open.get(fn, 0) + 1
            if _in_week(r.first_seen_at):
                sector_opened[fn] = sector_opened.get(fn, 0) + 1
            if not r.is_open and _in_week(r.last_seen_at):
                sector_closed[fn] = sector_closed.get(fn, 0) + 1

        total_open = sum(sector_open.values()) or 1
        report.sector = sorted(
            (
                SectorMove(
                    function=fn,
                    opened=sector_opened.get(fn, 0),
                    closed=sector_closed.get(fn, 0),
                    open_now=n,
                    share_of_sector=n / total_open,
                )
                for fn, n in sector_open.items()
            ),
            key=lambda s: -s.open_now,
        )

        if report.prior_week_available and report.sector:
            best = max(report.sector, key=lambda s: s.net)
            report.fastest_growing_function = best.function if best.net > 0 else None
        else:
            report.fastest_growing_function = None
            report.notes.append(
                "fastest_growing_function requires at least two weeks of data."
            )

        log.info(
            "signals.built",
            open_postings=report.total_open,
            companies=len(report.companies),
            cold_start=report.is_cold_start,
        )
    finally:
        if own:
            session.close()

    return report
