from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from telltale.config import Settings
from telltale.llm.client import ClassificationClient
from telltale.llm.usage import record as record_usage
from telltale.pipeline.signals import SignalReport, build_signal_report
from telltale.storage.models import WeeklyBrief
from telltale.storage.session import checkpoint_wal, get_session

log = structlog.get_logger()

BRIEFS_DIR = Path("briefs")
BRIEF_PROMPT_VERSION = "brief_v1"
_REQUIRED_SECTIONS = ("## What moved", "## What it might mean", "## Methodology")


@dataclass
class BriefResult:
    markdown: str
    path: Path | None
    week_start: datetime
    model_name: str
    warnings: list[str]


def _load_brief_prompt(version: str = BRIEF_PROMPT_VERSION) -> str:
    path = Path("prompts") / f"{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Brief prompt not found: {path}")
    return path.read_text()


def audit_numbers(markdown: str, report: SignalReport) -> list[str]:
    """Flag figures in the brief that do not appear in the signal report.

    The prompt forbids invented numbers; this checks rather than trusts. Percentages
    and counts present in the report are allowed, as are small ordinals and years.
    """
    payload = report.to_dict()
    allowed: set[str] = set()

    def _walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                allowed.add(str(k))
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, (int, float)):
            allowed.add(f"{node}")
            if isinstance(node, float):
                allowed.add(f"{node:.0f}")
                allowed.add(f"{round(node, 1)}")
        elif node is not None:
            text = str(node)
            allowed.add(text)
            # Numbers quoted inside a note ("All 46 function entries...") are
            # sourced too; without this they read as invented.
            allowed.update(re.findall(r"\d+(?:\.\d+)?", text))

    _walk(payload)

    # Counts that are trivially derivable and not worth flagging.
    allowed |= {str(n) for n in range(11)}
    allowed |= {str(report.total_open), str(report.total_classified)}

    warnings: list[str] = []
    body = markdown.split("## Methodology")[0]
    # ISO dates come from the report but appear as whole strings; drop them so their
    # year/month/day parts are not each flagged as unsourced figures.
    body = re.sub(r"\d{4}-\d{2}-\d{2}", " ", body)
    for token in re.findall(r"\d+(?:\.\d+)?", body):
        if token in allowed:
            continue
        if token.rstrip("0") in allowed or f"{float(token):.1f}" in allowed:
            continue
        warnings.append(f"figure {token!r} in brief is not present in the report")
    return warnings


def _check_bullet_counts(markdown: str) -> list[str]:
    """The brief is a one-pager; over-long sections dilute it."""
    limits = {"## What moved": (3, 5), "## What it might mean": (2, 3)}
    out: list[str] = []
    for heading, (lo, hi) in limits.items():
        if heading not in markdown:
            continue
        section = markdown.split(heading, 1)[1].split("\n## ", 1)[0]
        n = len(re.findall(r"^\s*[-*]\s+\S", section, re.MULTILINE))
        if n < lo or n > hi:
            out.append(f"{heading.strip('# ')!r} has {n} bullets, expected {lo}-{hi}")
    return out


def render_snapshot_brief(
    report: SignalReport, reason: str = "classification was skipped"
) -> str:
    """Build a brief from the signal report with no LLM involved.

    CI must still produce something useful when no provider key is configured, and
    a templated summary of real numbers beats either failing the run or silently
    publishing nothing. Deliberately contains no interpretation: inference is the
    one part that needs a model.
    """
    d = report.to_dict()
    lines = [
        f"# Weekly Hiring Brief — {d['week_start']} to {d['week_end']}",
        "",
        f"**Headline:** Snapshot only — no narrative was generated because {reason}.",
        "",
        "## What moved",
        "",
    ]

    sector = d["sector_wide"][:5]
    if sector:
        for s in sector:
            lines.append(
                f"- {s['function']}: {s['open_now']} open roles, "
                f"{s['share_of_sector_pct']}% of all tracked postings."
            )
    else:
        lines.append("- No classified postings available for this week.")

    lines += ["", "## What it might mean", ""]
    lines.append(
        "- Not available. Interpretation requires the language model, and "
        f"{reason}."
    )
    lines.append(
        "- The figures above are computed directly from the database and are "
        "complete regardless."
    )

    lines += [
        "",
        "## Methodology",
        "",
        f"- Open postings: {d['totals']['open_postings']}",
        f"- Companies tracked: {d['totals']['companies_tracked']}",
        f"- Classification prompt version: {d['prompt_version']}",
        "- Brief generated without a language model (deterministic template).",
    ]
    for note in d["notes"]:
        lines.append(f"- {note}")

    return "\n".join(lines) + "\n"


def save_brief(
    markdown: str,
    report: SignalReport,
    briefs_dir: Path = BRIEFS_DIR,
) -> Path:
    """Write a brief to disk and to weekly_briefs."""
    briefs_dir.mkdir(parents=True, exist_ok=True)
    path = briefs_dir / f"{report.week_start.date().isoformat()}.md"
    path.write_text(markdown if markdown.endswith("\n") else markdown + "\n")

    session = get_session()
    try:
        week = report.week_start.replace(tzinfo=None)
        # One brief per week: the markdown file is overwritten on a re-run, so the
        # table must not accumulate near-duplicate rows the dashboard would list.
        session.query(WeeklyBrief).filter(WeeklyBrief.week_start == week).delete()
        session.add(
            WeeklyBrief(
                week_start=week,
                brief_markdown=markdown,
                signal_json=json.dumps(report.to_dict(), indent=2, sort_keys=True),
                generated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        session.commit()
    finally:
        session.close()
        checkpoint_wal()
    return path


def generate_brief(
    report: SignalReport | None = None,
    client: ClassificationClient | None = None,
    briefs_dir: Path = BRIEFS_DIR,
    persist: bool = True,
) -> BriefResult:
    """Turn a SignalReport into a one-page markdown brief."""
    report = report or build_signal_report()
    template = _load_brief_prompt()
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    prompt = template.replace("{{SIGNALS}}", payload)

    client = client or ClassificationClient(settings=Settings())
    log.info(
        "brief.start",
        provider=client.backend.provider,
        model=client.backend.model,
        cold_start=report.is_cold_start,
    )
    before = getattr(client.backend, "tokens_used", 0)
    markdown = client.backend.complete(prompt).strip()
    spent = getattr(client.backend, "tokens_used", 0) - before
    if spent > 0:
        record_usage(client.backend.provider, spent)

    # Strip a stray code fence if the model wrapped the document.
    if markdown.startswith("```"):
        markdown = re.sub(r"^```[a-z]*\n|\n```$", "", markdown).strip()

    warnings = audit_numbers(markdown, report)
    missing = [s for s in _REQUIRED_SECTIONS if s not in markdown]
    if missing:
        warnings.append(f"missing sections: {', '.join(missing)}")
    warnings.extend(_check_bullet_counts(markdown))
    for w in warnings:
        log.warning("brief.audit", detail=w)

    path: Path | None = None
    if persist:
        path = save_brief(markdown, report, briefs_dir)

    log.info("brief.done", path=str(path) if path else None, warnings=len(warnings))
    return BriefResult(
        markdown=markdown,
        path=path,
        week_start=report.week_start,
        model_name=client.backend.model_name,
        warnings=warnings,
    )
