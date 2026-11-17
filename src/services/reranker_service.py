"""Rank retrieved chunks against the question.

The configured provider is Voyage (an API call) or the local BGE cross-encoder.
If the Voyage call fails, the local model scores the chunks so retrieval still
completes, or, with RERANKER_LOCAL_FALLBACK off, the search order is kept.
"""

import logging
import os
import time
from functools import lru_cache

from src.config.settings import settings
from src.services import service_status
from src.services.model_loading import MODEL_LOAD_LOCK

logger = logging.getLogger(__name__)

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEVICE = "cpu"
SEARCH_ORDER = "search-order"  # rerank_model when no reranker was available


def get_reranker():
    """Return the cached cross-encoder, loading it under the shared model lock."""
    with MODEL_LOAD_LOCK:
        return _load_reranker()


@lru_cache(maxsize=1)
def _load_reranker():
    """Load the cross-encoder once. First call downloads the model."""
    device = os.environ.get("RERANKER_DEVICE", DEFAULT_DEVICE)
    from sentence_transformers import CrossEncoder

    logger.info(f"Loading reranker: {RERANKER_MODEL} on device={device} (stock; first run downloads ~568MB)")
    return CrossEncoder(RERANKER_MODEL, device=device)


def voyage_scores(query: str, documents: list[str]) -> list[float]:
    """Relevance scores from Voyage's reranker, in document order."""
    from src.services.cost_tracker import compute_cost
    from src.services.embeddings import _get_voyage_client
    from src.services.request_trace import record

    start = time.monotonic()
    result = _get_voyage_client().rerank(query, documents, model=settings.VOYAGE_RERANK_MODEL, truncation=True)
    record(
        "rerank_call",
        provider="voyage",
        model=settings.VOYAGE_RERANK_MODEL,
        duration_s=round(time.monotonic() - start, 4),
        usage_available=True,
        input_tokens=result.total_tokens,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=compute_cost(settings.VOYAGE_RERANK_MODEL, input_tokens=result.total_tokens),
    )
    scores = [0.0] * len(documents)
    for item in result.results:
        scores[item.index] = item.relevance_score
    return scores


def score(query: str, documents: list[str]) -> tuple[list[float], str]:
    """Scores and the name of the model that produced them."""
    if settings.RERANKER_PROVIDER == "voyage":
        try:
            scores = voyage_scores(query, documents)
            service_status.note_ok("voyage_rerank")
            return scores, settings.VOYAGE_RERANK_MODEL
        except Exception as error:  # noqa: BLE001 - any provider failure falls back
            if not settings.RERANKER_LOCAL_FALLBACK:
                logger.warning("Voyage reranker unavailable (%s); keeping the search order", type(error).__name__)
                service_status.note_failure("voyage_rerank", error, fallback="search order")
                return [0.0] * len(documents), SEARCH_ORDER  # equal scores: the stable sort keeps the order
            logger.warning("Voyage reranker unavailable (%s); using the local model", type(error).__name__)
            service_status.note_failure("voyage_rerank", error, fallback="local model")
    return [float(s) for s in get_reranker().predict([(query, d) for d in documents])], RERANKER_MODEL


def rerank(query: str, chunks: list[dict], top_k: int = 5) -> list[dict]:
    """Score query/chunk pairs, sort them, and attach rerank_score and rerank_model."""
    if not chunks:
        return []

    scores, model = score(query, [c.get("content", "") for c in chunks])
    scored = [{**chunk, "rerank_score": float(s), "rerank_model": model} for chunk, s in zip(chunks, scores)]
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    return scored[:top_k]


def select(scored: list[dict], size: int, how: str = "rerank", fusion_k: int = 10) -> list[dict]:
    """Choose the final chunks from reranker-sorted candidates.

    rerank: reranker order. fusion: reciprocal rank fusion of reranker and dense-search ranks, so
    neither model decides alone. protect: dense-search top 3, then reranker order.
    """
    if how == "fusion":

        def fused(position_and_chunk):
            position, chunk = position_and_chunk
            return 1 / (fusion_k + position) + 1 / (fusion_k + (chunk.get("dense_rank") or 999))

        return [c for _, c in sorted(enumerate(scored, 1), key=fused, reverse=True)[:size]]
    if how == "protect":
        protected = sorted((c for c in scored if (c.get("dense_rank") or 999) <= 3), key=lambda c: c["dense_rank"])
        return protected + [c for c in scored if c not in protected][: size - len(protected)]
    return scored[:size]
