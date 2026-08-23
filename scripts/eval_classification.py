#!/usr/bin/env python3
"""Sample classified postings for hand-labelling, then score the completed sheet.

    uv run python scripts/eval_classification.py sample --out eval.csv
    # ... fill in correct_category / correct_seniority by hand ...
    uv run python scripts/eval_classification.py report --csv eval.csv
"""
from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import click
from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telltale.llm.base import FUNCTION_CATEGORIES
from telltale.storage.models import JobPosting, PostingClassification
from telltale.storage.session import get_session

FIELDNAMES = [
    "posting_id",
    "company",
    "title",
    "predicted_category",
    "predicted_seniority",
    "correct_category",
    "correct_seniority",
    "notes",
]


def _fetch_classified(session: Session, prompt_version: str) -> list[dict]:
    stmt = (
        select(JobPosting, PostingClassification)
        .join(PostingClassification, PostingClassification.posting_id == JobPosting.id)
        .where(PostingClassification.prompt_version == prompt_version)
        .order_by(PostingClassification.id.desc())
    )
    rows = session.execute(stmt).all()

    seen: set[int] = set()
    out: list[dict] = []
    for posting, cls in rows:
        if posting.id in seen:
            continue
        seen.add(posting.id)
        out.append({
            "posting_id": posting.id,
            "company": posting.company_slug,
            "title": posting.title,
            "predicted_category": cls.function_category,
            "predicted_seniority": cls.seniority_level,
            "correct_category": "",
            "correct_seniority": "",
            "notes": "",
        })
    return out


def _stratify(rows: list[dict], n: int, max_per_company: int | None = None) -> list[dict]:
    """Round-robin across companies, capping any one company's contribution.

    Round-robin alone already balances a skewed dataset, but the cap makes the
    guarantee explicit: with Paytm at ~62% of postings, an uncapped random sample
    would be mostly Paytm and tell us little about the other six.
    """
    by_company: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_company[r["company"]].append(r)

    buckets = {k: iter(v) for k, v in by_company.items()}
    taken: Counter = Counter()
    picked: list[dict] = []

    while len(picked) < n and buckets:
        for company in list(buckets):
            if len(picked) >= n:
                break
            if max_per_company is not None and taken[company] >= max_per_company:
                del buckets[company]
                continue
            try:
                picked.append(next(buckets[company]))
                taken[company] += 1
            except StopIteration:
                del buckets[company]
    return picked


@click.group()
def cli() -> None:
    """Evaluate classification quality against hand labels."""


@cli.command()
@click.option("--out", type=click.Path(), default="eval/sample_v1.csv")
@click.option("--n", type=int, default=50, help="Sample size.")
@click.option(
    "--max-per-company",
    type=int,
    default=15,
    help="Cap on rows from any one company, so Paytm cannot dominate the sample.",
)
@click.option("--prompt-version", default="v1")
def sample(out: str, n: int, max_per_company: int, prompt_version: str) -> None:
    """Write a stratified sample of classified postings to CSV for labelling."""
    session = get_session()
    try:
        rows = _fetch_classified(session, prompt_version)
    finally:
        session.close()

    if not rows:
        raise click.ClickException(
            f"No classifications found at prompt_version={prompt_version!r}. "
            "Run `telltale classify` first."
        )

    picked = _stratify(rows, n, max_per_company=max_per_company)
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(picked)

    companies = Counter(r["company"] for r in picked)
    click.echo(f"Wrote {len(picked)} rows to {path}")
    click.echo("\nPer-company coverage:")
    for company, count in companies.most_common():
        click.echo(f"  {company:<16} {count}")
    click.echo("\nFill in correct_category and correct_seniority, then run:")
    click.echo(f"  uv run python scripts/eval_classification.py report --csv {path}")


def _accuracy_report(rows: list[dict], pred_key: str, true_key: str, label: str) -> None:
    labelled = [r for r in rows if r.get(true_key, "").strip()]
    if not labelled:
        click.echo(f"\n{label}: no hand labels found in column {true_key!r}.")
        return

    correct = sum(1 for r in labelled if r[pred_key].strip() == r[true_key].strip())
    total = len(labelled)
    click.echo(f"\n{label} accuracy: {correct}/{total} = {correct / total:.1%}")

    per_class_total: Counter = Counter()
    per_class_hit: Counter = Counter()
    for r in labelled:
        truth = r[true_key].strip()
        per_class_total[truth] += 1
        if r[pred_key].strip() == truth:
            per_class_hit[truth] += 1

    click.echo(f"\nPer-{label.lower()} accuracy (by true label):")
    click.echo(f"  {'label':<26} {'hit':>4} {'n':>4}  acc")
    for name, tot in sorted(per_class_total.items(), key=lambda kv: -kv[1]):
        hit = per_class_hit[name]
        click.echo(f"  {name:<26} {hit:>4} {tot:>4}  {hit / tot:.0%}")

    confusion: Counter = Counter()
    for r in labelled:
        truth = r[true_key].strip()
        pred = r[pred_key].strip()
        if truth != pred:
            confusion[(truth, pred)] += 1

    if confusion:
        click.echo(f"\n{label} confusions (true -> predicted):")
        for (truth, pred), count in confusion.most_common():
            click.echo(f"  {count:>3}x  {truth}  ->  {pred}")
    else:
        click.echo(f"\nNo {label.lower()} confusions.")


@cli.command()
@click.option("--csv", "csv_path", type=click.Path(exists=True), required=True)
def report(csv_path: str) -> None:
    """Read a completed CSV and report accuracy plus a confusion matrix."""
    with Path(csv_path).open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    click.echo(f"Rows in sheet: {len(rows)}")

    unknown = {
        r["correct_category"].strip()
        for r in rows
        if r.get("correct_category", "").strip()
        and r["correct_category"].strip() not in FUNCTION_CATEGORIES
    }
    if unknown:
        click.echo("\nWARNING: hand labels not in the taxonomy: " + ", ".join(sorted(unknown)))

    _accuracy_report(rows, "predicted_category", "correct_category", "Category")
    _accuracy_report(rows, "predicted_seniority", "correct_seniority", "Seniority")

    both = [
        r for r in rows
        if r.get("correct_category", "").strip() and r.get("correct_seniority", "").strip()
    ]
    if both:
        exact = sum(
            1 for r in both
            if r["predicted_category"].strip() == r["correct_category"].strip()
            and r["predicted_seniority"].strip() == r["correct_seniority"].strip()
        )
        click.echo(f"\nBoth fields correct: {exact}/{len(both)} = {exact / len(both):.1%}")


if __name__ == "__main__":
    cli()
