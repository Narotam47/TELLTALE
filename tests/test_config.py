from pathlib import Path

from telltale.config import Company, load_companies


def test_load_companies():
    companies = load_companies(Path("companies.yaml"))
    assert len(companies) > 0
    assert all(isinstance(c, Company) for c in companies)
    slugs = [c.slug for c in companies]
    assert "razorpay" in slugs
