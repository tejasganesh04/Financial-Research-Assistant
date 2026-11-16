"""Add each kept chunk's neighbours from the same filing before answering.

Tables and paragraphs are often split across consecutive chunks, so a kept chunk
may hold current assets while the next one holds current liabilities. The
chunks just before and after each kept chunk are added in reading order. Kept
chunks always stay; neighbours are added until the size budget is reached.
"""

import logging

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.state import RAGState
from src.services.request_trace import record
from src.services.vector_store import build_retrieval_filter, get_qdrant_client

logger = logging.getLogger(__name__)


def position(chunk: dict) -> tuple[str | None, int | None]:
    meta = chunk.get("metadata") or {}
    return meta.get("source_file"), meta.get("chunk_index")


def access_filter(state: RAGState) -> Filter | None:
    """The same document-type and confidentiality limits that retrieval applied to this request."""
    permissions = get_permissions(state.get("user_role", "analyst"))
    scope = state.get("access_scope") or {}
    return build_retrieval_filter(
        allowed_doc_types=state.get("allowed_doc_types", permissions["allowed_doc_types"]),
        allowed_confidentiality=scope.get("confidentiality", permissions["allowed_confidentiality"]),
    )


def fetch_neighbours(chunks: list[dict], access: Filter | None = None) -> dict[tuple, dict]:
    """Chunks at positions ±1 around each kept chunk, keyed by (file, position).

    Neighbours pass the request's access filter too, so a document that mixed access labels
    could never leak a restricted neighbour."""
    kept = {position(c) for c in chunks}
    wanted: dict[str, set[int]] = {}
    for source_file, index in kept:
        if source_file is None or index is None:
            continue
        for neighbour in (index - 1, index + 1):
            if neighbour >= 0 and (source_file, neighbour) not in kept:
                wanted.setdefault(source_file, set()).add(neighbour)
    found = {}
    client = get_qdrant_client()
    for source_file, indexes in wanted.items():
        points = client.scroll(
            settings.QDRANT_COLLECTION,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="source_file", match=MatchValue(value=source_file)),
                    FieldCondition(key="chunk_index", match=MatchAny(any=sorted(indexes))),
                    *(access.must if access is not None else []),
                ]
            ),
            limit=len(indexes),
            with_payload=True,
        )[0]
        for point in points:
            payload = point.payload or {}
            found[(source_file, payload.get("chunk_index"))] = {
                "content": payload.get("content", ""),
                "metadata": {k: v for k, v in payload.items() if k != "content"},
                "score": None,
                "neighbour": True,
            }
    return found


def context_expander_node(state: RAGState) -> dict:
    chunks = state.get("relevant_chunks") or []
    if not chunks or settings.NEIGHBOUR_CHAR_BUDGET <= 0:
        return {}
    try:
        neighbours = fetch_neighbours(chunks, access_filter(state))
    except Exception as error:  # noqa: BLE001 - answering from the kept chunks alone is still correct
        logger.warning("Neighbour lookup failed; answering from kept chunks only: %s", type(error).__name__)
        return {}
    expanded, seen, budget = [], set(), settings.NEIGHBOUR_CHAR_BUDGET
    for chunk in chunks:
        source_file, index = position(chunk)
        for key in (
            (source_file, index - 1) if index is not None else None,
            None,
            (source_file, index + 1) if index is not None else None,
        ):
            candidate = chunk if key is None else neighbours.get(key)
            if candidate is None:
                continue
            candidate_key = position(candidate)
            if candidate_key in seen:
                continue
            if candidate is not chunk:
                if len(candidate["content"]) > budget:
                    continue
                budget -= len(candidate["content"])
            seen.add(candidate_key)
            expanded.append(candidate)
    logger.info("Context expanded from %d to %d chunks", len(chunks), len(expanded))
    record(
        "context_expansion",
        kept_chunks=len(chunks),
        neighbours_added=len(expanded) - len(chunks),
        neighbour_chars=settings.NEIGHBOUR_CHAR_BUDGET - budget,
    )
    return {"relevant_chunks": expanded}
