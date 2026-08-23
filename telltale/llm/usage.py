from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger()

LEDGER_PATH = Path("data/usage.json")

# Free-tier daily token ceilings, by provider. Providers do not expose remaining
# daily budget in headers, so the only way to answer "will a full run fit?" before
# starting one is to track spend locally and subtract.
# Known daily token ceilings. Gemini's free tier binds on requests-per-minute
# rather than a published daily token cap, so it is deliberately absent: a wrong
# ceiling is worse than an honest "unknown".
DAILY_LIMITS: dict[str, int] = {"groq": 200_000}


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("usage.ledger_unreadable", path=str(path))
        return {}


def record(provider: str, tokens: int, path: Path = LEDGER_PATH) -> int:
    """Add tokens to today's tally for a provider. Returns the new total."""
    if tokens <= 0:
        return spent_today(provider, path)
    data = _load(path)
    day = data.setdefault(_today(), {})
    day[provider] = day.get(provider, 0) + tokens
    # Keep the file small: only the last 14 days matter.
    for old in sorted(data)[:-14]:
        del data[old]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
    return day[provider]


def spent_today(provider: str, path: Path = LEDGER_PATH) -> int:
    return _load(path).get(_today(), {}).get(provider, 0)


def remaining_today(provider: str, path: Path = LEDGER_PATH) -> int | None:
    """Tokens left against the known daily ceiling, or None if no ceiling is known.

    This is a local estimate: it only counts spend this tool recorded, so anything
    spent by other clients on the same key is invisible.
    """
    limit = DAILY_LIMITS.get(provider)
    if limit is None:
        return None
    return max(limit - spent_today(provider, path), 0)
