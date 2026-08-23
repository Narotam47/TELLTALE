from __future__ import annotations

import click
import structlog

from telltale.logging import setup_logging


@click.group()
def cli() -> None:
    """TELLTALE - Competitive intelligence from fintech hiring signals."""
    setup_logging()


@cli.command()
@click.option("--company", "company_slug", default=None, help="Scrape only this company slug.")
@click.option("--dry-run", is_flag=True, help="Fetch and normalize without writing to DB.")
def scrape(company_slug: str | None, dry_run: bool) -> None:
    """Scrape careers pages for all enabled companies."""
    log = structlog.get_logger()
    slugs = [company_slug] if company_slug else None

    if dry_run:
        from telltale.pipeline.ingest import dry_run as do_dry_run

        log.info("scrape.dry_run.start")
        results = do_dry_run(company_slugs=slugs)
        click.echo(f"\n{'Slug':<16} {'Adapter':<14} {'Found':>6}  Status")
        click.echo("-" * 56)
        for r in results:
            click.echo(f"{r['slug']:<16} {r['adapter']:<14} {r['found']:>6}  {r['status']}")
        click.echo()
    else:
        from telltale.pipeline.ingest import ingest_all

        log.info("scrape.start")

        from telltale.storage.models import Base
        from telltale.storage.session import engine

        Base.metadata.create_all(engine)

        summary = ingest_all(company_slugs=slugs)
        click.echo(f"\n{'Slug':<16} {'Status':<10} {'Found':>6} {'New':>5} {'Upd':>5} {'Closed':>7}  Error")
        click.echo("-" * 76)
        for r in summary.results:
            err = r.error or ""
            if len(err) > 30:
                err = err[:27] + "..."
            click.echo(
                f"{r.slug:<16} {r.status:<10} {r.found:>6} {r.new:>5} {r.updated:>5} {r.closed:>7}  {err}"
            )
        click.echo(f"\nTotal errors: {summary.total_errors}")


@cli.command()
@click.option("--limit", type=int, default=None, help="Classify at most N postings.")
@click.option("--prompt-version", default="v1", help="Prompt version to use and record.")
def classify(limit: int | None, prompt_version: str) -> None:
    """Classify unclassified postings with the configured LLM provider."""
    from telltale.pipeline.classify import classify_pending

    summary = classify_pending(limit=limit, prompt_version=prompt_version)

    click.echo(f"\nPrompt version : {prompt_version}")
    click.echo(f"Provider       : {summary.provider or '-'}")
    click.echo(f"Pending        : {summary.pending}")
    click.echo(f"Classified     : {summary.classified}")
    click.echo(f"Skipped        : {summary.skipped}\n")


@cli.command()
@click.option("--models", is_flag=True, help="List every model each account can access.")
@click.option(
    "--deep",
    is_flag=True,
    help="Send a batch-sized probe so a spent daily token budget is detected.",
)
def doctor(models: bool, deep: bool) -> None:
    """Report which LLM providers are reachable, usable, and in quota."""
    from telltale.config import Settings
    from telltale.llm.doctor import active_provider, run_all

    settings = Settings()
    current = active_provider(settings)
    results = run_all(settings, deep=deep)

    click.echo(f"\nLLM_PROVIDER = {current}\n")
    click.echo(f"{'provider':<10}{'status':<18}{'default model':<26}{'models':>7}")
    click.echo("-" * 62)
    for h in results:
        marker = "*" if h.provider == current else " "
        avail = {True: "yes", False: "NO", None: "?"}[h.default_model_available]
        click.echo(
            f"{marker}{h.provider:<9}{h.status:<18}{h.default_model[:22]:<22}"
            f"{'[' + avail + ']':>4}{len(h.models):>7}"
        )

    for h in results:
        detail = []
        if h.error:
            detail.append(f"error : {h.error}")
        if h.note:
            detail.append(f"hint  : {h.note}")
        for k, v in h.quota.items():
            detail.append(f"quota : {k} = {v}")
        if h.default_model_available is False:
            detail.append(
                f"hint  : {h.default_model!r} not available to this account; "
                f"pick one of the {len(h.models)} listed with --models"
            )
        if detail:
            click.echo(f"\n[{h.provider}]")
            for line in detail:
                click.echo(f"  {line}")

    if models:
        for h in results:
            if h.models:
                click.echo(f"\n[{h.provider}] {len(h.models)} models:")
                for m in h.models:
                    click.echo(f"  {m}")

    active = next((h for h in results if h.provider == current), None)
    click.echo()
    if active and active.status == "ok":
        click.echo(f"Active provider {current!s} is ready.")
    else:
        reason = active.status if active else "unknown"
        click.echo(f"Active provider {current!s} is NOT ready ({reason}).")
        healthy = [h.provider for h in results if h.status == "ok"]
        if healthy:
            click.echo(f"Reachable alternatives: {', '.join(healthy)}")
        raise SystemExit(1)


@cli.command()
@click.option("--prompt-version", default="v1", help="Classification version to read.")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw signal JSON.")
def signals(prompt_version: str, as_json: bool) -> None:
    """Compute this week's hiring signals."""
    import json as _json

    from telltale.pipeline.signals import build_signal_report

    report = build_signal_report(prompt_version=prompt_version)
    if as_json:
        click.echo(_json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return

    d = report.to_dict()
    click.echo(f"\nWeek {d['week_start']} to {d['week_end']}  (prompt {prompt_version})")
    click.echo(
        f"{d['totals']['open_postings']} open postings across "
        f"{d['totals']['companies_tracked']} companies"
    )
    if report.is_cold_start:
        click.echo("COLD START - no prior week, comparative signals unavailable")

    click.echo(f"\n{'function':<38}{'open':>6}{'share':>8}{'net':>6}")
    click.echo("-" * 58)
    for s in d["sector_wide"]:
        click.echo(
            f"{s['function']:<38}{s['open_now']:>6}{s['share_of_sector_pct']:>7}%"
            f"{s['net']:>6}"
        )

    click.echo("\nLargest function share per company:")
    for co in d["companies"]:
        mix = d["function_mix_by_company"][co]
        if not mix:
            continue
        top = next(iter(mix.items()))
        click.echo(f"  {co:<14}{top[0]:<38}{top[1]['share_pct']:>5}%")

    if report.fastest_growing_function:
        click.echo(f"\nFastest growing: {report.fastest_growing_function}")
    for n in d["notes"]:
        click.echo(f"note: {n}")
    click.echo()


@cli.command()
@click.option("--prompt-version", default="v1", help="Classification version to read.")
@click.option("--dry-run", is_flag=True, help="Print the brief without saving it.")
@click.option(
    "--snapshot",
    is_flag=True,
    help="Render figures only, with no LLM. Used when no provider key is set.",
)
@click.option("--reason", default="classification was skipped", help="Snapshot reason.")
def brief(prompt_version: str, dry_run: bool, snapshot: bool, reason: str) -> None:
    """Generate the weekly intelligence brief."""
    from telltale.pipeline.brief import (
        generate_brief,
        render_snapshot_brief,
        save_brief,
    )
    from telltale.pipeline.signals import build_signal_report

    report = build_signal_report(prompt_version=prompt_version)

    if snapshot:
        markdown = render_snapshot_brief(report, reason=reason)
        click.echo()
        click.echo(markdown)
        if not dry_run:
            click.echo(f"saved: {save_brief(markdown, report)}")
        click.echo("model: none (snapshot template)")
        return

    result = generate_brief(report, persist=not dry_run)
    click.echo()
    click.echo(result.markdown)
    click.echo()
    if result.path:
        click.echo(f"saved: {result.path}")
    click.echo(f"model: {result.model_name}")
    for w in result.warnings:
        click.echo(f"WARNING: {w}")


@cli.command("run")
@click.option("--prompt-version", default="v1", help="Classification version to use.")
@click.option("--skip-scrape", is_flag=True, help="Use the postings already stored.")
@click.option("--skip-classify", is_flag=True, help="Use the classifications already stored.")
def run_all(prompt_version: str, skip_scrape: bool, skip_classify: bool) -> None:
    """Run the full pipeline: scrape, classify, signals, brief."""
    log = structlog.get_logger()
    from telltale.pipeline.brief import generate_brief
    from telltale.pipeline.classify import classify_pending
    from telltale.pipeline.ingest import ingest_all
    from telltale.pipeline.signals import build_signal_report
    from telltale.storage.models import Base
    from telltale.storage.session import engine

    Base.metadata.create_all(engine)

    if skip_scrape:
        click.echo("[1/4] scrape   skipped")
    else:
        click.echo("[1/4] scrape")
        summary = ingest_all()
        ok = sum(1 for r in summary.results if r.status in ("success", "suspect"))
        found = sum(r.found for r in summary.results)
        click.echo(
            f"      {found} postings from {ok}/{len(summary.results)} sources, "
            f"{summary.total_errors} errors"
        )

    if skip_classify:
        click.echo("[2/4] classify skipped")
    else:
        click.echo("[2/4] classify")
        cs = classify_pending(prompt_version=prompt_version)
        click.echo(
            f"      {cs.classified} classified, {cs.skipped} skipped "
            f"({cs.provider or 'n/a'})"
        )

    click.echo("[3/4] signals")
    report = build_signal_report(prompt_version=prompt_version)
    click.echo(
        f"      {report.total_open} open across {len(report.companies)} companies"
        + ("  [cold start]" if report.is_cold_start else "")
    )

    click.echo("[4/4] brief")
    result = generate_brief(report)
    click.echo(f"      saved: {result.path}")
    for w in result.warnings:
        click.echo(f"      WARNING: {w}")
    log.info("run.done")


if __name__ == "__main__":
    cli()
