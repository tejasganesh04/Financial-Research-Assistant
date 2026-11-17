"""Groq's limits and Voyage's state, as remembered for the Health page."""

import json

import httpx
import pytest

from src.services import service_status


@pytest.fixture(autouse=True)
def fresh():
    service_status.reset()
    yield
    service_status.reset()


HEADERS = {
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-remaining-requests": "987",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "6512",
}


def test_reply_headers_are_remembered_per_model():
    service_status.note_groq_response("openai/gpt-oss-20b", 200, HEADERS)
    limits = service_status.groq_limits()["openai/gpt-oss-20b"]
    assert (limits.requests_limit, limits.requests_remaining) == (1000, 987)
    assert (limits.tokens_minute_limit, limits.tokens_minute_remaining) == (8000, 6512)
    assert limits.updated_at is not None
    assert limits.last_limited_at is None


def test_a_daily_refusal_records_groq_s_own_usage_and_wait():
    body = json.dumps(
        {
            "error": {
                "message": "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service "
                "tier `on_demand` on tokens per day (TPD): Limit 200000, Used 199012, Requested 2410. "
                "Please try again in 7m2.5s.",
                "code": "rate_limit_exceeded",
            }
        }
    )
    service_status.note_groq_response("openai/gpt-oss-120b", 429, {}, body)
    limits = service_status.groq_limits()["openai/gpt-oss-120b"]
    assert (limits.daily_limit, limits.daily_used) == (200000, 199012)
    assert limits.retry_after_seconds == 423
    assert limits.last_limited_at is not None


def test_retry_after_header_wins_over_the_message():
    service_status.note_groq_response("m", 429, {"retry-after": "12"}, "Please try again in 5m")
    assert service_status.groq_limits()["m"].retry_after_seconds == 12


def test_replies_without_a_model_are_ignored():
    service_status.note_groq_response(None, 200, HEADERS)
    assert service_status.groq_limits() == {}


def _respond(request):
    status = 429 if b"busy" in request.content else 200
    body = b'{"error":{"message":"tokens per day (TPD): Limit 200000, Used 5"}}' if status == 429 else b"{}"
    return httpx.Response(status, headers=HEADERS, content=body)


def test_sync_client_hook_reads_the_model_from_the_request():
    hooks, _ = service_status.groq_event_hooks()
    with httpx.Client(transport=httpx.MockTransport(_respond), event_hooks=hooks) as client:
        client.post("https://api.groq.com/x", json={"model": "openai/gpt-oss-20b"})
        client.post("https://api.groq.com/x", json={"model": "busy-model"})
    limits = service_status.groq_limits()
    assert limits["openai/gpt-oss-20b"].requests_remaining == 987
    assert limits["busy-model"].daily_used == 5


async def test_async_client_hook_reads_refusal_bodies():
    _, hooks = service_status.groq_event_hooks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(_respond), event_hooks=hooks) as client:
        response = await client.post("https://api.groq.com/x", json={"model": "busy-model"})
    # The body stays readable for the SDK after the hook has read it.
    assert "TPD" in response.text
    assert service_status.groq_limits()["busy-model"].daily_limit == 200000


def test_groq_clients_built_by_the_factory_carry_the_hooks(monkeypatch):
    from src.config.settings import settings
    from src.services.llm_factory import _groq

    monkeypatch.setattr(settings, "GROQ_API_KEY", "offline")
    model = _groq("openai/gpt-oss-20b")
    assert model.http_client.event_hooks["response"]
    assert model.http_async_client.event_hooks["response"]


def test_a_service_is_failing_until_it_works_again():
    service_status.note_failure("voyage_rerank", TimeoutError(), fallback="search order")
    state = service_status.service("voyage_rerank")
    assert state.failing and state.last_error == "TimeoutError" and state.fallback == "search order"
    service_status.note_ok("voyage_rerank")
    state = service_status.service("voyage_rerank")
    assert not state.failing and state.fallback is None


def test_langsmith_needs_both_the_switch_and_a_key(monkeypatch):
    for name in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_API_KEY", "LANGSMITH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    assert not service_status.langsmith_enabled()
    monkeypatch.setenv("LANGCHAIN_API_KEY", "key")
    assert service_status.langsmith_enabled()
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    assert not service_status.langsmith_enabled()


def test_reranker_failures_are_recorded_with_the_fallback(monkeypatch):
    from src.config.settings import settings
    from src.services import reranker_service

    monkeypatch.setattr(settings, "RERANKER_PROVIDER", "voyage")
    monkeypatch.setattr(settings, "RERANKER_LOCAL_FALLBACK", False)

    def broken(query, documents):
        raise ConnectionError("down")

    monkeypatch.setattr(reranker_service, "voyage_scores", broken)
    scores, model = reranker_service.score("q", ["a", "b"])
    assert model == reranker_service.SEARCH_ORDER and scores == [0.0, 0.0]
    assert service_status.service("voyage_rerank").fallback == "search order"

    monkeypatch.setattr(reranker_service, "voyage_scores", lambda q, d: [0.2, 0.9])
    reranker_service.score("q", ["a", "b"])
    assert not service_status.service("voyage_rerank").failing


async def test_draining_ignores_writes_left_on_another_event_loop():
    import asyncio

    from src.services import health_store

    other = asyncio.new_event_loop()
    stranded = other.create_task(asyncio.sleep(3600))
    health_store._pending.add(stranded)
    other.close()  # the task can never run now, as after a test's loop ends
    done = []

    async def write():
        done.append(True)

    health_store.in_background(write())
    await asyncio.wait_for(health_store.drain(), timeout=5)
    assert done == [True]
    assert stranded not in health_store._pending
