"""Qdrant storage and hybrid retrieval.

Dense vectors score semantic similarity; sparse BM25 vectors score keyword
matches. Reciprocal Rank Fusion combines the two rankings. Searches apply
mandatory permissions and validated company/year metadata filters."""

import logging
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchAny,
    Modifier,
    PayloadSchemaType,
    Prefetch,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
)

from src.config.settings import settings
from src.services.model_loading import MODEL_LOAD_LOCK

logger = logging.getLogger(__name__)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
BM25_MODEL_NAME = "Qdrant/bm25"


# Stored sparse values are BM25 term weights; Qdrant applies IDF at query time.
SPARSE_VECTOR_PARAMS = SparseVectorParams(index=SparseIndexParams(on_disk=False), modifier=Modifier.IDF)

# Fields used by access, company/year and document filters.
PAYLOAD_INDEXES = {
    "doc_type": PayloadSchemaType.KEYWORD,
    "confidentiality": PayloadSchemaType.KEYWORD,
    "company": PayloadSchemaType.KEYWORD,
    "source_file": PayloadSchemaType.KEYWORD,
    "fiscal_year": PayloadSchemaType.INTEGER,
}


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)


def create_payload_indexes(client: QdrantClient, collection_name: str) -> None:
    """Index the filtered payload fields. Existing indexes are left unchanged."""
    for field, schema in PAYLOAD_INDEXES.items():
        client.create_payload_index(collection_name=collection_name, field_name=field, field_schema=schema, wait=True)


def get_sparse_embedder():
    """Return the cached BM25 sparse embedder, loading it under the shared model lock."""
    with MODEL_LOAD_LOCK:
        return _load_sparse_embedder()


@lru_cache(maxsize=1)
def _load_sparse_embedder():
    """Load the BM25 sparse embedder (fastembed) once; loading is not cheap."""
    from fastembed import SparseTextEmbedding

    logger.info(f"Loading sparse embedder: {BM25_MODEL_NAME}")
    return SparseTextEmbedding(model_name=BM25_MODEL_NAME)


def compute_sparse_vectors(texts: list[str]) -> list[SparseVector]:
    """Return document-side BM25 vectors, one per input text."""
    embedder = get_sparse_embedder()
    return [SparseVector(indices=list(e.indices), values=list(e.values)) for e in embedder.embed(texts)]


def compute_query_sparse_vector(text: str) -> SparseVector:
    """Return a query-side BM25 vector: each distinct query term weighted 1."""
    e = next(iter(get_sparse_embedder().query_embed(text)))
    return SparseVector(indices=list(e.indices), values=list(e.values))


def build_retrieval_filter(
    allowed_doc_types: list[str],
    allowed_confidentiality: list[str],
    target_companies: list[str] | None = None,
    target_fiscal_years: list[int] | None = None,
) -> Filter | None:
    """Access conditions AND company-in-list AND year-in-list; empty lists add no entity filter."""
    conditions: list = []

    if "*" not in allowed_doc_types:
        conditions.append(FieldCondition(key="doc_type", match=MatchAny(any=allowed_doc_types)))

    if "*" not in allowed_confidentiality:
        conditions.append(FieldCondition(key="confidentiality", match=MatchAny(any=allowed_confidentiality)))

    if target_companies:
        conditions.append(FieldCondition(key="company", match=MatchAny(any=target_companies)))
    if target_fiscal_years:
        conditions.append(FieldCondition(key="fiscal_year", match=MatchAny(any=target_fiscal_years)))

    return Filter(must=conditions) if conditions else None


def hybrid_search(
    client: QdrantClient,
    query_text: str,
    query_dense_vector: list[float],
    rbac_filter: Filter | None = None,
    top_k: int = 50,
    fusion_candidates_per_arm: int | None = None,
    collection: str | None = None,
) -> list[dict]:
    """Search dense and sparse vectors with the same metadata filter and fuse their ranks.

    Each prefetch retrieves candidates (top_k per arm unless set); Reciprocal Rank Fusion
    combines the ranked lists and returns up to top_k chunks. Access rules are never relaxed."""
    sparse = compute_query_sparse_vector(query_text)
    per_arm = fusion_candidates_per_arm or top_k

    results = client.query_points(
        collection_name=collection or settings.QDRANT_COLLECTION,
        prefetch=[
            Prefetch(
                query=query_dense_vector,
                using=DENSE_VECTOR_NAME,
                limit=per_arm,
                filter=rbac_filter,
            ),
            Prefetch(
                query=sparse,
                using=SPARSE_VECTOR_NAME,
                limit=per_arm,
                filter=rbac_filter,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        query_filter=rbac_filter,
        limit=top_k,
        with_payload=True,
    )
    return [
        {
            "content": point.payload.get("content", ""),
            "metadata": {k: v for k, v in point.payload.items() if k != "content"},
            "score": point.score,
        }
        for point in results.points
    ]


def dense_ranks(
    client: QdrantClient, query_dense_vector: list[float], rbac_filter: Filter | None, limit: int = 50
) -> dict[tuple, int]:
    """Rank of each chunk in dense search alone, keyed by (source_file, chunk_index)."""
    points = client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=query_dense_vector,
        using=DENSE_VECTOR_NAME,
        query_filter=rbac_filter,
        limit=limit,
        with_payload=["source_file", "chunk_index"],
    ).points
    return {(p.payload.get("source_file"), p.payload.get("chunk_index")): rank for rank, p in enumerate(points, 1)}
