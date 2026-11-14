"""Partial-scope notes, refusal replacement and scope instructions for the answer model."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage

from src.graph.nodes import generator, response_formatter, terminal_nodes

CATALOG = [
    {"company": "amd", "name": "AMD", "aliases": [], "fiscal_years": [2022], "documents": [(2022, "10k")]},
    {"company": "3m", "name": "3M", "aliases": [], "fiscal_years": [2023], "documents": [(2023, "10q")]},
]
CHUNK = {"content": "Net revenue 23,601", "metadata": {"source_file": "AMD_2022_10K.pdf", "page_number": 50}}
NOTE = "No year was given, so the latest available filings were used: AMD FY2022."


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(response_formatter, "available_metadata", lambda state: CATALOG)


def test_refusal_becomes_a_scoped_not_found_reply_without_disclaimer_or_sources():
    result = response_formatter.response_formatter_node(
        {
            "generated_answer": "I don't have enough information to answer this question.",
            "relevant_chunks": [CHUNK],
            "hallucination_status": "unverified",
            "target_companies": ["amd"],
            "scope_notes": [NOTE],
        }
    )
    assert result["final_response"] == (
        "I couldn't find this in the filings available to you for AMD (FY2022 10-K). "
        "Some details are disclosed only in other filings, such as proxy statements, which aren't included."
        f"\n\n**Note:** {NOTE}"
    )
    assert result["response_metadata"]["sources"] == []
    assert "3M" not in result["final_response"]


def test_answers_keep_citations_and_gain_the_scope_note():
    result = response_formatter.response_formatter_node(
        {
            "generated_answer": "AMD's FY2022 revenue was $23.6 billion.",
            "relevant_chunks": [CHUNK],
            "hallucination_status": "grounded",
            "hallucination_score": 1.0,
            "scope_notes": ["Apple isn't available within your access, so it isn't included."],
        }
    )
    assert result["final_response"].endswith(
        "\n\n**Note:** Apple isn't available within your access, so it isn't included."
    )
    assert result["response_metadata"]["sources"]


def test_search_with_no_evidence_names_the_searched_filings():
    answer = terminal_nodes.no_info_node({"target_companies": ["3m"]})["final_response"]
    assert answer.startswith("I couldn't find this in the filings available to you for 3M (FY2023 10-Q).")


@pytest.mark.asyncio
@pytest.mark.parametrize("notes", [[], [NOTE]])
async def test_answer_model_sees_scope_only_when_it_was_narrowed(monkeypatch, notes):
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="Answer"))
    monkeypatch.setattr(generator.LLMFactory, "get_generator_llm", lambda: model)
    await generator.generator_node(
        {"contextualized_query": "AMD revenue?", "relevant_chunks": [CHUNK], "scope_notes": notes}
    )
    prompt = model.ainvoke.call_args.args[0][-1].content
    assert ("Scope of this answer:" in prompt) is bool(notes)
    if notes:
        assert NOTE in prompt and "do not refuse because another company or year is unavailable" in prompt
