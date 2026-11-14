"""Rank retrieved chunks with the standard BGE cross-encoder and cache scores."""

import logging
import os
from functools import lru_cache

from src.services.model_loading import MODEL_LOAD_LOCK

logger = logging.getLogger(__name__)

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEVICE = "cpu"


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


def rerank(query: str, chunks: list[dict], top_k: int = 5) -> list[dict]:
    """Score query/chunk pairs with BGE, sort them, and attach rerank_score metadata."""
    if not chunks:
        return []

    scores = get_reranker().predict([(query, c.get("content", "")) for c in chunks])
    scored = [{**chunk, "rerank_score": float(score)} for chunk, score in zip(chunks, scores)]
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    return scored[:top_k]
