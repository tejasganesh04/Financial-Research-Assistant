import pytest

from src.services.company_registry import canonical_company_slug, remove_company_mentions


def test_canonical_company_slug_basic_aliases():
    assert canonical_company_slug("Apple Inc.") == "apple"
    assert canonical_company_slug("MSFT") == "microsoft"
    assert canonical_company_slug("Tesla Motors") == "tesla"


@pytest.mark.parametrize(
    "text,company,expected",
    [
        (
            "What were American Water Works' current liabilities in 2022?",
            "american_water_works",
            "What were current liabilities in 2022?",
        ),
        ("What were AMD's total current liabilities?", "amd", "What were total current liabilities?"),
        ("MGM Resorts capex as a % of revenue", "mgm_resorts", "capex as a % of revenue"),
        # Ambiguous tickers that are ordinary words are removed only as uppercase tickers.
        ("Costco cost of goods sold for COST", "costco", "cost of goods sold for"),
        # Other companies stay; nothing is removed if only the name would remain.
        ("Microsoft revenue", "3m", "Microsoft revenue"),
        ("3M", "3m", "3M"),
    ],
)
def test_remove_company_mentions(text, company, expected):
    assert remove_company_mentions(text, company) == expected
