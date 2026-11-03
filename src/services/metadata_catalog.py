"""Small, cached catalog of company/year metadata actually present in Qdrant."""
from functools import lru_cache

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.services.company_registry import build_company_alias_map
from src.services.vector_store import get_qdrant_client


@lru_cache(maxsize=1)
def load_metadata_catalog() -> tuple[tuple[str, int | None, str, str], ...]:
    """Read metadata only, once per API process. Restart after restoring/changing the corpus.

    No chunk text or vectors are downloaded. Only successful reads are cached.
    Keep type/confidentiality so each request sees only its effective search scope.
    """
    client = get_qdrant_client()
    entries = set()
    offset = None
    try:
        while True:
            points, offset = client.scroll(
                collection_name=settings.QDRANT_COLLECTION,
                limit=1000, offset=offset, with_vectors=False,
                with_payload=["company", "fiscal_year", "doc_type", "confidentiality"],
            )
            for point in points:
                meta = point.payload or {}
                company = meta.get("company")
                if not isinstance(company, str) or not company.strip():
                    continue
                year = meta.get("fiscal_year")
                if year is not None:
                    year = int(year)
                entries.add((company.strip(), year, str(meta.get("doc_type", "")),
                             str(meta.get("confidentiality", ""))))
            if offset is None:
                break
    except Exception as error:
        raise RuntimeError("The knowledge-base metadata catalog is unavailable. Please try again.") from error
    return tuple(sorted(entries, key=lambda row: (row[0], row[1] or 0, row[2], row[3])))


def available_metadata(state: dict) -> list[dict]:
    """Company -> filing years within the same role-plus-escalation scope as retrieval."""
    permissions = get_permissions(state.get("user_role", "analyst"))
    scope = state.get("access_scope") or {}
    doc_types = scope.get("doc_types", permissions["allowed_doc_types"])
    levels = scope.get("confidentiality", permissions["allowed_confidentiality"])
    companies: dict[str, set[int]] = {}
    for company, year, doc_type, level in load_metadata_catalog():
        if "*" not in doc_types and doc_type not in doc_types:
            continue
        if "*" not in levels and level not in levels:
            continue
        years = companies.setdefault(company, set())
        if year is not None:
            years.add(year)
    aliases = build_company_alias_map()
    return [{"company": company, "aliases": sorted(aliases.get(company, set())),
             "fiscal_years": sorted(years)} for company, years in sorted(companies.items())]
