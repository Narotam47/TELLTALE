from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

import structlog
from bs4 import BeautifulSoup

from telltale.adapters.base import SourceAdapter
from telltale.adapters.http import fetch_url
from telltale.config import Company
from telltale.storage.repository import compute_content_hash

log = structlog.get_logger()

_JOB_ID_RE = re.compile(r"jobDetails/([a-f0-9]+)")
_POST_ID_RE = re.compile(r"^post-(\d+)$")
_DEPT_RE = re.compile(r"^job-department-(.+)$")
_LOC_RE = re.compile(r"^job-location-(.+)$")


def _slug_to_label(slug: str) -> str:
    return slug.replace("-", " ").title()


class DarwinboxWordPressAdapter(SourceAdapter):
    """Lendingkart: an Elementor careers loop on WordPress that links out to Darwinbox.

    The Darwinbox job pages are an Angular SPA behind Cloudflare Turnstile and carry
    no description in their HTML, so the description is taken from the WordPress post
    that backs each loop item instead. Department and location come from the taxonomy
    classes Elementor stamps on the loop wrapper.
    """

    adapter_type: ClassVar[str] = "darwinbox"

    def __init__(self, company: Company) -> None:
        super().__init__(company)
        if not company.careers_url:
            raise ValueError(
                f"careers_url required for Darwinbox WP adapter ({company.slug})"
            )

    def _fetch_description(self, post_url: str) -> str | None:
        try:
            resp = fetch_url(post_url)
        except Exception:
            log.warning("darwinbox.post_fetch_failed", url=post_url)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        node = soup.find("div", class_="elementor-widget-theme-post-content")
        if node is None:
            log.warning("darwinbox.no_post_content", url=post_url)
            return None

        text = node.get_text("\n", strip=True)
        return text or None

    def fetch(self) -> list[dict[str, Any]]:
        resp = fetch_url(self.company.careers_url)
        soup = BeautifulSoup(resp.text, "html.parser")
        base = f"{urlparse(self.company.careers_url).scheme}://{urlparse(self.company.careers_url).netloc}"

        items = soup.find_all("div", class_="e-loop-item")
        seen: dict[str, dict[str, Any]] = {}

        for item in items:
            anchor = item.find("a", href=_JOB_ID_RE)
            if anchor is None:
                continue
            match = _JOB_ID_RE.search(anchor["href"])
            if not match:
                continue
            job_id = match.group(1)
            if job_id in seen:
                continue

            heading = item.find(["h1", "h2", "h3"])
            title = heading.get_text(strip=True) if heading else ""
            if not title:
                log.warning("darwinbox.missing_title", job_id=job_id)
                continue

            classes = item.get("class", [])
            department = location = None
            post_id = None
            for cls in classes:
                if m := _DEPT_RE.match(cls):
                    department = _slug_to_label(m.group(1))
                elif m := _LOC_RE.match(cls):
                    location = _slug_to_label(m.group(1))
                elif m := _POST_ID_RE.match(cls):
                    post_id = m.group(1)

            description = None
            if post_id:
                description = self._fetch_description(urljoin(base, f"/?p={post_id}"))

            seen[job_id] = {
                "job_id": job_id,
                "title": title,
                "url": anchor["href"].split("?")[0],
                "raw_department": department,
                "location": location,
                "description_text": description,
            }

        return list(seen.values())

    def normalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        postings = []
        for job in raw:
            title = job.get("title", "")
            description_text = job.get("description_text")
            postings.append({
                "external_id": job["job_id"],
                "title": title,
                "raw_department": job.get("raw_department"),
                "location": job.get("location"),
                "description_text": description_text,
                "url": job.get("url"),
                "content_hash": compute_content_hash(title, description_text),
            })
        return postings
