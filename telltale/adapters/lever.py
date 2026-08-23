from __future__ import annotations

from typing import Any, ClassVar

from telltale.adapters.base import SourceAdapter
from telltale.adapters.http import fetch_url
from telltale.config import Company
from telltale.storage.repository import compute_content_hash


class LeverError(Exception):
    """Raised when Lever returns an error response (e.g. unknown slug)."""


class LeverAdapter(SourceAdapter):
    adapter_type: ClassVar[str] = "lever"

    def __init__(self, company: Company) -> None:
        super().__init__(company)
        if not company.endpoint_url:
            raise ValueError(f"endpoint_url required for Lever adapter ({company.slug})")
        self.api_url = company.endpoint_url.rstrip("/")

    def fetch(self) -> list[dict[str, Any]]:
        sep = "&" if "?" in self.api_url else "?"
        url = f"{self.api_url}{sep}mode=json"
        resp = fetch_url(url, check_robots=False)
        data = resp.json()
        if isinstance(data, dict):
            error = data.get("error", data.get("message", "unknown error"))
            raise LeverError(
                f"Lever API error for {self.company.slug}: {error}"
            )
        if not isinstance(data, list):
            raise LeverError(
                f"Unexpected Lever response type for {self.company.slug}: {type(data).__name__}"
            )
        return data

    def normalize(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        postings = []
        for job in raw:
            title = job.get("text", "")
            categories = job.get("categories", {})
            description_text = job.get("descriptionPlain")
            if not description_text:
                desc_parts = []
                for lst in job.get("lists", []):
                    desc_parts.append(lst.get("text", ""))
                    desc_parts.append(lst.get("content", ""))
                if job.get("additionalPlain"):
                    desc_parts.append(job["additionalPlain"])
                description_text = "\n".join(p for p in desc_parts if p) or None

            postings.append({
                "external_id": str(job["id"]),
                "title": title,
                "raw_department": categories.get("team"),
                "location": categories.get("location"),
                "employment_type": categories.get("commitment"),
                "description_text": description_text,
                "url": job.get("hostedUrl"),
                "content_hash": compute_content_hash(title, description_text),
            })
        return postings
