"""Hybrid search sends BM25 query weights and the collection applies IDF."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from qdrant_client.models import Modifier, PayloadSchemaType

from src.services import vector_store


class FakeBm25:
    def embed(self, texts):
        raise AssertionError("Queries must use query_embed, not document weights")

    def query_embed(self, text):
        yield SimpleNamespace(indices=[11, 22], values=[1.0, 1.0])


def test_hybrid_search_uses_query_side_bm25_with_the_same_filter_on_both_arms():
    client = MagicMock()
    client.query_points.return_value.points = []
    access_filter = vector_store.build_retrieval_filter(["10k"], ["public"])
    with patch.object(vector_store, "get_sparse_embedder", return_value=FakeBm25()):
        vector_store.hybrid_search(client, "Apple revenue", [0.1], rbac_filter=access_filter)
    dense, sparse = client.query_points.call_args.kwargs["prefetch"]
    assert sparse.query.indices == [11, 22] and sparse.query.values == [1.0, 1.0]
    assert dense.filter is sparse.filter is access_filter


def test_sparse_vectors_apply_idf():
    assert vector_store.SPARSE_VECTOR_PARAMS.modifier == Modifier.IDF


def test_payload_indexes_cover_every_filtered_field():
    client = MagicMock()
    vector_store.create_payload_indexes(client, "docs")
    created = {c.kwargs["field_name"]: c.kwargs["field_schema"] for c in client.create_payload_index.call_args_list}
    assert created == {
        "doc_type": PayloadSchemaType.KEYWORD,
        "confidentiality": PayloadSchemaType.KEYWORD,
        "company": PayloadSchemaType.KEYWORD,
        "source_file": PayloadSchemaType.KEYWORD,
        "fiscal_year": PayloadSchemaType.INTEGER,
    }
