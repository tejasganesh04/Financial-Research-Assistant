"""Routing format selection is provider-specific; these tests make no API calls."""
from types import SimpleNamespace

import pytest

from src.graph.nodes import router
from src.models.schemas import RouterDecision


class GroqStub:
    def __init__(self, model_name):
        self.model_name = model_name
        self.options = None

    def with_structured_output(self, schema, **options):
        self.options = options
        return SimpleNamespace(invoke=lambda messages: RouterDecision(
            intent="clarification", reason="Greeting", complexity="simple_lookup"))


@pytest.mark.parametrize("model,expected", [
    ("openai/gpt-oss-20b", "json_schema"),
    ("openai/gpt-oss-120b", "json_schema"),
    ("llama-3.3-70b-versatile", "function_calling"),
])
def test_groq_uses_supported_output_format(monkeypatch, model, expected):
    llm = GroqStub(model)
    monkeypatch.setattr(router, "ChatGroq", GroqStub)
    monkeypatch.setattr(router.LLMFactory, "get_router_llm", lambda: llm)
    result = router.router_node({"contextualized_query": "Hello"})
    assert result["query_intent"] == "clarification"
    assert llm.options == {"method": expected}


def test_other_providers_keep_existing_output_format(monkeypatch):
    class OtherProvider(GroqStub):
        pass
    llm = OtherProvider("gpt-4o-mini")
    monkeypatch.setattr(router.LLMFactory, "get_router_llm", lambda: llm)
    router.router_node({"contextualized_query": "Hello"})
    assert llm.options == {}
