"""Neighbour expansion keeps reading order, avoids duplicates, respects its budget and fails safe."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.graph.nodes import context_expander


def chunk(index, text=None, source="AMD_2022_10K.pdf"):
    return {
        "content": text or f"chunk {index}",
        "metadata": {"source_file": source, "chunk_index": index, "page_number": 56},
    }


def client_with(points_by_index):
    client = MagicMock()

    def scroll(collection, scroll_filter, limit, with_payload):
        wanted = scroll_filter.must[1].match.any
        return (
            [
                SimpleNamespace(
                    payload={"content": points_by_index[i], "source_file": "AMD_2022_10K.pdf", "chunk_index": i}
                )
                for i in wanted
                if i in points_by_index
            ],
            None,
        )

    client.scroll.side_effect = scroll
    return client


def test_neighbours_join_in_reading_order_without_duplicates(monkeypatch):
    stored = {4: "assets", 6: "liabilities", 7: "equity", 9: "notes"}
    monkeypatch.setattr(context_expander, "get_qdrant_client", lambda: client_with(stored))
    result = context_expander.context_expander_node({"relevant_chunks": [chunk(5), chunk(8)]})
    order = [c["metadata"]["chunk_index"] for c in result["relevant_chunks"]]
    assert order == [4, 5, 6, 7, 8, 9]
    assert [c.get("neighbour", False) for c in result["relevant_chunks"]] == [True, False, True, True, False, True]


def test_kept_chunks_stay_and_neighbours_stop_at_the_budget(monkeypatch):
    monkeypatch.setattr(context_expander, "NEIGHBOUR_CHAR_BUDGET", 10)
    monkeypatch.setattr(context_expander, "get_qdrant_client", lambda: client_with({0: "x" * 8, 2: "y" * 8}))
    result = context_expander.context_expander_node({"relevant_chunks": [chunk(1)]})
    assert [c["metadata"]["chunk_index"] for c in result["relevant_chunks"]] == [0, 1]


def test_lookup_failure_keeps_the_original_chunks(monkeypatch):
    def broken():
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(context_expander, "get_qdrant_client", broken)
    assert context_expander.context_expander_node({"relevant_chunks": [chunk(1)]}) == {}
    assert context_expander.context_expander_node({"relevant_chunks": []}) == {}
