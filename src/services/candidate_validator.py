"""Reject candidates outside selected company/year filters. Never re-add mismatches."""
from __future__ import annotations


def _year_from_chunk(chunk: dict) -> int | None:
    meta = chunk.get("metadata") or {}
    year = meta.get("fiscal_year")
    if isinstance(year, int):
        return year
    if isinstance(year, str) and year.isdigit():
        return int(year)
    period = str(meta.get("fb_doc_period", "")).strip()
    return int(period) if period.isdigit() else None


def validate_candidates(*, candidates: list[dict], target_companies: list[str],
                        target_fiscal_years: list[int]) -> tuple[list[dict], list[dict]]:
    kept, diagnostics = [], []
    for index, chunk in enumerate(candidates):
        meta = chunk.get("metadata") or {}
        company, year = meta.get("company"), _year_from_chunk(chunk)
        entity_match = not target_companies or company in target_companies
        year_match = not target_fiscal_years or year in target_fiscal_years
        valid = entity_match and year_match
        diagnostics.append({"candidate_id": index, "entity_match": entity_match, "year_match": year_match,
                            "valid": valid, "chunk_company": company, "chunk_year": year})
        if valid:
            kept.append(chunk)
    return kept, diagnostics
