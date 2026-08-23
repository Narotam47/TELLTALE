from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from telltale.adapters import ADAPTER_REGISTRY, get_adapter
from telltale.adapters.darwinbox_wp import DarwinboxWordPressAdapter
from telltale.adapters.greenhouse import GreenhouseAdapter
from telltale.adapters.keka import KekaAdapter
from telltale.adapters.lever import LeverAdapter, LeverError
from telltale.config import Company

FIXTURES = Path(__file__).parent / "fixtures"


def _make_company(**overrides) -> Company:
    defaults = {
        "name": "Test",
        "slug": "test",
        "careers_url": "https://example.com/careers",
        "adapter_type": "greenhouse",
    }
    defaults.update(overrides)
    return Company(**defaults)


def _mock_response(fixture_path: Path, content_type: str = "application/json") -> httpx.Response:
    content = fixture_path.read_bytes()
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://example.com"),
    )


class TestRegistry:
    def test_all_adapters_registered(self):
        assert set(ADAPTER_REGISTRY.keys()) == {"greenhouse", "lever", "darwinbox", "keka"}

    def test_get_adapter_returns_class(self):
        assert get_adapter("greenhouse") is GreenhouseAdapter
        assert get_adapter("lever") is LeverAdapter
        assert get_adapter("darwinbox") is DarwinboxWordPressAdapter
        assert get_adapter("keka") is KekaAdapter

    def test_get_adapter_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown adapter_type"):
            get_adapter("nonexistent")


class TestGreenhouseAdapter:
    @pytest.fixture()
    def adapter(self):
        company = _make_company(
            name="Razorpay",
            slug="razorpay",
            adapter_type="greenhouse",
            endpoint_url="https://boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs",
        )
        return GreenhouseAdapter(company)

    def test_normalize(self, adapter):
        fixture = json.loads((FIXTURES / "greenhouse_razorpay.json").read_text())
        raw = fixture["jobs"]
        postings = adapter.normalize(raw)

        assert len(postings) == 2
        p = postings[0]
        assert p["external_id"] == "4718628005"
        assert p["title"] == "Associate Manager, Solutions Engineering"
        assert p["location"] == "Bengaluru"
        assert p["raw_department"] == "Business Integration"
        assert p["description_text"] is not None
        assert "<" not in p["description_text"]
        assert p["content_hash"]
        assert p["url"].startswith("https://")

    def test_fetch_appends_content_param(self, adapter):
        fixture_path = FIXTURES / "greenhouse_razorpay.json"
        mock_resp = _mock_response(fixture_path)
        with patch("telltale.adapters.greenhouse.fetch_url", return_value=mock_resp) as mock_fetch:
            raw = adapter.fetch()
            call_url = mock_fetch.call_args[0][0]
            assert "content=true" in call_url
            assert len(raw) == 2

    def test_requires_endpoint_url(self):
        with pytest.raises(ValueError, match="endpoint_url required"):
            GreenhouseAdapter(_make_company(adapter_type="greenhouse"))


class TestLeverAdapter:
    @pytest.fixture()
    def adapter(self):
        company = _make_company(
            name="Zeta",
            slug="zeta",
            adapter_type="lever",
            endpoint_url="https://api.lever.co/v0/postings/zeta",
        )
        return LeverAdapter(company)

    def test_normalize(self, adapter):
        fixture = json.loads((FIXTURES / "lever_zeta.json").read_text())
        postings = adapter.normalize(fixture)

        assert len(postings) == 2
        p = postings[0]
        assert p["external_id"]
        assert p["title"]
        assert p["content_hash"]

    def test_fetch_raises_on_unknown_slug(self, adapter):
        fixture_path = FIXTURES / "lever_unknown_slug.json"
        mock_resp = _mock_response(fixture_path)
        with (
            patch("telltale.adapters.lever.fetch_url", return_value=mock_resp),
            pytest.raises(LeverError, match="Lever API error"),
        ):
            adapter.fetch()

    def test_fetch_success(self, adapter):
        fixture_path = FIXTURES / "lever_zeta.json"
        mock_resp = _mock_response(fixture_path)
        with patch("telltale.adapters.lever.fetch_url", return_value=mock_resp):
            raw = adapter.fetch()
            assert isinstance(raw, list)
            assert len(raw) == 2

    def test_requires_endpoint_url(self):
        with pytest.raises(ValueError, match="endpoint_url required"):
            LeverAdapter(_make_company(adapter_type="lever"))


class TestDarwinboxWordPressAdapter:
    @pytest.fixture()
    def adapter(self):
        company = _make_company(
            name="Lendingkart",
            slug="lendingkart",
            adapter_type="darwinbox",
            careers_url="https://www.lendingkart.com/job/",
            endpoint_url="https://hrlendingkart.darwinbox.in/ms/candidatev2/main/careers",
        )
        return DarwinboxWordPressAdapter(company)

    @staticmethod
    def _routed_fetch():
        """Serve the listing page first, then the post-content page for each job."""
        listing = _mock_response(
            FIXTURES / "darwinbox_lendingkart.html", content_type="text/html"
        )
        post = FIXTURES / "darwinbox_post_content.html"

        def _fetch(url, **kwargs):
            if "/?p=" in url:
                return _mock_response(post, content_type="text/html")
            return listing

        return _fetch

    def test_fetch_deduplicates(self, adapter):
        with patch(
            "telltale.adapters.darwinbox_wp.fetch_url", side_effect=self._routed_fetch()
        ):
            raw = adapter.fetch()
        assert len(raw) == 3
        job_ids = [j["job_id"] for j in raw]
        assert len(job_ids) == len(set(job_ids))

    def test_extracts_distinct_titles(self, adapter):
        """Regression: every posting once carried the first job's title."""
        with patch(
            "telltale.adapters.darwinbox_wp.fetch_url", side_effect=self._routed_fetch()
        ):
            raw = adapter.fetch()

        titles = [j["title"] for j in raw]
        assert len(set(titles)) == 3
        assert "Regional FCU Manager" in titles[1]

    def test_extracts_taxonomy_from_loop_classes(self, adapter):
        with patch(
            "telltale.adapters.darwinbox_wp.fetch_url", side_effect=self._routed_fetch()
        ):
            raw = adapter.fetch()

        assert raw[0]["raw_department"] == "Operations"
        assert raw[0]["location"] == "Bangalore"
        assert raw[1]["raw_department"] == "Risk Management"
        assert raw[1]["location"] == "Chennai"

    def test_fetches_description_from_wordpress_post(self, adapter):
        """Regression: postings used to reach the classifier with no description."""
        with patch(
            "telltale.adapters.darwinbox_wp.fetch_url", side_effect=self._routed_fetch()
        ):
            raw = adapter.fetch()

        for job in raw:
            assert job["description_text"], "every posting must carry a description"
            assert "Key Responsibilities" in job["description_text"]
            assert "Copyright Lendingkart" not in job["description_text"]

    def test_normalize(self, adapter):
        with patch(
            "telltale.adapters.darwinbox_wp.fetch_url", side_effect=self._routed_fetch()
        ):
            raw = adapter.fetch()
        postings = adapter.normalize(raw)

        assert len(postings) == 3
        p = postings[0]
        assert p["external_id"] == "a6a7318ee583fe"
        assert "Deputy Manager" in p["title"]
        assert p["description_text"]
        assert p["content_hash"]

    def test_survives_missing_post_content(self, adapter):
        listing = _mock_response(
            FIXTURES / "darwinbox_lendingkart.html", content_type="text/html"
        )

        def _fetch(url, **kwargs):
            if "/?p=" in url:
                raise RuntimeError("post page 500")
            return listing

        with patch("telltale.adapters.darwinbox_wp.fetch_url", side_effect=_fetch):
            raw = adapter.fetch()

        assert len(raw) == 3
        assert all(j["description_text"] is None for j in raw)


class TestKekaAdapter:
    @pytest.fixture()
    def adapter(self):
        company = _make_company(
            name="Jupiter",
            slug="jupiter",
            adapter_type="keka",
            endpoint_url="https://jupiter.keka.com/careers",
            keka_identifier="b5279857-cf81-4dde-a215-fc48957ee2b5",
        )
        return KekaAdapter(company)

    def test_normalize(self, adapter):
        fixture = json.loads((FIXTURES / "keka_jupiter.json").read_text())
        postings = adapter.normalize(fixture)

        assert len(postings) == 2
        p = postings[0]
        assert p["external_id"] == "138159"
        assert p["title"] == "Legal Associate"
        assert p["raw_department"] == "Legal"
        assert "Bengaluru" in p["location"]
        assert p["description_text"] is not None
        assert "<" not in p["description_text"]
        assert p["content_hash"]

    def test_fetch_builds_correct_url(self, adapter):
        fixture_path = FIXTURES / "keka_jupiter.json"
        mock_resp = _mock_response(fixture_path)
        with patch("telltale.adapters.keka.fetch_url", return_value=mock_resp) as mock_fetch:
            adapter.fetch()
            call_url = mock_fetch.call_args[0][0]
            assert "/api/embedjobs/default/active/b5279857-" in call_url

    def test_requires_endpoint_url(self):
        with pytest.raises(ValueError, match="endpoint_url required"):
            KekaAdapter(_make_company(adapter_type="keka", keka_identifier="abc"))

    def test_requires_keka_identifier(self):
        with pytest.raises(ValueError, match="keka_identifier required"):
            KekaAdapter(_make_company(
                adapter_type="keka",
                endpoint_url="https://example.keka.com/careers",
            ))


class TestRun:
    def test_run_calls_fetch_then_normalize(self):
        company = _make_company(
            adapter_type="greenhouse",
            endpoint_url="https://boards-api.greenhouse.io/v1/boards/test/jobs",
        )
        adapter = GreenhouseAdapter(company)
        fixture = json.loads((FIXTURES / "greenhouse_razorpay.json").read_text())
        mock_resp = _mock_response(FIXTURES / "greenhouse_razorpay.json")
        with patch("telltale.adapters.greenhouse.fetch_url", return_value=mock_resp):
            result = adapter.run()
            assert len(result) == 2
            assert all("external_id" in p for p in result)
