"""Contextualization boundaries and state updates, with no provider calls."""
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.graph.nodes.guardrails import _contextualize_query, _format_history, guardrails_node


def test_first_question_does_not_call_a_model():
    query = "What was Microsoft's revenue in FY2016?"
    with patch("src.graph.nodes.guardrails.LLMFactory.get_router_llm") as factory:
        assert _contextualize_query(query, [HumanMessage(content=query)]) == query
    factory.assert_not_called()


def test_no_usable_history_does_not_call_a_model():
    query = "What was Microsoft's revenue?"
    with patch("src.graph.nodes.guardrails.LLMFactory.get_router_llm") as factory:
        assert _contextualize_query(query, [SystemMessage(content="Context"), HumanMessage(content=query)]) == query
    factory.assert_not_called()


def test_history_excludes_current_question_and_old_turns_and_shortens_answers():
    messages = [message for i in range(4) for message in (
        HumanMessage(content=f"Earlier question [{i}]"),
        AIMessage(content="a" * 400 + f" answer tail [{i}]"),
    )] + [HumanMessage(content="CURRENT QUESTION")]
    history = _format_history(messages)
    assert "Earlier question [0]" not in history
    assert all(f"Earlier question [{i}]" in history for i in (1, 2, 3))
    assert "CURRENT QUESTION" not in history
    assert "answer tail" not in history
    assert history.count("Assistant: " + "a" * 400 + "...") == 3


@pytest.mark.parametrize("response", ["", "   ", "WHAT ABOUT ITS REVENUE?"])
def test_empty_or_equivalent_rewrite_keeps_original_question(response):
    query = "What about its revenue?"
    model = MagicMock()
    model.invoke.return_value = AIMessage(content=response)
    messages = [HumanMessage(content="Microsoft FY2016"), AIMessage(content="Earlier answer"),
                HumanMessage(content=query)]
    with patch("src.graph.nodes.guardrails.LLMFactory.get_router_llm", return_value=model):
        assert _contextualize_query(query, messages) == query
    model.invoke.assert_called_once()


def test_model_error_keeps_original_question():
    query = "And in FY2017?"
    model = MagicMock()
    model.invoke.side_effect = RuntimeError("Offline simulated outage")
    with patch("src.graph.nodes.guardrails.LLMFactory.get_router_llm", return_value=model):
        assert _contextualize_query(query, [HumanMessage(content="Microsoft revenue FY2016"),
                                           AIMessage(content="Earlier answer"), HumanMessage(content=query)]) == query


def test_rewrite_updates_only_working_query_and_preserves_user_history():
    query = "What about its operating income?"
    standalone = "What was Microsoft's operating income in FY2016?"
    messages = [HumanMessage(content="What was Microsoft's revenue in FY2016?"),
                AIMessage(content="Earlier answer"), HumanMessage(content=query)]
    state = {"messages": messages, "original_query": query, "allowed_doc_types": ["10k"]}
    model = MagicMock()
    model.invoke.return_value = AIMessage(content=f'  "{standalone}"  ')
    with patch("src.graph.nodes.guardrails.check_injection_llm_guard", return_value="pass"), patch(
        "src.graph.nodes.guardrails.LLMFactory.get_router_llm", return_value=model
    ):
        update = guardrails_node(state)
    assert update == {"guardrail_status": "clean", "contextualized_query": standalone}
    assert state["messages"] is messages
    assert state["messages"][-1].content == query
    assert state["original_query"] == query
    prompt = model.invoke.call_args.args[0]
    assert len(prompt) == 2 and isinstance(prompt[0], SystemMessage) and isinstance(prompt[1], HumanMessage)
    assert "Microsoft" in prompt[1].content and "FY2016" in prompt[1].content
    assert "Follow-up question: " + query in prompt[1].content
    assert query not in prompt[0].content


def test_injection_block_skips_contextualization():
    with patch("src.graph.nodes.guardrails.check_injection_llm_guard", return_value="block"), patch(
        "src.graph.nodes.guardrails.LLMFactory.get_router_llm"
    ) as factory:
        result = guardrails_node({"messages": [HumanMessage(content="Earlier"),
                                               AIMessage(content="Answer"), HumanMessage(content="Blocked input")]})
    assert result == {"guardrail_status": "injection_detected", "contextualized_query": "Blocked input"}
    factory.assert_not_called()
