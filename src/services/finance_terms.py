"""Financial concepts and the statement wording that reports them.

Analyst questions name concepts ("quick ratio", "capital-intensive") that the
financial statements never print; the statements use line items ("Cash and cash
equivalents", "Total current liabilities"). Adding those line items to the text
used for search and reranking lets table chunks match. Definitions are standard
textbook ones, not tuned to any benchmark question.
"""

from __future__ import annotations

import re

BALANCE_SHEET = "balance sheet"
INCOME_STATEMENT = "income statement statement of operations"
CASH_FLOW = "statement of cash flows"

TERMS: list[tuple[str, str]] = [
    (
        r"quick ratio|acid[- ]test",
        f"{BALANCE_SHEET} cash and cash equivalents short-term investments accounts receivable "
        "total current liabilities",
    ),
    (r"current ratio|working capital", f"{BALANCE_SHEET} total current assets total current liabilities"),
    (r"liquidity", f"{BALANCE_SHEET} cash and cash equivalents total current assets total current liabilities"),
    (r"gross (?:profit )?margin|gross profit", f"{INCOME_STATEMENT} net revenue cost of sales gross profit"),
    (r"operating (?:profit )?margin|operating income", f"{INCOME_STATEMENT} operating income net revenue"),
    (r"net (?:profit |income )?margin|profit margin", f"{INCOME_STATEMENT} net income net revenue"),
    (r"ebitda", f"{INCOME_STATEMENT} operating income depreciation and amortization"),
    (
        r"capex|capital expenditure|capital[- ]intensive|capital intensity",
        f"{CASH_FLOW} purchases of property plant and equipment {BALANCE_SHEET} "
        "property plant and equipment net total assets",
    ),
    (
        r"free cash ?flow|\bfcf\b",
        f"{CASH_FLOW} net cash provided by operating activities purchases of property plant and equipment",
    ),
    (r"operating cash ?flow|cash from operations", f"{CASH_FLOW} net cash provided by operating activities"),
    (r"\bppn?&?e\b|property,? plant", f"{BALANCE_SHEET} property plant and equipment net"),
    (
        r"\bdpo\b|days payable",
        f"{BALANCE_SHEET} accounts payable inventories {INCOME_STATEMENT} cost of sales",
    ),
    (r"\bdso\b|days sales outstanding|receivable turnover", f"{BALANCE_SHEET} accounts receivable net revenue"),
    (r"\bdio\b|days inventory|inventory turnover", f"{BALANCE_SHEET} inventories {INCOME_STATEMENT} cost of sales"),
    (
        r"cash conversion cycle",
        f"{BALANCE_SHEET} inventories accounts receivable accounts payable {INCOME_STATEMENT} cost of sales revenue",
    ),
    (
        r"asset turnover",
        f"{INCOME_STATEMENT} net revenue {BALANCE_SHEET} total assets property plant and equipment net",
    ),
    (r"effective tax rate", f"{INCOME_STATEMENT} provision for income taxes income before income taxes"),
    (r"interest coverage", f"{INCOME_STATEMENT} operating income interest expense"),
    (
        r"\bleverage\b|debt[- ]to[- ]equity|total debt",
        f"{BALANCE_SHEET} long-term debt short-term borrowings total stockholders equity",
    ),
    (r"return on assets|\broa\b", f"{INCOME_STATEMENT} net income {BALANCE_SHEET} total assets"),
    (r"return on equity|\broe\b", f"{INCOME_STATEMENT} net income {BALANCE_SHEET} total stockholders equity"),
    (r"\beps\b|earnings per share", f"{INCOME_STATEMENT} diluted earnings per share"),
    (r"dividend", f"{CASH_FLOW} dividends paid dividends declared per share"),
]
PATTERNS = [(re.compile(pattern, re.IGNORECASE), words) for pattern, words in TERMS]


def statement_terms(question: str) -> str:
    """Statement wording for the financial concepts the question names; empty if none."""
    found: list[str] = []
    for pattern, terms in PATTERNS:
        if pattern.search(question) and terms not in found:
            found.append(terms)
    return "; ".join(found)


def with_statement_terms(question: str) -> str:
    terms = statement_terms(question)
    return f"{question}\n{terms}" if terms else question
