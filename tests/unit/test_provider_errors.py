"""Provider limits stay visible at every fallback boundary; no network calls."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from src.services.provider_errors import (
    ProviderRateLimitError,
    is_rate_limited,
    raise_if_rate_limited,
    rate_limit_payload,
)


def limit_error(provider="groq", headers=None, body=None):
    from importlib import import_module

    response = httpx.Response(429, headers=headers or {}, request=httpx.Request("POST", "https://example.invalid"))
    return import_module(provider).RateLimitError("PRIVATE provider body and credentials", response=response, body=body)


@pytest.mark.parametrize("provider", ["groq", "openai", "anthropic"])
def test_real_sdk_limit_classes_have_safe_public_messages(provider):
    error = limit_error(provider, {"Retry-After": "12.2"})
    with pytest.raises(ProviderRateLimitError) as caught:
        raise_if_rate_limited(error, "grader", model="configured-model")
    public = caught.value.payload()
    assert public["status"] == 429 and public["code"] == "provider_rate_limited"
    assert public["retry_after_seconds"] == 13
    assert "grader" in public["message"] and "configured-model" in public["message"]
    assert "PRIVATE" not in str(public)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after-ms": "1501"}, 2),
        ({"retry-after": "invalid"}, None),
        ({"retry-after": "NaN"}, None),
        ({}, None),
    ],
)
def test_retry_time_is_measured_or_unknown_not_assumed(headers, expected):
    with pytest.raises(ProviderRateLimitError) as caught:
        raise_if_rate_limited(limit_error(headers=headers), "router")
    assert caught.value.retry_after_seconds == expected


def test_http_date_retry_after_and_wrapped_exception():
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    try:
        try:
            raise limit_error(headers={"retry-after": date})
        except Exception as error:
            raise RuntimeError("Search unavailable") from error
    except Exception as error:
        public = rate_limit_payload(error)
    assert public["status"] == 429 and 58 <= public["retry_after_seconds"] <= 60


def test_usage_quota_is_not_presented_as_a_short_wait():
    public = rate_limit_payload(limit_error("openai", body={"error": {"code": "insufficient_quota"}}))
    assert public["code"] == "provider_quota_exceeded"
    assert "billing" in public["message"] and "Wait" not in public["message"]


def test_unrelated_failure_is_not_relabelled_and_normalized_error_keeps_stage():
    assert not is_rate_limited(ValueError("Bad input"))
    assert rate_limit_payload(ValueError("Bad input")) is None
    error = ProviderRateLimitError(stage="retrieval", provider="Voyage", model="voyage-finance-2")
    with pytest.raises(ProviderRateLimitError) as caught:
        raise_if_rate_limited(error, "another_node")
    assert caught.value is error


def failing_model():
    model = MagicMock()
    model.model_name = "openai/gpt-oss-20b"
    model.invoke.side_effect = limit_error()
    model.with_structured_output.return_value.invoke.side_effect = limit_error()
    return model


@pytest.mark.parametrize(
    "stage",
    [
        "metadata_extractor",
        "router",
        "grader",
        "query_rewriter",
        "hallucination_checker",
        "guardrails.contextualization",
        "guardrails.injection_review",
        "research.decompose",
        "research.sufficiency",
        "research.synthesize",
    ],
)
def test_model_fallbacks_never_hide_rate_limits(stage, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    from src.graph.nodes import (
        grader,
        guardrails,
        hallucination,
        metadata_extractor,
        query_rewriter,
        research_agent,
        router,
    )
    from src.services import guardrails_service
    from src.services.llm_factory import LLMFactory

    model = failing_model()
    for method in (
        "get_router_llm",
        "get_grader_llm",
        "get_hallucination_llm",
        "get_research_decompose_llm",
        "get_research_sufficiency_llm",
        "get_research_synthesize_llm",
    ):
        monkeypatch.setattr(LLMFactory, method, lambda: model)
    monkeypatch.setattr(metadata_extractor, "available_metadata", lambda state: [])
    monkeypatch.setattr(grader.settings, "ENABLE_DETERMINISTIC_VALIDATOR", False)
    monkeypatch.setattr(grader, "_GRADER_PARALLELISM", 1)
    from src.services import result_cache

    monkeypatch.setattr(result_cache._client, "get", lambda key: None)
    monkeypatch.setattr(query_rewriter, "retry_llm_call", lambda fn, **kw: fn())
    chunk = {"content": "Financial evidence", "metadata": {}}
    state = {
        "contextualized_query": "Microsoft revenue?",
        "reranked_chunks": [chunk],
        "relevant_chunks": [chunk],
        "generated_answer": "Private draft",
    }
    decomposition = research_agent._Decomposition(qualifiers=[], required_quantities=[], sub_questions=["Revenue?"])
    calls = {
        "metadata_extractor": lambda: metadata_extractor.metadata_extractor_node(state),
        "router": lambda: router.router_node(state),
        "grader": lambda: grader.grader_node(state),
        "query_rewriter": lambda: query_rewriter.query_rewriter_node(state),
        "hallucination_checker": lambda: hallucination.hallucination_checker_node(state),
        "guardrails.contextualization": lambda: guardrails._contextualize_query(
            "What about profit?",
            [
                HumanMessage(content="Revenue?"),
                AIMessage(content="Prior answer"),
                HumanMessage(content="What about profit?"),
            ],
        ),
        "guardrails.injection_review": lambda: guardrails_service.check_injection_llm("Financial question"),
        "research.decompose": lambda: research_agent._decompose("Revenue?", [], []),
        "research.sufficiency": lambda: research_agent._judge_sufficiency("Revenue?", decomposition, [chunk]),
        "research.synthesize": lambda: research_agent._synthesize("Revenue?", decomposition, [chunk]),
    }
    with pytest.raises(ProviderRateLimitError) as caught:
        calls[stage]()
    assert caught.value.stage == stage and caught.value.model == "openai/gpt-oss-20b"


@pytest.mark.asyncio
async def test_generator_rate_limit_never_releases_a_draft(monkeypatch):
    from unittest.mock import AsyncMock

    from src.graph.nodes import generator

    model = SimpleNamespace(model_name="openai/gpt-oss-120b", ainvoke=AsyncMock(side_effect=limit_error()))
    monkeypatch.setattr(generator.LLMFactory, "get_generator_llm", lambda: model)
    with pytest.raises(ProviderRateLimitError) as caught:
        await generator.generator_node({"contextualized_query": "Revenue?", "relevant_chunks": []})
    assert caught.value.stage == "generator" and caught.value.model == "openai/gpt-oss-120b"


def test_own_retry_loop_does_not_hammer_a_rate_limited_api(monkeypatch):
    from src.services import llm_retry

    call = MagicMock(side_effect=limit_error())
    sleep = MagicMock()
    monkeypatch.setattr(llm_retry.time, "sleep", sleep)
    with pytest.raises(Exception) as caught:
        llm_retry.retry_llm_call(call)
    assert is_rate_limited(caught.value)
    assert call.call_count == 1
    sleep.assert_not_called()


def test_voyage_limit_has_model_and_does_not_sleep(monkeypatch):
    import voyageai.error as errors

    from src.services import embeddings

    monkeypatch.setattr(embeddings.settings, "EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr(embeddings.settings, "EMBEDDING_MODEL", "voyage-finance-2")
    client = MagicMock()
    client.embed.side_effect = errors.RateLimitError("Rate limit exceeded")
    monkeypatch.setattr(embeddings, "_get_voyage_client", lambda: client)
    sleep = MagicMock()
    monkeypatch.setattr(embeddings.time, "sleep", sleep)
    with pytest.raises(ProviderRateLimitError) as caught:
        embeddings._voyage_embed(["Revenue?"], "query")
    assert caught.value.provider == "Voyage" and caught.value.model == "voyage-finance-2"
    assert client.embed.call_count == 1
    sleep.assert_not_called()


def test_rate_limited_grader_does_not_call_remaining_queued_chunks(monkeypatch):
    from src.graph.nodes import grader
    from src.services import result_cache

    model = failing_model()
    monkeypatch.setattr(grader.LLMFactory, "get_grader_llm", lambda: model)
    monkeypatch.setattr(grader.settings, "ENABLE_DETERMINISTIC_VALIDATOR", False)
    monkeypatch.setattr(grader, "_GRADER_PARALLELISM", 1)
    monkeypatch.setattr(result_cache._client, "get", lambda key: None)
    chunks = [{"content": f"Evidence {i}", "metadata": {}} for i in range(8)]
    with pytest.raises(ProviderRateLimitError):
        grader.grader_node({"contextualized_query": "Revenue?", "reranked_chunks": chunks})
    assert model.with_structured_output.return_value.invoke.call_count == 1
