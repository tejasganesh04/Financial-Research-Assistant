"""The admin Health endpoint over a real database: summaries, ranges, problems, access and cleanup.

Runs against the isolated TEST_POSTGRES_URL database (skipped without it); no model calls.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.api.routes import admin_health
from src.config.settings import settings
from src.services import health_store, service_status, user_store
from tests.integration.test_accounts import PASSWORD, bearer, client, sign_in


@pytest.fixture
async def health_db(auth_pool, monkeypatch):
    for username, role in (("admin", "admin"), ("analyst", "analyst"), ("hr", "hr")):
        await user_store.create_account(auth_pool, username, username.title(), role, PASSWORD, "Dept")
    async with auth_pool.connection() as conn:
        await conn.execute("DROP TABLE IF EXISTS question_metrics, mcp_calls CASCADE")
    await health_store.ensure_schema(auth_pool)
    service_status.reset()
    admin_health._langsmith_cache.update(at=None, value=None)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setattr(settings, "FORCE_GROQ_ONLY", True)
    monkeypatch.setattr(settings, "GROQ_API_KEY", "offline")
    monkeypatch.setattr(settings, "BACKUP_STATUS_FILE", "")
    # Qdrant is not part of the test stack; the search check is covered separately.
    monkeypatch.setattr(admin_health, "_qdrant_points", lambda: 41233)
    yield auth_pool
    service_status.reset()


def metrics(request_id, outcome="answered", total_ms=18000, **extra):
    base = {
        "request_id": request_id,
        "conversation_id": "thread-" + request_id,
        "outcome": outcome,
        "total_ms": total_ms,
        "queue_ms": 0,
        "steps": {
            "guardrails": {"ms": 1000, "runs": 1},
            "retrieval": {"ms": 3000, "runs": 1},
            "grader": {"ms": 8000, "runs": 1},
            "generator": {"ms": 4000, "runs": 1},
        },
        "models": {
            "openai/gpt-oss-20b": {
                "kind": "llm",
                "calls": 12,
                "input": 9000,
                "output": 1000,
                "cost": None,
                "errors": 0,
            },
            "openai/gpt-oss-120b": {"kind": "llm", "calls": 2, "input": 4000, "output": 500, "cost": None, "errors": 0},
            "rerank-3": {"kind": "rerank", "calls": 1, "input": 20000, "output": 0, "cost": 0.001, "errors": 0},
        },
        "model_calls": 14,
        "input_tokens": 13000,
        "output_tokens": 1500,
        "cost_usd": 0.001,
        "search_retries": 0,
        "answer_revisions": 0,
        "sources": 3,
        "confidence": 0.9,
        "error_code": None,
        "error_step": None,
        "error_detail": None,
    }
    return {**base, **extra}


async def admin_get(path):
    async with client() as c:
        admin = await sign_in(c, "admin")
        return await c.get(path, headers=bearer(admin))


async def test_only_admins_can_open_it_and_bad_input_is_refused(health_db):
    async with client() as c:
        analyst = await sign_in(c)
        assert (await c.get("/v1/admin/health", headers=bearer(analyst))).status_code == 403
        assert (await c.get("/v1/admin/health")).status_code == 401
    assert (await admin_get("/v1/admin/health?range=90d")).status_code == 422
    assert (await admin_get("/v1/admin/health?tz=Mars/Olympus")).status_code == 422


async def test_an_empty_database_gives_a_calm_complete_page(health_db):
    response = await admin_get("/v1/admin/health?range=24h&tz=Asia/Kolkata")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["overall"] == {"state": "ok", "message": "All systems working"}
    assert [c["key"] for c in data["checks"]] == ["website", "answers", "search", "reranker", "backups"]
    assert data["checks"][2]["detail"] == "41,233 passages"
    assert data["checks"][4]["state"] == "off"
    assert len(data["series"]) == 24 and all(p["questions"] == 0 for p in data["series"])
    assert data["questions"]["total"] == 0 and data["latency"]["p50_ms"] is None
    assert data["problems"] == {"total": 0, "recent": []}
    # Groq-only with no history yet: the full allowance, at the measured cost per question.
    assert data["groq"]["questions_left"] == 200_000 // 14_500
    assert data["langsmith"]["enabled"] is False


async def test_questions_outcomes_latency_and_ai_usage_add_up(health_db):
    pool = health_db
    for i, total in enumerate((10000, 20000, 30000, 40000)):
        await health_store.record_question(pool, metrics(f"q{i}", total_ms=total), username="analyst", role="analyst")
    await health_store.record_question(
        pool,
        metrics("q-review", outcome="awaiting_approval", search_retries=1, provider_retries=2),
        username="hr",
        role="hr",
    )
    await health_store.record_question(
        pool,
        metrics(
            "q-limited",
            outcome="rate_limited",
            total_ms=2000,
            error_code="provider_rate_limited",
            error_step="grader",
            error_detail="Groq · openai/gpt-oss-20b · retry after 360 s",
        ),
        username="hr",
        role="hr",
    )
    data = (await admin_get("/v1/admin/health?range=24h")).json()

    q = data["questions"]
    assert (q["total"], q["completed"], q["people"]) == (6, 5, 2)
    assert q["outcomes"] == {"answered": 4, "rate_limited": 1, "awaiting_approval": 1}
    assert sum(p["questions"] for p in data["series"]) == 6
    assert sum(p["problems"] for p in data["series"]) == 1
    # Typical time over completed questions only: 10, 18, 20, 30, 40 s -> 20 s.
    assert data["latency"]["p50_ms"] == 20000
    assert data["latency"]["p95_ms"] > 30000

    steps = {s["label"]: s for s in data["latency"]["steps"]}
    assert steps["Checking relevance"]["avg_ms"] == 8000
    assert list(steps)[0] == "Checking relevance"
    assert abs(sum(s["share"] for s in steps.values()) - 1) < 1e-6

    ai = data["ai"]
    assert ai["tokens_per_question"] == 14500 and ai["calls_per_question"] == 14
    assert ai["confidence"] == 0.9 and ai["sources"] == 3
    assert ai["search_retry_share"] == 0.2  # 1 of the 5 that reached an answer
    assert ai["throttled_share"] == 0.2 and ai["failed_model_calls"] == 0
    assert ai["cost_usd"] == 0  # free tiers are not billed
    models = {m["model"]: m for m in ai["models"]}
    assert models["openai/gpt-oss-20b"]["provider"] == "Groq" and models["openai/gpt-oss-20b"]["calls"] == 72
    assert models["rerank-3"]["provider"] == "Voyage" and models["rerank-3"]["cost_usd"] == 0.0

    # The rate limit is the newest question, so Answers says so and the problem is listed.
    answers = data["checks"][1]
    assert answers["state"] == "warn" and answers["detail"].startswith("Rate-limited")
    assert data["overall"]["state"] == "warn"
    problem = data["problems"]["recent"][0]
    assert problem["title"] == "Rate-limited during relevance check"
    assert problem["request_id"] == "q-limited" and problem["trace_url"] is None

    # Groq used 6 questions' worth of tokens in the last 24 hours.
    groq = {m["model"]: m for m in data["groq"]["models"]}
    assert groq["openai/gpt-oss-20b"]["used_24h"] == 6 * 10000
    assert groq["openai/gpt-oss-20b"]["remaining"] == 200_000 - 60_000


async def test_paid_models_are_costed_per_question(health_db):
    paid = metrics(
        "paid",
        models={
            "claude-sonnet-4-6": {"kind": "llm", "calls": 3, "input": 9000, "output": 900, "cost": 0.0405, "errors": 0}
        },
    )
    await health_store.record_question(health_db, paid, username="analyst", role="analyst")
    ai = (await admin_get("/v1/admin/health")).json()["ai"]
    assert ai["models"][0]["provider"] == "Anthropic"
    assert ai["cost_usd"] == 0.0405 and ai["cost_per_question"] == 0.0405


async def test_ranges_compare_with_the_period_before(health_db):
    pool = health_db
    now = datetime.now(timezone.utc)
    async with pool.connection() as conn:
        for request_id, days in (("today", 0), ("three-days", 3), ("ten-days", 10), ("old", 40)):
            at = now - timedelta(days=days, minutes=5)
            await conn.execute(
                "INSERT INTO question_metrics (request_id, started_at, finished_at, outcome, total_ms)"
                " VALUES (%s, %s, %s, 'answered', 5000)",
                (request_id, at, at),
            )
    week = (await admin_get("/v1/admin/health?range=7d&tz=UTC")).json()
    assert len(week["series"]) == 7
    assert week["questions"]["total"] == 2 and week["questions"]["previous"] == 1
    month = (await admin_get("/v1/admin/health?range=30d&tz=America/New_York")).json()
    assert len(month["series"]) == 30 and month["questions"]["total"] == 3


async def test_mcp_calls_are_counted_and_failures_listed(health_db):
    pool = health_db
    for status in ("ok", "ok", "failed"):
        await health_store.record_mcp_call(
            pool, tool="search_documents", username="analyst", role="analyst", status=status, duration_ms=800
        )
    await health_store.record_mcp_call(
        pool, tool="read_pages", username="admin", role="admin", status="ok", duration_ms=120, results=2
    )
    data = (await admin_get("/v1/admin/health")).json()
    mcp = data["mcp"]
    assert (mcp["calls"], mcp["people"], mcp["searches"], mcp["page_reads"], mcp["failed"]) == (4, 2, 3, 1, 1)
    assert mcp["search_p50_ms"] == 800
    assert data["problems"]["recent"][0]["title"] == "MCP search documents failed"
    assert sum(p["mcp_calls"] for p in data["series"]) == 4


async def test_checks_report_search_reranker_and_backups(health_db, monkeypatch, tmp_path):
    def unreachable():
        raise ConnectionError

    monkeypatch.setattr(admin_health, "_qdrant_points", unreachable)
    monkeypatch.setattr(settings, "RERANKER_PROVIDER", "voyage")
    service_status.note_failure("voyage_rerank", TimeoutError(), fallback="search order")
    status_file = tmp_path / "backup.json"
    monkeypatch.setattr(settings, "BACKUP_STATUS_FILE", str(status_file))

    checks = {c["key"]: c for c in (await admin_get("/v1/admin/health")).json()["checks"]}
    assert checks["search"] == {
        "key": "search",
        "label": "Search",
        "state": "down",
        "detail": "Search database not responding",
    }
    assert checks["reranker"]["state"] == "warn" and "search order" in checks["reranker"]["detail"]
    assert checks["backups"]["detail"] == "No backup recorded yet"

    for hours, state in ((3, "ok"), (30, "warn"), (60, "down")):
        at = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        status_file.write_text(json.dumps({"at": at, "bytes": 2_400_000}))
        data = (await admin_get("/v1/admin/health")).json()
        backups = {c["key"]: c for c in data["checks"]}["backups"]
        assert backups["state"] == state, (hours, backups)
    assert backups["detail"].endswith("2.4 MB")
    # Search down outranks everything else in the headline.
    assert data["overall"] == {"state": "down", "message": "Search: Search database not responding"}


async def test_groq_s_own_daily_figure_lowers_the_estimate(health_db):
    service_status.note_groq_response(
        "openai/gpt-oss-120b", 429, {"retry-after": "300"}, "tokens per day (TPD): Limit 200000, Used 195000"
    )
    data = (await admin_get("/v1/admin/health")).json()
    groq = {m["model"]: m for m in data["groq"]["models"]}
    assert groq["openai/gpt-oss-120b"]["remaining"] <= 5100
    assert groq["openai/gpt-oss-120b"]["limited_now"] is True
    assert data["groq"]["limiting_model"] == "openai/gpt-oss-120b"
    assert data["groq"]["questions_left"] == 0


async def test_old_rows_are_cleaned_up(health_db, monkeypatch):
    pool = health_db
    old = datetime.now(timezone.utc) - timedelta(days=120)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO question_metrics (request_id, started_at, outcome) VALUES ('old', %s, 'answered')", (old,)
        )
        await conn.execute("INSERT INTO mcp_calls (at, tool, status) VALUES (%s, 'read_pages', 'ok')", (old,))
    await health_store.record_question(pool, metrics("new"), username="analyst", role="analyst")
    await health_store.purge(pool)
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM question_metrics")).fetchone())[0] == 1
        assert (await (await conn.execute("SELECT count(*) FROM mcp_calls")).fetchone())[0] == 0


async def test_storing_twice_keeps_one_row_and_a_broken_database_is_harmless(health_db):
    pool = health_db
    await health_store.record_question(pool, metrics("same"), username="a", role="analyst")
    await health_store.record_question(pool, metrics("same"), username="a", role="analyst")
    async with pool.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) FROM question_metrics")).fetchone())[0] == 1
        await conn.execute("DROP TABLE question_metrics")
    # The table is gone: the write is logged and skipped, never raised.
    await health_store.record_question(pool, metrics("lost"), username="a", role="analyst")
    await health_store.record_mcp_call(pool, tool="x", username=None, role=None, status="ok", duration_ms=1)


async def test_a_brief_per_minute_refusal_is_not_reported_as_rate_limited(health_db):
    # Groq's client waits a few seconds and retries these itself; the question still succeeds.
    service_status.note_groq_response("openai/gpt-oss-20b", 429, {"retry-after": "4"}, "tokens per minute (TPM)")
    data = (await admin_get("/v1/admin/health")).json()
    groq = {m["model"]: m for m in data["groq"]["models"]}
    assert groq["openai/gpt-oss-20b"]["limited_now"] is False
    assert data["checks"][1]["state"] == "ok"


async def test_an_older_table_gains_new_columns_at_start_up(health_db):
    pool = health_db
    async with pool.connection() as conn:
        await conn.execute("ALTER TABLE question_metrics DROP COLUMN provider_retries")
    await health_store.ensure_schema(pool)
    await health_store.record_question(pool, metrics("after-upgrade", provider_retries=1), username="a", role="analyst")
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT provider_retries FROM question_metrics WHERE request_id = 'after-upgrade'")
        assert (await cur.fetchone())[0] == 1
