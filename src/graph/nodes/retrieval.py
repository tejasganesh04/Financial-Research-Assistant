"""Hybrid search with mandatory access rules and validated company/year restrictions."""

import logging

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.state import RAGState
from src.services.embeddings import embed_text
from src.services.finance_terms import with_statement_terms
from src.services.request_trace import record
from src.services.vector_store import build_retrieval_filter, dense_ranks, get_qdrant_client, hybrid_search

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
    # Statement wording lets table chunks match questions that name analyst concepts.
    search_text = with_statement_terms(query) if settings.SEARCH_STATEMENT_TERMS else query
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
        client, vector = get_qdrant_client(), embed_text(search_text)
        chunks = hybrid_search(
            client=client,
            query_text=search_text,
            query_dense_vector=vector,
            rbac_filter=search_filter,
            top_k=settings.RETRIEVAL_TOP_K,
        )
        if settings.RERANK_SELECTION != "rerank":
            # Fusion and protect selections combine the dense-search rank with the reranker's.
            ranks = dense_ranks(client, vector, search_filter, limit=settings.RETRIEVAL_TOP_K)
            for chunk in chunks:
                meta = chunk.get("metadata") or {}
                chunk["dense_rank"] = ranks.get((meta.get("source_file"), meta.get("chunk_index")))
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
