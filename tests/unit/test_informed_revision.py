"""Informed draft revision through the real graph, with model/search responses mocked."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from src.api.routes.conversations import _build_initial_state
from src.config.rbac_config import get_permissions
from src.graph import builder
from src.graph.nodes import generator, hallucination
from src.models.auth import User
from src.models.schemas import HallucinationCheck
from src.services.request_trace import RequestTrace

QUESTION = "What was the company's revenue?"
CHUNK = {"content": "Revenue was $10 million.", "metadata": {"source_file": "filing.pdf", "page_number": 8}}
WRONG_DRAFT = "Revenue was $20 million."
CORRECT_DRAFT = "Revenue was $10 million. [Source: filing.pdf, Page 8]"
FEEDBACK = "The draft says $20 million; filing.pdf supports $10 million. Correct the revenue figure."


def checker_returning(monkeypatch, verdict):
    model = MagicMock()
    model.with_structured_output.return_value.invoke.return_value = verdict
    monkeypatch.setattr(hallucination.LLMFactory, "get_hallucination_llm", lambda: model)
    return model


@pytest.mark.parametrize("grounded,score", [(False, 0.3), (True, 0.5)])
def test_failed_checks_save_existing_explanation_for_revision(monkeypatch, grounded, score):
    checker_returning(monkeypatch, HallucinationCheck(
        grounded=grounded, score=score, explanation="  " + FEEDBACK + "  "))
    result = hallucination.hallucination_checker_node({
        "generated_answer": WRONG_DRAFT, "relevant_chunks": [CHUNK], "generation_retry_count": 0,
    })
    assert result["hallucination_status"] == "hallucinated"
    assert result["hallucination_feedback"] == FEEDBACK
    assert result["generation_retry_count"] == 1


def test_successful_check_clears_previous_feedback(monkeypatch):
    checker_returning(monkeypatch, HallucinationCheck(grounded=True, score=0.9, explanation="Supported"))
    result = hallucination.hallucination_checker_node({
        "generated_answer": CORRECT_DRAFT, "relevant_chunks": [CHUNK],
        "hallucination_feedback": FEEDBACK, "generation_retry_count": 1,
    })
    assert result["hallucination_feedback"] == ""
    assert result["generation_retry_count"] == 1


@pytest.mark.parametrize("failure", ["missing_answer", "missing_evidence", "checker_error"])
def test_unverified_checks_clear_stale_feedback(monkeypatch, failure):
    model = checker_returning(monkeypatch, None)
    model.with_structured_output.return_value.invoke.side_effect = RuntimeError("checker unavailable")
    state = {"generated_answer": CORRECT_DRAFT, "relevant_chunks": [CHUNK], "hallucination_feedback": FEEDBACK}
    if failure == "missing_answer":
        state["generated_answer"] = ""
    elif failure == "missing_evidence":
        state["relevant_chunks"] = []
    result = hallucination.hallucination_checker_node(state)
    assert result["hallucination_status"] == "unverified"
    assert result["hallucination_feedback"] == ""
    if failure != "checker_error":
        model.with_structured_output.return_value.invoke.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retries,feedback", [
    ("", 0, ""),
    ("hallucinated", 0, FEEDBACK),
    ("grounded", 1, FEEDBACK),
    ("unverified", 1, FEEDBACK),
    ("hallucinated", 1, ""),
])
async def test_initial_and_unreviewed_attempts_do_not_receive_revision_material(monkeypatch, status, retries, feedback):
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content=CORRECT_DRAFT))
    monkeypatch.setattr(generator.LLMFactory, "get_generator_llm", lambda: model)
    await generator.generator_node({
        "contextualized_query": QUESTION, "relevant_chunks": [CHUNK],
        "generated_answer": WRONG_DRAFT, "hallucination_feedback": feedback,
        "hallucination_status": status, "generation_retry_count": retries,
    })
    prompt = model.ainvoke.call_args.args[0][-1].content
    assert QUESTION in prompt and CHUNK["content"] in prompt
    assert WRONG_DRAFT not in prompt and FEEDBACK not in prompt
    assert "Previous draft (not verified evidence)" not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("revision_passes", [True, False])
@pytest.mark.parametrize("research_path", [False, True])
async def test_real_graph_revises_then_rechecks_with_bounded_retry_and_new_turn_reset(
    monkeypatch, revision_passes, research_path,
):
    # Keep actual generator, checker, edges and formatter; replace only upstream model/search work.
    monkeypatch.setattr(builder, "guardrails_node", lambda s: {
        "guardrail_status": "clean", "contextualized_query": s["original_query"]})
    monkeypatch.setattr(builder, "metadata_extractor_node", lambda s: {"metadata_unavailable": False})
    monkeypatch.setattr(builder, "router_node", lambda s: {
        "query_intent": "retrieval", "query_complexity": "research_required" if research_path else "simple_lookup"})
    monkeypatch.setattr(builder, "retrieval_node", lambda s: {"retrieved_chunks": [CHUNK]})
    monkeypatch.setattr(builder, "reranker_node", lambda s: {"reranked_chunks": [CHUNK]})
    monkeypatch.setattr(builder, "grader_node", lambda s: {"relevant_chunks": [CHUNK]})
    synthesis = "Research finding: revenue is $10 million."
    monkeypatch.setattr(builder, "research_agent_node", lambda s: {
        "relevant_chunks": [CHUNK], "agent_synthesis": synthesis})

    answer_model = MagicMock()
    answer_model.ainvoke = AsyncMock(side_effect=[
        AIMessage(content=WRONG_DRAFT), AIMessage(content=CORRECT_DRAFT), AIMessage(content=CORRECT_DRAFT),
    ])
    monkeypatch.setattr(generator.LLMFactory, "get_generator_llm", lambda: answer_model)
    review_model = checker_returning(monkeypatch, None)
    review_model.with_structured_output.return_value.invoke.side_effect = [
        HallucinationCheck(grounded=False, score=0.3, explanation=FEEDBACK),
        HallucinationCheck(grounded=revision_passes, score=0.95 if revision_passes else 0.4,
                           explanation="Supported" if revision_passes else "Still uncertain about the figure."),
        HallucinationCheck(grounded=True, score=0.95, explanation="Supported"),
    ]
    graph = builder.build_graph(InMemorySaver())
    user = User(user_id="analyst", name="Analyst", role="analyst", department="Research")
    trace = RequestTrace("informed-revision", QUESTION, user.role, get_permissions(user.role))
    config = {"configurable": {"thread_id": "informed-revision"}, "callbacks": [trace]}
    with trace.activate():
        result = await graph.ainvoke(_build_initial_state(QUESTION, user, trace.request_id), config)
    trace.finish()

    prompts = [call.args[0][-1].content for call in answer_model.ainvoke.call_args_list]
    assert len(prompts) == 2
    assert WRONG_DRAFT not in prompts[0] and FEEDBACK not in prompts[0]
    assert WRONG_DRAFT in prompts[1] and FEEDBACK in prompts[1]
    assert QUESTION in prompts[1] and CHUNK["content"] in prompts[1]
    assert all((synthesis in prompt) is research_path for prompt in prompts)
    system = answer_model.ainvoke.call_args_list[1].args[0][0].content
    assert "Do not accept a suggested fact or number unless the evidence supports it" in system
    assert WRONG_DRAFT not in system and FEEDBACK not in system
    reviews = review_model.with_structured_output.return_value.invoke.call_args_list
    assert len(reviews) == 2
    assert WRONG_DRAFT in reviews[0].args[0][-1].content
    assert CORRECT_DRAFT in reviews[1].args[0][-1].content
    assert result["generated_answer"] == CORRECT_DRAFT
    assert result["generation_retry_count"] == (1 if revision_passes else 2)
    assert ("could not be fully verified" in result["final_response"]) is not revision_passes
    assert result["hallucination_feedback"] == ("" if revision_passes else "Still uncertain about the figure.")
    assert len(result["messages"]) == 2  # Only the user message and released answer, not the failed draft.
    assert result["messages"][-1].content == result["final_response"]

    events_text = (trace.directory / "events.jsonl").read_text()
    events = [json.loads(line) for line in events_text.splitlines()]
    assert [e["revision"] for e in events if e["stage"] == "decision.generator"] == [False, True]
    assert WRONG_DRAFT not in events_text and FEEDBACK not in events_text

    # Reuse the same checkpoint with a fresh API input: no old draft/feedback enters the next prompt.
    new_input = _build_initial_state(QUESTION, user, "next-request")
    assert new_input["hallucination_feedback"] == ""
    next_result = await graph.ainvoke(new_input, {"configurable": config["configurable"]})
    next_prompt = answer_model.ainvoke.call_args.args[0][-1].content
    assert FEEDBACK not in next_prompt and WRONG_DRAFT not in next_prompt
    assert "Previous draft (not verified evidence)" not in next_prompt
    assert next_result["generation_retry_count"] == 0
    assert next_result["hallucination_feedback"] == ""
