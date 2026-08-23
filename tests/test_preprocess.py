from __future__ import annotations

from telltale.llm.preprocess import (
    build_window,
    find_role_anchor,
    normalize,
    role_content_share,
)

PHONEPE = """About PhonePe Limited:

Headquartered in India, its flagship product, the PhonePe digital payments app,
was launched in Aug 2016. As of April 2025, PhonePe has over 60 Crore registered
users and processes over 33 Crore transactions daily.

Culture:

At PhonePe, we go the extra mile to make sure you can bring your best self to work.

As a leader, you will be:

Facilitating discussions and lead decision making on all engineering aspects
Able to define and execute the engineering plans for the areas under ownership
Responsible for the health of the settlement systems owned by the team
"""

LENDINGKART = """Key Responsibilities
- Lead Office Operations: Oversee daily workspace activities and equipment maintenance.
- Manage the Branches: branch level day to day mis and handling the daily activities.
"""

NO_ANCHOR = """We are a fast growing company.
Our mission is to change the world.
"""


class TestAnchors:
    def test_finds_sentence_style_anchor(self):
        """Regression: 'As a leader, you will be:' was missed, leaving pure boilerplate."""
        anchor = find_role_anchor(PHONEPE)
        assert anchor is not None
        assert "you will" in anchor[1].lower()

    def test_finds_heading_anchor(self):
        anchor = find_role_anchor(LENDINGKART)
        assert anchor is not None
        assert anchor[0] == 0
        assert "responsibilities" in anchor[1].lower()

    def test_no_anchor_in_pure_boilerplate(self):
        assert find_role_anchor(NO_ANCHOR) is None


class TestBuildWindow:
    def test_strips_company_preamble(self):
        w = build_window(PHONEPE, 600, strip=True)
        assert w.stripped
        assert "Headquartered in India" not in w.text
        assert "60 Crore" not in w.text
        assert "Facilitating discussions" in w.text

    def test_keeps_settlement_evidence_that_truncation_hid(self):
        """The whole point: payments detail must survive into the window."""
        raw = build_window(PHONEPE, 300, strip=False)
        stripped = build_window(PHONEPE, 300, strip=True)
        assert "settlement" not in raw.text
        assert "settlement" in stripped.text

    def test_leaves_clean_description_untouched(self):
        w = build_window(LENDINGKART, 600, strip=True)
        assert w.text.startswith("Key Responsibilities")

    def test_falls_back_when_no_role_content(self):
        w = build_window(NO_ANCHOR, 600, strip=True)
        assert not w.stripped
        assert w.text.startswith("We are a fast growing company")

    def test_strip_disabled_returns_head(self):
        w = build_window(PHONEPE, 100, strip=False)
        assert w.text.startswith("About PhonePe")
        assert not w.stripped

    def test_handles_empty_and_none(self):
        assert build_window(None, 600).text == ""
        assert build_window("", 600).text == ""


class TestNormalize:
    def test_decodes_double_encoded_entities(self):
        assert "&nbsp;" not in normalize("Drive results&amp;nbsp; daily")
        assert "&" in normalize("Risk &amp; Compliance")

    def test_collapses_runs_of_whitespace(self):
        assert normalize("a    b\n\n\n\nc") == "a b\n\nc"


class TestRoleContentShare:
    def test_metric_is_independent_of_stripper(self):
        """Guards against a self-validating metric that always reports ~100%."""
        before = role_content_share(normalize(PHONEPE), 0, 200)
        assert before < 0.5, "opening 200 chars are company marketing"

    def test_share_rises_after_stripping(self):
        text = normalize(PHONEPE)
        w = build_window(PHONEPE, 600, strip=True)
        assert role_content_share(text, w.start, 600) > role_content_share(text, 0, 600)
