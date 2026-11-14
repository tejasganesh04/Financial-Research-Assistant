"""Hybrid search with mandatory access rules and validated company/year restrictions."""

import logging

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.state import RAGState
from src.services.company_registry import remove_company_mentions
from src.services.embeddings import embed_text
from src.services.request_trace import record
from src.services.vector_store import build_retrieval_filter, get_qdrant_client, hybrid_search

logger = logging.getLogger(__name__)


def retrieval_node(state: RAGState) -> dict:
    query = state.get("retrieval_query") or state.get("contextualized_query", "")
    companies = state.get("target_companies") or []
    years = state.get("target_fiscal_years") or []
    permissions = get_permissions(state.get("user_role", "analyst"))
    scope = state.get("access_scope") or {}
    # The RBAC node selected types; confidentiality uses the same effective access scope.
    allowed_types = state.get("allowed_doc_types", permissions["allowed_doc_types"])
    levels = scope.get("confidentiality", permissions["allowed_confidentiality"])
    if state.get("metadata_unavailable"):
        return {"retrieved_chunks": []}
    # The filter already restricts one company's documents; its name in the search text only favours
    # passages that repeat the name over tables that never do. Comparisons keep names to separate companies.
    search_text = remove_company_mentions(query, companies[0]) if len(companies) == 1 else query
    try:
        search_filter = build_retrieval_filter(
            allowed_doc_types=allowed_types,
            allowed_confidentiality=levels,
            target_companies=companies,
            target_fiscal_years=years,
        )
        record(
            "search_filter",
            companies=companies,
            fiscal_years=years,
            search_text=search_text,
            filter=search_filter.model_dump(mode="json", exclude_none=True) if search_filter else None,
        )
        chunks = hybrid_search(
            client=get_qdrant_client(),
            query_text=search_text,
            query_dense_vector=embed_text(search_text),
            rbac_filter=search_filter,
            top_k=settings.RETRIEVAL_TOP_K,
        )
        # An empty result stays empty. Neither retries nor research may widen selected filters.
        from src.services.event_log import emit

        emit(
            "retrieval",
            target_companies=companies,
            target_fiscal_years=years,
            n_candidates=len(chunks),
            query_preview=query[:120],
        )
        return {"retrieved_chunks": chunks}
    except Exception as error:
        logger.error("Retrieval failed: %s", error, exc_info=True)
        raise RuntimeError("Document search is unavailable. Check service status before retrying.") from error
