from __future__ import annotations

import html as html_mod
import re
from typing import Any, ClassVar

import structlog

from telltale.adapters.base import SourceAdapter
from telltale.adapters.http import fetch_url
from telltale.config import Company
from telltale.storage.repository import compute_content_hash


def _strip_tags(text: str) -> str:
    """Unescape HTML entities and strip tags to plain text."""
    text = html_mod.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class GreenhouseAdapter(SourceAdapter):
    adapter_type: ClassVar[str] = "greenhouse"

    def __init__(self, company: Company) -> None:
        super().__init__(company)
        if not company.endpoint_url:
            raise ValueError(f"endpoint_url required for Greenhouse adapter ({company.slug})")
        self.api_url = company.endpoint_url.rstrip("/")

    def fetch(self) -> list[dict[str, Any]]:
        log = structlog.get_logger()
        sep = "&" if "?" in self.api_url else "?"
        url = f"{self.api_url}{sep}content=true"
        resp = fetch_url(url, check_robots=False)
        data = resp.json()
        jobs = data.get("jobs", [])
        meta_total = (data.get("meta") or {}).get("total")
        if meta_total is not None and meta_total != len(jobs):
            log.warning(
                "greenhouse.partial_response",
                company=self.company.slug,
                meta_total=meta_total,
                returned=len(jobs),
            )
        return jobs

    def normalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        postings = []
        for job in raw:
            title = job.get("title", "")
            desc_html = job.get("content", "")
            description_text = _strip_tags(desc_html) if desc_html else None
            departments = job.get("departments", [])
            raw_department = departments[0]["name"] if departments else None
            location = job.get("location", {}).get("name")

            postings.append({
                "external_id": str(job["id"]),
                "title": title,
                "raw_department": raw_department,
                "location": location,
                "description_text": description_text,
                "url": job.get("absolute_url"),
                "content_hash": compute_content_hash(title, description_text),
            })
        return postings
