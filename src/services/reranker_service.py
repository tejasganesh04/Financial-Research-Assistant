"""Rank retrieved chunks with the standard BGE cross-encoder and cache scores."""
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEVICE = "cpu"


@lru_cache(maxsize=1)
def get_reranker():
    """Load the cross-encoder once and cache it. First call downloads the model.

    """
    device = os.environ.get("RERANKER_DEVICE", DEFAULT_DEVICE)
    from sentence_transformers import CrossEncoder

    logger.info(f"Loading reranker: {RERANKER_MODEL} on device={device} (stock; first run downloads ~568MB)")
    return CrossEncoder(RERANKER_MODEL, device=device)


def rerank(query: str, chunks: list[dict], top_k: int = 5) -> list[dict]:
    """Score query/chunk pairs with BGE and return the highest-ranked results.
    
    Reuse a score only for the same model, query and chunk text. Compute missing
    scores locally, sort candidates, and attach rerank_score metadata."""
    if not chunks:
        return []

    # Include the model in the key so changing it invalidates old scores.
    model_id = RERANKER_MODEL

    # Probe each pair's cache. Track which need a model forward pass.
    from src.services.result_cache import _KEY_PREFIX, _client, _hash_key

    cache_prefix = _KEY_PREFIX["reranker"]
    cached_scores: dict[int, float] = {}
    pending_indices: list[int] = []
    pending_pairs: list[tuple[str, str]] = []

    for i, c in enumerate(chunks):
        chunk_text = c.get("content", "")
        key = cache_prefix + _hash_key(model_id, query, chunk_text)
        hit = _client.get(key)
        if isinstance(hit, (int, float)):
            cached_scores[i] = float(hit)
        else:
            pending_indices.append(i)
            pending_pairs.append((query, chunk_text))

    # Compute scores only for misses, then store them.
    from src.services.request_trace import record
    record("local_cache", cache="reranker", hits=len(cached_scores), misses=len(pending_pairs))
    if pending_pairs:
        reranker = get_reranker()
        new_scores = reranker.predict(pending_pairs)
        for idx, score in zip(pending_indices, new_scores):
            score_f = float(score)
            cached_scores[idx] = score_f
            chunk_text = chunks[idx].get("content", "")
            key = cache_prefix + _hash_key(model_id, query, chunk_text)
            _client.set(key, score_f)

    scored = [
        {**chunk, "rerank_score": cached_scores[i]}
        for i, chunk in enumerate(chunks)
    ]
    scored.sort(key=lambda c: c["rerank_score"], reverse=True)
    return scored[:top_k]
