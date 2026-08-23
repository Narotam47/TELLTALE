from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CompanyRow(Base):
    __tablename__ = "companies"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    ats_platform: Mapped[str] = mapped_column(String(32))
    endpoint_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    postings: Mapped[list[JobPosting]] = relationship(back_populates="company")
    scrape_runs: Mapped[list[ScrapeRun]] = relationship(back_populates="company")


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("company_slug", "external_id", name="uq_company_external"),
        Index("ix_company_open", "company_slug", "is_open"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("companies.slug"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(512))
    raw_department: Mapped[str | None] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(String(256))
    employment_type: Mapped[str | None] = mapped_column(String(64))
    description_text: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)

    company: Mapped[CompanyRow] = relationship(back_populates="postings")
    classifications: Mapped[list[PostingClassification]] = relationship(
        back_populates="posting"
    )


class PostingClassification(Base):
    __tablename__ = "posting_classifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("job_postings.id"), index=True)
    function_category: Mapped[str] = mapped_column(String(128))
    seniority_level: Mapped[str] = mapped_column(String(64))
    rationale: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    classified_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    posting: Mapped[JobPosting] = relationship(back_populates="classifications")


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("companies.slug"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    records_found: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(32))
    error_text: Mapped[str | None] = mapped_column(Text)

    company: Mapped[CompanyRow] = relationship(back_populates="scrape_runs")


class WeeklyBrief(Base):
    __tablename__ = "weekly_briefs"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[datetime] = mapped_column(DateTime)
    brief_markdown: Mapped[str] = mapped_column(Text)
    signal_json: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
