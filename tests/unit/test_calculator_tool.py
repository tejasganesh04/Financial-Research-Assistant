"""The answer model's calculator: tool loop, checker hand-off and the reader-facing note."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from src.config.prompts import GENERATOR_CALCULATOR_PROMPT, HALLUCINATION_CALCULATOR_PROMPT
from src.graph.nodes import generator, hallucination, response_formatter
from src.models.schemas import HallucinationCheck

CHUNK = {
    "content": "Accounts payable 25,309 and 34,616; cost of sales 111,934 (in millions).",
    "metadata": {"source_file": "AMAZON_2017_10K.pdf", "page_number": 40},
}
STATE = {"contextualized_query": "What was Amazon's FY2017 DPO?", "relevant_chunks": [CHUNK]}


def calc_call(program: str, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "calculate", "args": {"program": program}, "id": call_id}])


class ScriptedModel(BaseChatModel):
    """Returns queued replies and records what each call received."""

    replies: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append({"messages": list(messages), "tools": kwargs.get("tools")})
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)


@pytest.fixture
def model(monkeypatch):
    scripted = ScriptedModel()
    monkeypatch.setattr(generator.LLMFactory, "get_generator_llm", lambda: scripted)
    return scripted


async def test_answer_uses_the_calculator_result(model):
    model.replies = [
        calc_call("avg = average(25309, 34616)\ndpo = 365 * avg / 111934"),
        AIMessage(content="DPO = 365 × 29,962.5 ÷ 111,934 = 97.70 days [Source: AMAZON_2017_10K.pdf, Page 40]"),
    ]
    result = await generator.generator_node(dict(STATE))
    assert result["generated_answer"].startswith("DPO = 365 × 29,962.5")
    assert result["calculations"][0]["results"] == {"avg": 29962.5, "dpo": 97.703222}
    first, second = model.seen
    assert first["tools"][0]["function"]["name"] == "calculate"
    assert GENERATOR_CALCULATOR_PROMPT in first["messages"][0].content
    tool_reply = second["messages"][-1]
    assert isinstance(tool_reply, ToolMessage) and tool_reply.tool_call_id == "c1"
    assert '"dpo": 97.703222' in tool_reply.content and "program" not in tool_reply.content


async def test_calculator_errors_go_back_to_the_model(model):
    model.replies = [calc_call("x = 34,616 / 2"), calc_call("x = 34616 / 2", "c2"), AIMessage(content="17,308")]
    result = await generator.generator_node(dict(STATE))
    assert result["generated_answer"] == "17,308"
    assert "thousands separators" in model.seen[1]["messages"][-1].content
    assert [("error" in c) for c in result["calculations"]] == [True, False]


async def test_round_limit_forces_an_answer_without_tools(model, monkeypatch):
    monkeypatch.setattr(generator.settings, "CALCULATOR_MAX_ROUNDS", 2)
    model.replies = [calc_call("a = 1 + 1"), calc_call("b = 2 + 2", "c2"), AIMessage(content="Final answer")]
    result = await generator.generator_node(dict(STATE))
    assert result["generated_answer"] == "Final answer"
    last = model.seen[-1]
    assert last["tools"] is None
    assert "a = 1 + 1 = 2" in last["messages"][-1].content and "b = 2 + 2 = 4" in last["messages"][-1].content
    assert not any(isinstance(m, ToolMessage) for m in last["messages"])


async def test_disabled_calculator_sends_no_tools(model, monkeypatch):
    monkeypatch.setattr(generator.settings, "CALCULATOR_ENABLED", False)
    model.replies = [AIMessage(content="Answer")]
    result = await generator.generator_node(dict(STATE))
    assert result == {"generated_answer": "Answer", "calculations": []}
    assert model.seen[0]["tools"] is None
    assert GENERATOR_CALCULATOR_PROMPT not in model.seen[0]["messages"][0].content


async def test_unknown_tool_names_are_refused(model):
    model.replies = [
        AIMessage(content="", tool_calls=[{"name": "shell", "args": {"program": "ls"}, "id": "x"}]),
        AIMessage(content="Answer"),
    ]
    result = await generator.generator_node(dict(STATE))
    assert "Unknown tool" in result["calculations"][0]["error"]


def test_anthropic_style_text_blocks_are_joined():
    assert generator._text(AIMessage(content=[{"type": "text", "text": "A"}, {"type": "text", "text": "B"}])) == "AB"


def _checker(monkeypatch):
    structured = MagicMock()
    structured.invoke.return_value = HallucinationCheck(grounded=True, score=0.95, explanation="ok")
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    monkeypatch.setattr(hallucination.LLMFactory, "get_hallucination_llm", lambda: llm)
    return structured


def test_checker_receives_calculator_results(monkeypatch):
    structured = _checker(monkeypatch)
    state = {
        "generated_answer": "DPO = 97.70 days",
        "relevant_chunks": [CHUNK],
        "calculations": [{"program": "...", "results": {"dpo": 97.703222}, "steps": ["dpo = 365 * x / y = 97.703222"]}],
    }
    hallucination.hallucination_checker_node(state)
    system, user = structured.invoke.call_args.args[0]
    assert isinstance(system, SystemMessage) and HALLUCINATION_CALCULATOR_PROMPT in system.content
    assert "dpo = 365 * x / y = 97.703222" in user.content


def test_checker_prompt_is_unchanged_without_calculations(monkeypatch):
    structured = _checker(monkeypatch)
    hallucination.hallucination_checker_node({"generated_answer": "A", "relevant_chunks": [CHUNK], "calculations": []})
    system, user = structured.invoke.call_args.args[0]
    assert HALLUCINATION_CALCULATOR_PROMPT not in system.content and "Calculator results" not in user.content


def test_reader_note_only_when_a_calculation_succeeded():
    base = {"generated_answer": "DPO = 97.70 days", "relevant_chunks": [CHUNK], "hallucination_status": "grounded"}
    base["hallucination_score"] = 0.95
    with_calc = response_formatter.response_formatter_node({**base, "calculations": [{"steps": ["dpo = 97.7"]}]})
    assert "**Calculations:** worked out with an exact calculator" in with_calc["final_response"]
    assert with_calc["response_metadata"]["calculations"] == ["dpo = 97.7"]
    failed_only = response_formatter.response_formatter_node({**base, "calculations": [{"error": "Line 1: bad"}]})
    assert "Calculations" not in failed_only["final_response"]
