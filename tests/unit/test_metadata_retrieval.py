"""Selected metadata filters stay intact through searches, retries, grading and research."""
from unittest.mock import MagicMock, patch

from src.graph.nodes import grader, research_agent, retrieval
from src.services.vector_store import build_retrieval_filter


def conditions(query_filter):
    return {condition.key: condition.match.any for condition in query_filter.must}


def test_company_and_year_lists_are_anded_with_access_rules():
    result = build_retrieval_filter(["10k"], ["public"], ["microsoft", "3m"], [2016, 2023])
    assert conditions(result) == {"doc_type": ["10k"], "confidentiality": ["public"],
                                  "company": ["microsoft", "3m"], "fiscal_year": [2016, 2023]}


def test_empty_lists_omit_only_company_year_filters():
    assert conditions(build_retrieval_filter(["10k"], ["public"], [], [])) == {
        "doc_type": ["10k"], "confidentiality": ["public"],
    }


def test_empty_search_and_query_rewrite_do_not_relax_filters():
    state = {"contextualized_query": "Compare Microsoft periods", "user_role": "analyst",
             "allowed_doc_types": ["10k"], "target_companies": ["microsoft"], "target_fiscal_years": [2016, 2023]}
    with patch.object(retrieval, "embed_text", return_value=[0.0]), patch.object(
        retrieval, "get_qdrant_client"
    ), patch.object(retrieval, "hybrid_search", return_value=[]) as search:
        assert retrieval.retrieval_node(state)["retrieved_chunks"] == []
        retrieval.retrieval_node({**state, "retrieval_query": "A refined search"})
    assert search.call_count == 2  # One search per invocation, not progressively broader searches.
    filters = [conditions(c.kwargs["rbac_filter"]) for c in search.call_args_list]
    assert filters[0] == filters[1]
    assert filters[0]["company"] == ["microsoft"] and filters[0]["fiscal_year"] == [2016, 2023]


def test_unavailable_metadata_never_calls_search_or_embedding():
    with patch.object(retrieval, "embed_text") as embed, patch.object(retrieval, "hybrid_search") as search:
        assert retrieval.retrieval_node({"metadata_unavailable": True})["retrieved_chunks"] == []
    embed.assert_not_called()
    search.assert_not_called()


def test_wrong_year_candidates_never_reach_grading_model_even_when_diagnostics_disabled(monkeypatch):
    monkeypatch.setattr(grader.settings, "ENABLE_DETERMINISTIC_VALIDATOR", False)
    state = {"contextualized_query": "Microsoft FY2016", "target_companies": ["microsoft"],
             "target_fiscal_years": [2016], "reranked_chunks": [
                 {"content": "Wrong year", "metadata": {"company": "microsoft", "fiscal_year": 2023}},
             ]}
    with patch.object(grader.LLMFactory, "get_grader_llm") as model:
        assert grader.grader_node(state)["relevant_chunks"] == []
    model.assert_not_called()


def test_research_subquestion_keeps_parent_lists():
    state = {"target_companies": ["microsoft", "3m"], "target_fiscal_years": [2016, 2023],
             "allowed_doc_types": ["10k"], "access_scope": None}
    chunk = {"content": "evidence"}
    with patch.object(research_agent, "retrieval_node", return_value={"retrieved_chunks": [chunk]}) as search, patch.object(
        research_agent, "reranker_node", return_value={"reranked_chunks": [chunk]}
    ), patch.object(research_agent, "grader_node", return_value={"relevant_chunks": [chunk]}):
        assert research_agent._retrieve_and_grade_for_subq(state, "Subquestion") == [chunk]
    actual = search.call_args.args[0]
    assert actual["target_companies"] == state["target_companies"]
    assert actual["target_fiscal_years"] == state["target_fiscal_years"]
    assert actual["retrieval_query"] == "Subquestion"
    assert "retrieval_query" not in state
