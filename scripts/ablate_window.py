#!/usr/bin/env python3
"""Ablation: does boilerplate stripping change labels, holding the prompt fixed?

Condition A = raw first N chars (current behaviour)
Condition B = boilerplate-stripped window

Same prompt (classify_v2), same model, same batch size, same postings, same order,
so the only variable is the input window. B is persisted under prompt_version
"v2+strip"; A is not persisted (v2 already holds raw-window labels).
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

import click
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telltale.config import Settings
from telltale.llm.client import ClassificationClient
from telltale.storage.models import JobPosting
from telltale.storage.repository import insert_classifications
from telltale.storage.session import checkpoint_wal, get_session

FOCUS = ("Payments Infrastructure", "Engineering - Platform", "Engineering - Mobile")


def _stratified(postings: list[JobPosting], n: int) -> list[JobPosting]:
    by_co: dict[str, list[JobPosting]] = defaultdict(list)
    for p in postings:
        by_co[p.company_slug].append(p)
    for v in by_co.values():
        v.sort(key=lambda p: p.id)
    buckets = {k: iter(v) for k, v in by_co.items()}
    picked: list[JobPosting] = []
    while len(picked) < n and buckets:
        for co in list(buckets):
            if len(picked) >= n:
                break
            try:
                picked.append(next(buckets[co]))
            except StopIteration:
                del buckets[co]
    picked.sort(key=lambda p: p.id)
    return picked


def _run(postings, strip: bool, version: str):
    client = ClassificationClient(
        settings=Settings(), strip_boilerplate=strip, rate_limit_retries=4
    )
    results = client.classify_batch(postings, version)
    tokens = getattr(client.backend, "tokens_used", 0)
    return {r.posting_id: r for r in results}, tokens, client


@click.command()
@click.option("--n", type=int, default=50)
@click.option("--prompt-version", default="v2")
@click.option("--store-as", default="v2+strip")
def main(n: int, prompt_version: str, store_as: str) -> None:
    session = get_session()
    postings = list(
        session.execute(
            select(JobPosting).where(JobPosting.is_open.is_(True))
        ).scalars().all()
    )
    sample = _stratified(postings, n)
    # Plain snapshot: ORM instances detach when the session closes, and the
    # analysis below runs after that.
    by_id = {p.id: {"company": p.company_slug, "title": p.title} for p in sample}
    click.echo(f"sample: {len(sample)} postings")
    for co, c in Counter(p.company_slug for p in sample).most_common():
        click.echo(f"  {co:<14}{c}")

    click.echo("\n--- condition A: raw window ---")
    a, tok_a, _ = _run(sample, strip=False, version=prompt_version)
    click.echo(f"A: {len(a)} labels, {tok_a:,} tokens")

    click.echo("\n--- condition B: stripped window ---")
    b, tok_b, client_b = _run(sample, strip=True, version=prompt_version)
    click.echo(f"B: {len(b)} labels, {tok_b:,} tokens")

    if b:
        insert_classifications(session, list(b.values()), store_as)
        session.commit()
        click.echo(f"stored {len(b)} rows as prompt_version={store_as!r}")
    session.close()
    checkpoint_wal()

    common = sorted(set(a) & set(b))
    if not common:
        raise SystemExit("no overlap between conditions")

    cat_diff = [i for i in common if a[i].function_category != b[i].function_category]
    sen_diff = [i for i in common if a[i].seniority_level != b[i].seniority_level]
    click.echo("\n" + "=" * 72)
    click.echo(f"compared {len(common)} postings")
    click.echo(
        f"category differs : {len(cat_diff):>3}/{len(common)} "
        f"({len(cat_diff)/len(common):.1%})"
    )
    click.echo(
        f"seniority differs: {len(sen_diff):>3}/{len(common)} "
        f"({len(sen_diff)/len(common):.1%})"
    )

    moves = Counter(
        (a[i].function_category, b[i].function_category) for i in cat_diff
    )
    if moves:
        click.echo("\ncategory moves (A raw -> B stripped):")
        for (x, y), c in moves.most_common():
            click.echo(f"  {c:>3}x  {x:<26} -> {y}")

    click.echo("\n" + "=" * 72)
    click.echo("PAYMENTS INFRASTRUCTURE (A raw -> B stripped)")
    click.echo("=" * 72)
    click.echo(f"{'company':<14}" + "".join(f"{c[:20]:>23}" for c in FOCUS))
    for co in sorted({by_id[i]["company"] for i in common}):
        ids = [i for i in common if by_id[i]["company"] == co]
        line = f"{co:<14}"
        for cat in FOCUS:
            x = sum(1 for i in ids if a[i].function_category == cat)
            y = sum(1 for i in ids if b[i].function_category == cat)
            line += f"{f'{x} -> {y}':>23}"
        click.echo(line)
    line = f"{'ALL':<14}"
    for cat in FOCUS:
        x = sum(1 for i in common if a[i].function_category == cat)
        y = sum(1 for i in common if b[i].function_category == cat)
        line += f"{f'{x} -> {y}':>23}"
    click.echo(line)

    total = tok_a + tok_b
    click.echo("\n" + "=" * 72)
    click.echo(f"tokens: A {tok_a:,} + B {tok_b:,} = {total:,}")
    per_posting_b = tok_b / max(len(sample), 1)
    click.echo(
        f"condition B cost per posting: {per_posting_b:.0f} tokens  "
        f"-> full 367-posting run ≈ {per_posting_b*367:,.0f} tokens"
    )


if __name__ == "__main__":
    main()
