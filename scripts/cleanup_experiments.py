#!/usr/bin/env python3
"""Delete experimental classification rows, keeping only the shipping dataset.

v2, v2-gemini and v2+strip were comparison artifacts for the taxonomy and window
investigations (see docs/taxonomy.md). They are partial, sit on the superseded
13-category taxonomy, and would silently mix taxonomies if anything read them.

Only `posting_classifications` rows are touched. Postings, scrape runs, companies
and briefs are left alone.

    uv run python scripts/cleanup_experiments.py --dry-run
    uv run python scripts/cleanup_experiments.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEEP = "v1"


@click.command()
@click.option("--db", default="telltale.db", help="SQLite database path.")
@click.option("--keep", default=KEEP, help="prompt_version to preserve.")
@click.option("--dry-run", is_flag=True, help="Report what would go, delete nothing.")
def main(db: str, keep: str, dry_run: bool) -> None:
    path = Path(db)
    if not path.exists():
        raise click.ClickException(f"No database at {path}")

    con = sqlite3.connect(path)
    try:
        before = con.execute(
            "select prompt_version, model_name, count(*) "
            "from posting_classifications group by 1, 2 order by 1"
        ).fetchall()

        click.echo("Current classification rows:")
        for version, model, n in before:
            marker = "KEEP  " if version == keep else "DELETE"
            click.echo(f"  {marker}  {version:<12}{model:<30}{n:>5}")

        doomed = sum(n for v, _, n in before if v != keep)
        kept = sum(n for v, _, n in before if v == keep)

        if kept == 0:
            raise click.ClickException(
                f"Refusing to run: no rows at prompt_version={keep!r}, so this would "
                "empty the table. Check --keep."
            )
        if doomed == 0:
            click.echo("\nNothing to delete.")
            return

        # Counts that must not change: this only removes classifications.
        untouched = {
            t: con.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("job_postings", "scrape_runs", "companies", "weekly_briefs")
        }

        if dry_run:
            click.echo(f"\nDRY RUN: would delete {doomed} rows, keep {kept}.")
            return

        cur = con.execute(
            "delete from posting_classifications where prompt_version != ?", (keep,)
        )
        con.commit()
        click.echo(f"\nDeleted {cur.rowcount} rows.")

        after = con.execute(
            "select prompt_version, count(*) from posting_classifications group by 1"
        ).fetchall()
        click.echo("Remaining:")
        for version, n in after:
            click.echo(f"  {version:<12}{n:>5}")

        for table, expected in untouched.items():
            actual = con.execute(f"select count(*) from {table}").fetchone()[0]
            status = "ok" if actual == expected else "CHANGED"
            click.echo(f"  {table:<20}{actual:>5}  ({status})")
            if actual != expected:
                raise click.ClickException(
                    f"{table} changed from {expected} to {actual} - this script must "
                    "only touch posting_classifications"
                )
    finally:
        con.close()

    from telltale.storage.session import checkpoint_wal

    checkpoint_wal()
    click.echo("\nWAL checkpointed; the .db file is self-contained.")


if __name__ == "__main__":
    main()
