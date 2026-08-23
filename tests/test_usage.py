from __future__ import annotations

import json

from telltale.llm import usage


def _path(tmp_path):
    return tmp_path / "usage.json"


def test_record_accumulates(tmp_path):
    p = _path(tmp_path)
    assert usage.record("groq", 100, p) == 100
    assert usage.record("groq", 250, p) == 350
    assert usage.spent_today("groq", p) == 350


def test_ignores_non_positive(tmp_path):
    p = _path(tmp_path)
    usage.record("groq", 0, p)
    assert usage.spent_today("groq", p) == 0


def test_remaining_subtracts_from_daily_limit(tmp_path):
    p = _path(tmp_path)
    usage.record("groq", 150_000, p)
    assert usage.remaining_today("groq", p) == 50_000


def test_remaining_never_negative(tmp_path):
    p = _path(tmp_path)
    usage.record("groq", 250_000, p)
    assert usage.remaining_today("groq", p) == 0


def test_unknown_provider_has_no_ceiling(tmp_path):
    assert usage.remaining_today("ollama", _path(tmp_path)) is None


def test_providers_tracked_separately(tmp_path):
    p = _path(tmp_path)
    usage.record("groq", 100, p)
    usage.record("gemini", 40, p)
    assert usage.spent_today("groq", p) == 100
    assert usage.spent_today("gemini", p) == 40


def test_only_today_counts(tmp_path):
    p = _path(tmp_path)
    p.write_text(json.dumps({"2020-01-01": {"groq": 999}}))
    assert usage.spent_today("groq", p) == 0


def test_prunes_old_days(tmp_path):
    p = _path(tmp_path)
    old = {f"2020-01-{d:02d}": {"groq": 1} for d in range(1, 26)}
    p.write_text(json.dumps(old))
    usage.record("groq", 5, p)
    assert len(json.loads(p.read_text())) <= 15


def test_survives_corrupt_ledger(tmp_path):
    p = _path(tmp_path)
    p.write_text("{ not json")
    assert usage.spent_today("groq", p) == 0
    assert usage.record("groq", 10, p) == 10
