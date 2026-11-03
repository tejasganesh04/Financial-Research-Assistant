"""Check real call sites keep request data out of system instructions; no APIs."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from src.graph.nodes import (
    generator,
    grader,
    guardrails,
    hallucination,
    metadata_extractor,
    query_rewriter,
    research_agent,
    router,
)
from src.models.schemas import GradeResult, HallucinationCheck, InjectionCheck, MetadataExtraction, RouterDecision
from src.services import guardrails_service, result_cache

QUESTION = "REQUEST_QUESTION_7F2: compare revenue"
EVIDENCE = "REQUEST_EVIDENCE_7F2: revenue was $10 million."
HISTORY = "REQUEST_HISTORY_7F2"
FEEDBACK = "REQUEST_FEEDBACK_7F2"
CHUNK = {"content": EVIDENCE, "metadata": {"source_file": "filing.pdf", "page_number": 8}}
PLAN = research_agent._Decomposition(
    qualifiers=["REQUEST_QUALIFIER_7F2"], required_quantities=["REQUEST_QUANTITY_7F2"],
    sub_questions=[QUESTION],
)


def assert_roles(messages, *request_values):
    assert len(messages) == 2
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    system = messages[0].content
    if isinstance(system, list):  # Anthropic's optional cache-control block.
        system = "\n".join(block["text"] for block in system)
    for value in request_values:
        assert value not in system
        assert value in messages[1].content
    assert system.strip()


@pytest.mark.parametrize("stage", [
    "router", "rewrite", "contextualization", "metadata", "injection",
    "decompose", "sufficiency", "synthesis", "grader", "checker",
])
def test_each_sync_call_separates_rules_from_request_material(monkeypatch, stage):
    model = MagicMock()
    model.model_name = "offline-test-model"
    model.invoke.return_value = AIMessage(content="Result")
    structured = model.with_structured_output.return_value
    call = model.invoke
    expected = [QUESTION]
    # All imports share this factory; replace every model selector used below.
    for name in (
        "get_router_llm", "get_grader_llm", "get_research_decompose_llm",
        "get_research_sufficiency_llm", "get_research_synthesize_llm", "get_hallucination_llm",
    ):
        monkeypatch.setattr(router.LLMFactory, name, lambda: model)

    if stage == "router":
        structured.invoke.return_value = RouterDecision(intent="retrieval", reason="Financial question")
        assert router.router_node({"contextualized_query": QUESTION})["query_intent"] == "retrieval"
        call = structured.invoke
    elif stage == "rewrite":
        assert query_rewriter.query_rewriter_node({
            "contextualized_query": QUESTION, "grading_results": [{"relevant": False, "reason": FEEDBACK}],
        })["retrieval_query"] == "Result"
        expected.append(FEEDBACK)
    elif stage == "contextualization":
        assert guardrails._contextualize_query(QUESTION, [
            HumanMessage(content=HISTORY), AIMessage(content="Earlier answer"), HumanMessage(content=QUESTION),
        ]) == "Result"
        expected.append(HISTORY)
    elif stage == "metadata":
        monkeypatch.setattr(metadata_extractor, "available_metadata", lambda s: [
            {"company": "REQUEST_COMPANY_7F2", "fiscal_years": [2023]},
        ])
        structured.invoke.return_value = MetadataExtraction(reason="Unrestricted")
        assert not metadata_extractor.metadata_extractor_node({"contextualized_query": QUESTION})["metadata_unavailable"]
        expected.append("REQUEST_COMPANY_7F2")
        call = structured.invoke
    elif stage == "injection":
        structured.invoke.return_value = InjectionCheck(is_injection=False, confidence=0.9, reason="Allowed")
        assert guardrails_service.check_injection_llm(QUESTION) == (False, 0.9)
        call = structured.invoke
    elif stage == "decompose":
        structured.invoke.return_value = PLAN
        assert research_agent._decompose(QUESTION, ["REQUEST_COMPANY_7F2"], [2023]) == PLAN
        expected += ["REQUEST_COMPANY_7F2", "2023"]
        call = structured.invoke
    elif stage == "sufficiency":
        structured.invoke.return_value = research_agent._SufficiencyVerdict(decision="sufficient", reason="Enough")
        assert research_agent._judge_sufficiency(QUESTION, PLAN, [CHUNK]).decision == "sufficient"
        expected += [EVIDENCE, PLAN.required_quantities[0], PLAN.qualifiers[0]]
        call = structured.invoke
    elif stage == "synthesis":
        assert research_agent._synthesize(QUESTION, PLAN, [CHUNK]) == "Result"
        expected += [EVIDENCE, PLAN.required_quantities[0], PLAN.qualifiers[0]]
    elif stage == "grader":
        cache = MagicMock()
        cache.get.return_value = None
        monkeypatch.setattr(result_cache, "_client", cache)
        structured.invoke.return_value = GradeResult(relevant=True, reason="Supports question")
        assert grader.grader_node({"contextualized_query": QUESTION, "reranked_chunks": [CHUNK]})["relevant_chunks"] == [CHUNK]
        expected.append(EVIDENCE)
        call = structured.invoke
    elif stage == "checker":
        structured.invoke.return_value = HallucinationCheck(grounded=True, score=1, explanation="Supported")
        assert hallucination.hallucination_checker_node({
            "generated_answer": QUESTION, "relevant_chunks": [CHUNK],
        })["hallucination_status"] == "grounded"
        expected.append(EVIDENCE)
        call = structured.invoke
    call.assert_called_once()
    assert_roles(call.call_args.args[0], *expected)


@pytest.mark.asyncio
async def test_generator_keeps_revision_draft_feedback_and_sources_in_human_message(monkeypatch):
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="Revised answer"))
    monkeypatch.setattr(generator.LLMFactory, "get_generator_llm", lambda: model)
    result = await generator.generator_node({
        "contextualized_query": QUESTION, "relevant_chunks": [CHUNK],
        "hallucination_status": "hallucinated", "generation_retry_count": 1,
        "hallucination_feedback": FEEDBACK, "generated_answer": "REQUEST_DRAFT_7F2",
        "agent_synthesis": "REQUEST_SYNTHESIS_7F2",
    })
    assert result == {"generated_answer": "Revised answer"}
    assert_roles(model.ainvoke.call_args.args[0], QUESTION, EVIDENCE, FEEDBACK,
                 "REQUEST_DRAFT_7F2", "REQUEST_SYNTHESIS_7F2")


@pytest.mark.parametrize("stage", ["decompose", "sufficiency"])
@pytest.mark.parametrize("model_name,native", [("openai/gpt-oss-20b", True), ("llama-3.3-70b-versatile", False)])
def test_research_uses_native_schema_only_for_supported_groq_models(monkeypatch, stage, model_name, native):
    model = ChatGroq(model=model_name, api_key="offline-test")
    structured = MagicMock()
    build = MagicMock(return_value=structured)
    monkeypatch.setattr(ChatGroq, "with_structured_output", build)
    if stage == "decompose":
        structured.invoke.return_value = PLAN
        monkeypatch.setattr(research_agent.LLMFactory, "get_research_decompose_llm", lambda: model)
        assert research_agent._decompose(QUESTION, [], []) == PLAN
        schema = research_agent._Decomposition
    else:
        structured.invoke.return_value = research_agent._SufficiencyVerdict(decision="sufficient", reason="Enough")
        monkeypatch.setattr(research_agent.LLMFactory, "get_research_sufficiency_llm", lambda: model)
        assert research_agent._judge_sufficiency(QUESTION, PLAN, [CHUNK]).decision == "sufficient"
        schema = research_agent._SufficiencyVerdict
    assert build.call_args.args == (schema,)
    assert build.call_args.kwargs == ({"method": "json_schema"} if native else {})
    structured.invoke.assert_called_once()
