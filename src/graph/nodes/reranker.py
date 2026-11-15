"""Reranker node — narrows hybrid-search candidates to the top-K most relevant.

Reads `retrieved_chunks` (~50 candidates from hybrid dense+BM25 search),
scores each against the query with the configured reranker, and writes the
top-K to `reranked_chunks`. Downstream grader + generator read from
`reranked_chunks` instead of the raw retrieval pool.
"""

import logging

from src.config.settings import settings
from src.models.state import RAGState
from src.services.finance_terms import with_statement_terms
from src.services.reranker_service import rerank, select

logger = logging.getLogger(__name__)


def reranker_node(state: RAGState) -> dict:
    """Rerank retrieved candidates and select top-K."""
    query = state.get("retrieval_query") or state.get("contextualized_query", "")
    candidates = state.get("retrieved_chunks", [])

    if not candidates:
        return {"reranked_chunks": []}

    top_k = settings.RERANKER_TOP_K
    rerank_text = with_statement_terms(query) if settings.SEARCH_STATEMENT_TERMS else query
    scored = rerank(rerank_text, candidates, top_k=len(candidates))
    reranked = select(scored, top_k, settings.RERANK_SELECTION, settings.RERANK_FUSION_K)
    logger.info(
        f"Reranked {len(candidates)} -> {len(reranked)} chunks "
        f"(top score: {reranked[0]['rerank_score']:.3f} | bottom: {reranked[-1]['rerank_score']:.3f})"
    )
    from src.services.event_log import emit

    emit(
        "reranker",
        n_input=len(candidates),
        n_kept=len(reranked),
        top_score=reranked[0]["rerank_score"] if reranked else None,
        bottom_score=reranked[-1]["rerank_score"] if reranked else None,
        top_k=top_k,
        model=reranked[0].get("rerank_model") if reranked else None,
    )
    return {"reranked_chunks": reranked}
