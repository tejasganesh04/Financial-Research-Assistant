"""The recorder's per-question summary (stored for the Health page) and its file switch."""

from uuid import uuid4

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.services.request_trace import RequestTrace, record_approval, trace_root


def make_trace():
    return RequestTrace("conversation-1", "Microsoft revenue?", "analyst", get_permissions("analyst"))


def step(trace, name, failed=False):
    run = uuid4()
    trace.on_chain_start({}, {}, run_id=run, name=name)
    if failed:
        trace.on_chain_error(RuntimeError("boom"), run_id=run)
    else:
        trace.on_chain_end({}, run_id=run)


def test_summary_counts_steps_retries_models_and_the_answer():
    trace = make_trace()
    for name in ("guardrails", "retrieval", "grader", "retrieval", "generator", "generator"):
        step(trace, name)
    for tokens in (100, 300):
        trace.record("model_call", model="openai/gpt-oss-20b", input_tokens=tokens, output_tokens=10, cost_usd=None)
    trace.record("model_call", model="openai/gpt-oss-120b", input_tokens=50, output_tokens=5, error_type="Timeout")
    trace.record("rerank_call", model="rerank-3", input_tokens=900, output_tokens=0, cost_usd=0.000045)
    trace.record("embedding_call", model="voyage-finance-2", input_tokens=12, output_tokens=0, cost_usd=0.0000014)
    trace.record("queue_wait", duration_s=1.25)
    trace.record("answer_ready", sources=3, confidence=0.91)
    trace.finish("answered")
    m = trace.metrics()
    assert m["request_id"] == trace.request_id and m["conversation_id"] == "conversation-1"
    assert m["outcome"] == "answered"
    assert m["steps"]["retrieval"]["runs"] == 2 and m["steps"]["generator"]["runs"] == 2
    assert (m["search_retries"], m["answer_revisions"]) == (1, 1)
    assert m["models"]["openai/gpt-oss-20b"] == {
        "kind": "llm",
        "calls": 2,
        "input": 400,
        "output": 20,
        "cost": None,
        "errors": 0,
    }
    assert m["models"]["openai/gpt-oss-120b"]["errors"] == 1
    assert m["models"]["rerank-3"]["kind"] == "rerank"
    assert m["models"]["voyage-finance-2"]["kind"] == "embedding"
    # Model calls and tokens count the language models only.
    assert (m["model_calls"], m["input_tokens"], m["output_tokens"]) == (3, 450, 25)
    assert m["queue_ms"] == 1250
    assert (m["sources"], m["confidence"]) == (3, 0.91)
    assert m["total_ms"] >= 0 and m["error_code"] is None


def test_a_provider_limit_becomes_a_rate_limited_outcome_with_its_step():
    trace = make_trace()
    step(trace, "grader", failed=True)
    trace.record(
        "provider_limit",
        code="provider_rate_limited",
        provider="Groq",
        model="openai/gpt-oss-20b",
        node="grader",
        retry_after_seconds=360,
    )
    trace.finish("failed")
    m = trace.metrics()
    assert m["outcome"] == "rate_limited"
    assert (m["error_code"], m["error_step"]) == ("provider_rate_limited", "grader")
    assert m["error_detail"] == "Groq · openai/gpt-oss-20b · retry after 360 s"


def test_a_failed_step_is_named_when_there_is_no_provider_limit():
    trace = make_trace()
    step(trace, "retrieval", failed=True)
    trace.finish("failed")
    m = trace.metrics()
    assert m["outcome"] == "failed" and m["error_step"] == "retrieval" and m["error_code"] is None


def test_sinks_run_once_with_the_finished_trace():
    trace = make_trace()
    seen = []
    trace.sinks.append(lambda t: seen.append(t.metrics()["outcome"]))
    trace.finish("answered")
    trace.finish("failed")
    assert seen == ["answered"]


def test_a_failing_sink_does_not_stop_the_others():
    trace = make_trace()
    seen = []
    trace.sinks.append(lambda t: 1 / 0)
    trace.sinks.append(lambda t: seen.append(True))
    trace.finish("answered")
    assert seen == [True]


def test_with_files_off_nothing_is_written(monkeypatch):
    monkeypatch.setattr(settings, "REQUEST_TRACE_FILES", False)
    trace = make_trace()
    step(trace, "generator")
    trace.finish("answered")
    record_approval("conversation-1", decision="approved")
    assert not trace.directory.exists()
    assert not trace_root().exists()
    # The summary is still complete.
    assert trace.metrics()["steps"]["generator"]["runs"] == 1


def test_with_files_on_reports_are_still_written():
    trace = make_trace()
    step(trace, "generator")
    trace.finish("answered")
    assert (trace.directory / "events.jsonl").is_file()
    assert (trace.directory / "summary.md").is_file()
