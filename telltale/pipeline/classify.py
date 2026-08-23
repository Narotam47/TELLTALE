from __future__ import annotations

from dataclasses import dataclass

import structlog

from telltale.llm.client import VERSION_PROFILES, ClassificationClient, base_version
from telltale.llm.usage import record as record_usage
from telltale.storage.repository import get_unclassified, insert_classifications
from telltale.storage.session import checkpoint_wal, get_session

log = structlog.get_logger()


@dataclass
class ClassifySummary:
    pending: int = 0
    classified: int = 0
    skipped: int = 0
    provider: str = ""


def classify_pending(
    limit: int | None = None,
    prompt_version: str = "v1",
    client: ClassificationClient | None = None,
) -> ClassifySummary:
    """Classify open postings lacking a classification at prompt_version."""
    summary = ClassifySummary()
    session = get_session()
    try:
        pending = get_unclassified(session, prompt_version)
        if limit is not None:
            pending = pending[:limit]
        summary.pending = len(pending)

        if not pending:
            log.info("classify.nothing_pending", prompt_version=prompt_version)
            return summary

        if client is None:
            profile = VERSION_PROFILES.get(
                prompt_version, VERSION_PROFILES.get(base_version(prompt_version), {})
            )
            client = ClassificationClient(**profile)
            log.info("classify.profile", prompt_version=prompt_version, **profile)
        log.info(
            "classify.start",
            pending=len(pending),
            prompt_version=prompt_version,
            provider=client.backend.provider,
        )

        spent_so_far = 0

        def _persist(batch_results):
            # Record tokens per batch for the same reason rows commit per batch:
            # a killed or quota-capped run must not lose its accounting, or the
            # ledger under-reports and doctor gives a false green next time.
            nonlocal spent_so_far
            insert_classifications(session, batch_results, prompt_version)
            session.commit()
            total = getattr(client.backend, "tokens_used", 0)
            delta = total - spent_so_far
            if delta > 0:
                record_usage(client.backend.provider, delta)
                spent_so_far = total

        results = client.classify_batch(pending, prompt_version, on_batch=_persist)
        # Anything the per-batch hook missed (a final partial batch).
        remainder = getattr(client.backend, "tokens_used", 0) - spent_so_far
        if remainder > 0:
            record_usage(client.backend.provider, remainder)
        log.info(
            "classify.tokens",
            spent=getattr(client.backend, "tokens_used", 0),
            provider=client.backend.provider,
        )
        summary.provider = client.backend.provider
        summary.classified = len(results)
        summary.skipped = summary.pending - summary.classified

        log.info(
            "classify.done",
            classified=summary.classified,
            skipped=summary.skipped,
            provider=summary.provider,
        )
    except Exception:
        session.rollback()
        log.exception("classify.failed")
        raise
    finally:
        session.close()
        checkpoint_wal()

    return summary
