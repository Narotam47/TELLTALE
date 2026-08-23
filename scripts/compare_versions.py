#!/usr/bin/env python3
"""Compare two classification prompt versions stored side by side.

    uv run python scripts/compare_versions.py --old v1 --new v2
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load(con: sqlite3.Connection, version: str) -> dict[int, tuple[str, str, str]]:
    """posting_id -> (company, category, seniority), latest row per posting."""
    rows = con.execute(
        """
        select c.posting_id, p.company_slug, c.function_category, c.seniority_level
        from posting_classifications c
        join job_postings p on p.id = c.posting_id
        where c.prompt_version = ?
        order by c.id
        """,
        (version,),
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


@click.command()
@click.option("--db", default="telltale.db")
@click.option("--old", default="v1")
@click.option("--new", default="v2")
def main(db: str, old: str, new: str) -> None:
    con = sqlite3.connect(db)
    a, b = _load(con, old), _load(con, new)
    con.close()

    print(f"{old}: {len(a)} classifications    {new}: {len(b)} classifications")
    common = sorted(set(a) & set(b))
    print(f"comparable (present in both): {len(common)}\n")
    if not common:
        raise SystemExit("No overlap between the two versions.")

    # ---- category distribution side by side ----
    ca = Counter(a[i][1] for i in common)
    cb = Counter(b[i][1] for i in common)
    n = len(common)
    print("=" * 74)
    print("FUNCTION CATEGORY — distribution")
    print("=" * 74)
    print(f"{'category':<26}{old:>7}{'%':>7}{new:>7}{'%':>7}{'delta':>8}")
    print("-" * 74)
    for cat in sorted(set(ca) | set(cb), key=lambda k: -cb.get(k, 0)):
        x, y = ca.get(cat, 0), cb.get(cat, 0)
        print(f"{cat:<26}{x:>7}{x/n:>7.1%}{y:>7}{y/n:>7.1%}{y-x:>+8}")
    print(f"{'TOTAL':<26}{n:>7}{'':>7}{n:>7}")

    # ---- seniority distribution ----
    sa = Counter(a[i][2] for i in common)
    sb = Counter(b[i][2] for i in common)
    print("\n" + "=" * 74)
    print("SENIORITY — distribution")
    print("=" * 74)
    print(f"{'level':<26}{old:>7}{'%':>7}{new:>7}{'%':>7}{'delta':>8}")
    print("-" * 74)
    for lvl in sorted(set(sa) | set(sb), key=lambda k: -sb.get(k, 0)):
        x, y = sa.get(lvl, 0), sb.get(lvl, 0)
        print(f"{lvl:<26}{x:>7}{x/n:>7.1%}{y:>7}{y/n:>7.1%}{y-x:>+8}")

    # ---- churn ----
    cat_moves = Counter((a[i][1], b[i][1]) for i in common if a[i][1] != b[i][1])
    sen_moves = Counter((a[i][2], b[i][2]) for i in common if a[i][2] != b[i][2])
    changed = sum(cat_moves.values())
    print("\n" + "=" * 74)
    print(f"CATEGORY CHANGES: {changed}/{n} ({changed/n:.1%}) postings relabelled")
    print("=" * 74)
    for (x, y), count in cat_moves.most_common():
        print(f"  {count:>4}x  {x:<26} -> {y}")
    if not cat_moves:
        print("  (none)")

    sc = sum(sen_moves.values())
    print(f"\nSENIORITY CHANGES: {sc}/{n} ({sc/n:.1%})")
    for (x, y), count in sen_moves.most_common(12):
        print(f"  {count:>4}x  {x:<26} -> {y}")

    # ---- payments infrastructure focus ----
    print("\n" + "=" * 74)
    print("PAYMENTS INFRASTRUCTURE — the categories under test")
    print("=" * 74)
    focus = ("Payments Infrastructure", "Engineering - Platform", "Engineering - Mobile")
    print(f"{'company':<14}" + "".join(f"{c[:22]:>24}" for c in focus))
    print("-" * 86)
    companies = sorted({a[i][0] for i in common})
    for co in companies:
        ids = [i for i in common if a[i][0] == co]
        line = f"{co:<14}"
        for cat in focus:
            x = sum(1 for i in ids if a[i][1] == cat)
            y = sum(1 for i in ids if b[i][1] == cat)
            line += f"{f'{x} -> {y}':>24}"
        print(line)
    line = f"{'ALL':<14}"
    for cat in focus:
        x = sum(1 for i in common if a[i][1] == cat)
        y = sum(1 for i in common if b[i][1] == cat)
        line += f"{f'{x} -> {y}':>24}"
    print(line)


if __name__ == "__main__":
    main()
