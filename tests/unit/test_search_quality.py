"""Statement wording for financial concepts and the final chunk selection rules."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.graph.nodes import reranker, retrieval
from src.services.finance_terms import statement_terms, with_statement_terms
from src.services.reranker_service import select


def test_concepts_gain_statement_wording_and_other_questions_are_unchanged():
    assert "total current liabilities" in statement_terms("Does AMD have a healthy quick ratio?")
    assert "statement of cash flows" in statement_terms("What was 3M's FY2018 capex?")
    assert with_statement_terms("Who are Boeing's primary customers?") == "Who are Boeing's primary customers?"
    expanded = with_statement_terms("Quick ratio and current ratio?")
    assert expanded.startswith("Quick ratio and current ratio?\n") and expanded.count("balance sheet") == 2


def chunks(dense_ranks):
    return [{"content": f"c{i}", "dense_rank": rank} for i, rank in enumerate(dense_ranks)]


def test_selection_rules():
    # Reranker order is c0..c5; c5 is the dense top result the reranker placed last.
    scored = chunks([None, 9, None, None, 2, 1])
    assert [c["content"] for c in select(scored, 3)] == ["c0", "c1", "c2"]
    assert "c5" in [c["content"] for c in select(scored, 3, "fusion", fusion_k=1)]
    assert [c["content"] for c in select(scored, 3, "protect")] == ["c5", "c4", "c0"]


def test_retrieval_searches_with_statement_wording_and_records_dense_rank(monkeypatch):
    monkeypatch.setattr(retrieval.settings, "SEARCH_STATEMENT_TERMS", True)
    monkeypatch.setattr(retrieval.settings, "RERANK_SELECTION", "fusion")
    hit = {"content": "balance sheet", "metadata": {"source_file": "AMD.pdf", "chunk_index": 7}}
    client = MagicMock()
    client.query_points.return_value.points = [SimpleNamespace(payload={"source_file": "AMD.pdf", "chunk_index": 7})]
    with (
        patch.object(retrieval, "embed_text", return_value=[0.0]) as embed,
        patch.object(retrieval, "get_qdrant_client", return_value=client),
        patch.object(retrieval, "hybrid_search", return_value=[hit]) as search,
    ):
        result = retrieval.retrieval_node({"contextualized_query": "AMD quick ratio FY2022?", "user_role": "analyst"})
    assert "total current liabilities" in embed.call_args.args[0]
    assert search.call_args.kwargs["query_text"] == embed.call_args.args[0]
    assert result["retrieved_chunks"][0]["dense_rank"] == 1


def test_switching_statement_wording_off_restores_the_question(monkeypatch):
    monkeypatch.setattr(reranker.settings, "SEARCH_STATEMENT_TERMS", False)
    monkeypatch.setattr(reranker.settings, "RERANK_SELECTION", "rerank")
    with patch.object(reranker, "rerank", side_effect=lambda text, chunks, top_k: chunks) as scored:
        reranker.reranker_node(
            {"contextualized_query": "AMD quick ratio?", "retrieved_chunks": [{"content": "a", "rerank_score": 1.0}]}
        )
    assert scored.call_args.args[0] == "AMD quick ratio?"


def test_voyage_scores_order_chunks_and_name_the_model(monkeypatch):
    from src.services import reranker_service

    monkeypatch.setattr(reranker_service.settings, "RERANKER_PROVIDER", "voyage")
    monkeypatch.setattr(reranker_service.settings, "VOYAGE_RERANK_MODEL", "rerank-3")
    monkeypatch.setattr(reranker_service, "voyage_scores", lambda query, documents: [0.1, 0.9])
    ranked = reranker_service.rerank("AMD revenue", [{"content": "risk"}, {"content": "income statement"}], top_k=2)
    assert [c["content"] for c in ranked] == ["income statement", "risk"]
    assert ranked[0]["rerank_model"] == "rerank-3"


def test_voyage_failure_falls_back_to_the_local_model(monkeypatch):
    from src.services import reranker_service

    def unavailable(query, documents):
        raise ConnectionError("voyage down")

    local = MagicMock()
    local.predict.return_value = [0.2, 0.7]
    monkeypatch.setattr(reranker_service.settings, "RERANKER_PROVIDER", "voyage")
    monkeypatch.setattr(reranker_service, "voyage_scores", unavailable)
    monkeypatch.setattr(reranker_service, "get_reranker", lambda: local)
    ranked = reranker_service.rerank("AMD revenue", [{"content": "a"}, {"content": "b"}], top_k=2)
    assert [c["content"] for c in ranked] == ["b", "a"]
    assert ranked[0]["rerank_model"] == reranker_service.RERANKER_MODEL


def test_voyage_failure_keeps_search_order_without_local_fallback(monkeypatch):
    from src.services import reranker_service

    def unavailable(query, documents):
        raise ConnectionError("voyage down")

    def must_not_load():
        raise AssertionError("the local model must not load")

    monkeypatch.setattr(reranker_service.settings, "RERANKER_PROVIDER", "voyage")
    monkeypatch.setattr(reranker_service.settings, "RERANKER_LOCAL_FALLBACK", False)
    monkeypatch.setattr(reranker_service, "voyage_scores", unavailable)
    monkeypatch.setattr(reranker_service, "get_reranker", must_not_load)
    chunks = [{"content": c} for c in ("first", "second", "third")]
    ranked = reranker_service.rerank("AMD revenue", chunks, top_k=2)
    assert [c["content"] for c in ranked] == ["first", "second"]
    assert ranked[0]["rerank_model"] == reranker_service.SEARCH_ORDER
