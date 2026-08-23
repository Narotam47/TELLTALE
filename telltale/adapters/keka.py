from __future__ import annotations

from typing import Any, ClassVar

from bs4 import BeautifulSoup

from telltale.adapters.base import SourceAdapter
from telltale.adapters.http import fetch_url
from telltale.config import Company
from telltale.storage.repository import compute_content_hash


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(separator="\n", strip=True) or None


class KekaAdapter(SourceAdapter):
    adapter_type: ClassVar[str] = "keka"

    def __init__(self, company: Company) -> None:
        super().__init__(company)
        if not company.endpoint_url:
            raise ValueError(f"endpoint_url required for Keka adapter ({company.slug})")
        self.base_url = company.endpoint_url.rstrip("/")
        self.identifier = company.keka_identifier
        if not self.identifier:
            raise ValueError(f"keka_identifier required for Keka adapter ({company.slug})")

    def fetch(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/embedjobs/default/active/{self.identifier}"
        resp = fetch_url(url, check_robots=False)
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"Expected list from Keka API, got {type(data).__name__}")
        return data

    def normalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        postings = []
        for job in raw:
            title = job.get("title", "")
            desc_html = job.get("description", "")
            description_text = _strip_html(desc_html)

            locations = job.get("jobLocations", [])
            if locations:
                loc = locations[0]
                parts = [p for p in (loc.get("city"), loc.get("countryName")) if p]
                location = ", ".join(parts) or loc.get("name")
            else:
                location = None

            postings.append({
                "external_id": str(job["id"]),
                "title": title,
                "raw_department": job.get("departmentName"),
                "location": location,
                "description_text": description_text,
                "content_hash": compute_content_hash(title, description_text),
            })
        return postings
