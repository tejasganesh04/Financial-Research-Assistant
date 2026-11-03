"""Trace tests use fake responses, including real LangGraph callback propagation."""
import json
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph

from src.config.rbac_config import get_permissions
from src.services.cost_tracker import RequestScopedCostHandler
from src.services.request_trace import RequestTrace, active_trace, observe_step
from src.models.state import RAGState


def make_trace():
    return RequestTrace("conversation", "Microsoft revenue?", "analyst", get_permissions("analyst"))


def events(trace):
    return [json.loads(line) for line in (trace.directory / "events.jsonl").read_text().splitlines()]


def test_retries_and_summary_save_decisions_without_drafts():
    trace = make_trace()
    for _ in range(2):
        run = uuid4()
        trace.on_chain_start({}, {}, run_id=run, name="generator")
        trace.on_chain_end({"generated_answer": "PRIVATE DRAFT", "generation_retry_count": 1}, run_id=run)
    trace.finish("answered")
    saved = events(trace)
    assert [e["attempt"] for e in saved if e["stage"] == "step_finished"] == [1, 2]
    assert "PRIVATE DRAFT" not in (trace.directory / "events.jsonl").read_text()
    summary = (trace.directory / "summary.md").read_text()
    assert "generator | 2" in summary
    assert "prompts.py" in summary
    assert active_trace.get() is None


def test_write_failure_and_summary_failure_do_not_change_result(monkeypatch):
    trace = make_trace()
    monkeypatch.setattr(type(trace.directory), "mkdir", MagicMock(side_effect=OSError("read-only")))
    with trace.activate():
        assert observe_step("retrieval", lambda: {"retrieved_chunks": []}) == {"retrieved_chunks": []}
    trace.finish("answered")
    assert trace.outcome == "answered"


def test_missing_usage_unknown_pricing_and_failed_call_are_explicit():
    trace = make_trace()
    handler = RequestScopedCostHandler(trace)
    for model, usage in [("gpt-4o-mini", None), ("unknown-model", {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10})]:
        run = uuid4()
        handler.on_llm_start({"kwargs": {"model": model}}, [], run_id=run)
        handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="", usage_metadata=usage))]]), run_id=run)
    run = uuid4()
    handler.on_llm_start({"kwargs": {"model": "gpt-4o-mini"}}, [], run_id=run)
    handler.on_llm_error(TimeoutError(), run_id=run)
    trace.finish()
    calls = [e for e in events(trace) if e["stage"] == "model_call"]
    assert len(calls) == 3
    assert all(e["cost_usd"] is None for e in calls)
    assert calls[0]["usage_available"] is False
    assert calls[-1]["error_type"] == "TimeoutError"
    assert "(incomplete)" in (trace.directory / "summary.md").read_text()


class OfflineModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "offline-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="relevant", response_metadata={"model_name": "gpt-4o-mini"},
            usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                            "input_token_details": {"cache_read": 4}},
        ))])

    def with_structured_output(self, schema, **kwargs):
        def grade(messages):
            self.invoke(messages)
            return schema(relevant=True, reason="Relevant financial evidence")
        return RunnableLambda(grade)


@pytest.mark.asyncio
async def test_parallel_grading_keeps_usage_and_requests_separate(monkeypatch):
    import asyncio
    from src.graph.nodes import grader
    from src.config.settings import settings
    from src.services import result_cache
    monkeypatch.setattr(settings, "ENABLE_DETERMINISTIC_VALIDATOR", False)
    monkeypatch.setattr(grader, "_GRADER_PARALLELISM", 3)
    monkeypatch.setattr(grader.LLMFactory, "get_grader_llm", lambda: OfflineModel())
    cache = MagicMock()
    cache.get.return_value = None
    monkeypatch.setattr(result_cache, "_client", cache)
    graph = StateGraph(RAGState)
    graph.add_node("grader", grader.grader_node)
    graph.add_edge(START, "grader")
    graph.add_edge("grader", END)
    graph = graph.compile()

    async def question():
        trace = make_trace()
        handler = RequestScopedCostHandler(trace)
        with trace.activate():
            result = await graph.ainvoke({"contextualized_query": "revenue", "reranked_chunks": [
                {"content": f"Financial evidence {i}", "metadata": {}} for i in range(3)
            ]}, config={"callbacks": [trace, handler]})
        trace.finish()
        assert "relevant_chunks" in result, repr(result)
        assert len(result["relevant_chunks"]) == 3
        assert handler.aggregate()["tokens"]["input"] == 30
        assert handler.aggregate()["tokens"]["output"] == 6
        calls = [e for e in events(trace) if e["stage"] == "model_call"]
        assert len(calls) == 3
        assert all(e["request_id"] == trace.request_id for e in calls)
        return trace.request_id

    ids = await asyncio.gather(question(), question())
    assert ids[0] != ids[1]
    assert active_trace.get() is None


def test_embedding_usage_and_local_cache_hits(monkeypatch):
    from src.services import embeddings, result_cache
    from src.config.settings import settings
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "voyage-finance-2")
    client = MagicMock()
    client.embed.return_value = SimpleNamespace(embeddings=[[0.1, 0.2]], total_tokens=12)
    monkeypatch.setattr(embeddings, "_get_voyage_client", lambda: client)
    cache = MagicMock()
    cache.get.side_effect = [None, [0.1, 0.2]]
    monkeypatch.setattr(result_cache, "_client", cache)
    trace = make_trace()
    with trace.activate():
        assert embeddings.embed_text("revenue") == [0.1, 0.2]
        assert embeddings.embed_text("revenue") == [0.1, 0.2]
    trace.finish()
    calls = [e for e in events(trace) if e["stage"] == "embedding_call"]
    assert len(calls) == 1
    assert calls[0]["input_tokens"] == 12
    assert calls[0]["cost_usd"] is not None
    assert [e["hit"] for e in events(trace) if e["stage"] == "local_cache"] == [False, True]


def test_research_substeps_are_timed_without_saving_synthesis(monkeypatch):
    from src.graph.nodes import research_agent as agent
    chunk = {"content": "PRIVATE DOCUMENT TEXT", "metadata": {"source_file": "filing.pdf", "page_number": 3}}
    monkeypatch.setattr(agent, "_decompose", lambda *a: agent._Decomposition(
        qualifiers=[], required_quantities=["revenue"], sub_questions=["Find revenue"]))
    monkeypatch.setattr(agent, "retrieval_node", lambda s: {"retrieved_chunks": [chunk]})
    monkeypatch.setattr(agent, "reranker_node", lambda s: {"reranked_chunks": [chunk]})
    monkeypatch.setattr(agent, "grader_node", lambda s: {"relevant_chunks": [chunk]})
    monkeypatch.setattr(agent, "_judge_sufficiency", lambda *a: agent._SufficiencyVerdict(decision="sufficient", reason="Enough evidence"))
    monkeypatch.setattr(agent, "_synthesize", lambda *a: "PRIVATE SYNTHESIS")
    trace = make_trace()
    with trace.activate():
        result = agent.research_agent_node({"contextualized_query": "revenue"})
    trace.finish()
    assert result["agent_synthesis"] == "PRIVATE SYNTHESIS"
    path = [e["step"] for e in events(trace) if e["stage"] == "step_finished"]
    assert path == ["research.decompose", "retrieval", "reranker", "grader", "research.sufficiency", "research.synthesize"]
    report = (trace.directory / "summary.md").read_text()
    assert "filing.pdf" in report
    assert "PRIVATE" not in report
