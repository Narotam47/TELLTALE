from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog

log = structlog.get_logger()

_USER_AGENT = "TELLTALE/0.1 (competitive-intelligence; +https://github.com/telltale-ci)"
_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json",
}
_MAX_RETRIES = 3
_MIN_DOMAIN_INTERVAL = 2.0

_domain_last_request: dict[str, float] = {}
_robots_cache: dict[str, RobotFileParser | None] = {}


def _rate_limit(domain: str) -> None:
    now = time.monotonic()
    last = _domain_last_request.get(domain, 0.0)
    wait = _MIN_DOMAIN_INTERVAL - (now - last)
    if wait > 0:
        time.sleep(wait)
    _domain_last_request[domain] = time.monotonic()


def _check_robots(url: str) -> bool:
    """Return True if URL is allowed by robots.txt. Caches per domain."""
    parsed = urlparse(url)
    domain = parsed.netloc
    if domain not in _robots_cache:
        robots_url = f"{parsed.scheme}://{domain}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
            _robots_cache[domain] = rp
        except Exception:
            _robots_cache[domain] = None
    rp = _robots_cache[domain]
    if rp is None:
        return True
    return rp.can_fetch(_USER_AGENT, url)


def _is_ats_api(url: str) -> bool:
    """ATS JSON APIs are public APIs, exempt from robots.txt checks."""
    host = urlparse(url).netloc
    return any(
        api_host in host
        for api_host in (
            "boards-api.greenhouse.io",
            "boards.eu.greenhouse.io",
            "api.lever.co",
            "keka.com/careers/api",
        )
    ) or "keka.com" in host and "/api/" in url


def fetch_url(
    url: str,
    *,
    client: httpx.Client | None = None,
    check_robots: bool = True,
    cache_dir: Path | None = None,
    cache_key: str | None = None,
) -> httpx.Response:
    """Fetch a URL with retry, rate limiting, and optional robots.txt check."""
    if check_robots and not _is_ats_api(url) and not _check_robots(url):
        log.warning("robots_disallowed", url=url)
        raise PermissionError(f"robots.txt disallows fetching {url}")

    domain = urlparse(url).netloc
    own_client = client is None
    if own_client:
        client = httpx.Client(headers=_DEFAULT_HEADERS, follow_redirects=True, timeout=30)

    try:
        for attempt in range(1, _MAX_RETRIES + 1):
            _rate_limit(domain)
            try:
                resp = client.get(url)
                if resp.status_code in (429, 500, 502, 503, 504):
                    backoff = 2 ** attempt
                    log.warning(
                        "http_retry",
                        url=url,
                        status=resp.status_code,
                        attempt=attempt,
                        backoff=backoff,
                    )
                    if attempt < _MAX_RETRIES:
                        time.sleep(backoff)
                        continue
                resp.raise_for_status()

                if cache_dir and cache_key:
                    day_dir = cache_dir / cache_key / datetime.now(UTC).date().isoformat()
                    day_dir.mkdir(parents=True, exist_ok=True)
                    suffix = ".json" if "json" in resp.headers.get("content-type", "") else ".html"
                    (day_dir / f"response{suffix}").write_bytes(resp.content)

                return resp
            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES:
                    backoff = 2 ** attempt
                    log.warning("http_timeout", url=url, attempt=attempt, backoff=backoff)
                    time.sleep(backoff)
                    continue
                raise
    finally:
        if own_client:
            client.close()

    raise RuntimeError("unreachable")
