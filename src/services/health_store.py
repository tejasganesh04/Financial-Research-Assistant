"""Per-question and per-MCP-call summaries in PostgreSQL, behind the admin Health page.

One row per question (outcome, timings per step, model usage, errors) and one per MCP tool call.
Rows are written after the work finishes, never in its way, and kept METRICS_RETENTION_DAYS.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config.settings import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS question_metrics (
    request_id text PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL DEFAULT now(),
    username text,
    role text,
    thread_id text,
    channel text NOT NULL DEFAULT 'web',
    outcome text NOT NULL,
    total_ms integer NOT NULL DEFAULT 0,
    queue_ms integer NOT NULL DEFAULT 0,
    steps jsonb NOT NULL DEFAULT '{}'::jsonb,
    models jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_calls integer NOT NULL DEFAULT 0,
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost_usd double precision NOT NULL DEFAULT 0,
    search_retries integer NOT NULL DEFAULT 0,
    answer_revisions integer NOT NULL DEFAULT 0,
    provider_retries integer NOT NULL DEFAULT 0,
    sources integer,
    confidence double precision,
    error_code text,
    error_step text,
    error_detail text,
    traced boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS question_metrics_started ON question_metrics (started_at DESC);
ALTER TABLE question_metrics ADD COLUMN IF NOT EXISTS provider_retries integer NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS mcp_calls (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    tool text NOT NULL,
    username text,
    role text,
    status text NOT NULL,
    duration_ms integer NOT NULL DEFAULT 0,
    results integer,
    error text
);
CREATE INDEX IF NOT EXISTS mcp_calls_at ON mcp_calls (at DESC);
"""

# Background writes still running; kept so they are not garbage-collected mid-flight.
_pending: set[asyncio.Task] = set()


async def ensure_schema(pool) -> None:
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('fra_health_schema'))")
            for statement in filter(str.strip, SCHEMA.split(";")):
                await conn.execute(statement)


async def purge(pool) -> None:
    days = max(settings.METRICS_RETENTION_DAYS, 1)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM question_metrics WHERE started_at < now() - make_interval(days => %s)", (days,))
        await conn.execute("DELETE FROM mcp_calls WHERE at < now() - make_interval(days => %s)", (days,))


async def record_question(
    pool, metrics: dict, *, username: str | None, role: str | None, channel: str = "web", traced: bool = False
) -> None:
    """Store one question's summary; a failure is logged and never reaches the person asking."""
    finished = datetime.now(timezone.utc)
    started = finished - timedelta(milliseconds=metrics.get("total_ms") or 0)
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """INSERT INTO question_metrics (request_id, started_at, finished_at, username, role, thread_id,
                       channel, outcome, total_ms, queue_ms, steps, models, model_calls, input_tokens,
                       output_tokens, cost_usd, search_retries, answer_revisions, provider_retries, sources,
                       confidence, error_code, error_step, error_detail, traced)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s)
                   ON CONFLICT (request_id) DO NOTHING""",
                (
                    metrics["request_id"],
                    started,
                    finished,
                    username,
                    role,
                    metrics.get("conversation_id"),
                    channel,
                    metrics.get("outcome") or "unknown",
                    metrics.get("total_ms") or 0,
                    metrics.get("queue_ms") or 0,
                    Jsonb(metrics.get("steps") or {}),
                    Jsonb(metrics.get("models") or {}),
                    metrics.get("model_calls") or 0,
                    metrics.get("input_tokens") or 0,
                    metrics.get("output_tokens") or 0,
                    metrics.get("cost_usd") or 0.0,
                    metrics.get("search_retries") or 0,
                    metrics.get("answer_revisions") or 0,
                    metrics.get("provider_retries") or 0,
                    metrics.get("sources"),
                    metrics.get("confidence"),
                    metrics.get("error_code"),
                    metrics.get("error_step"),
                    (metrics.get("error_detail") or "")[:300] or None,
                    traced,
                ),
            )
    except Exception:
        logger.warning("Could not store question metrics", exc_info=True)


async def record_mcp_call(
    pool,
    *,
    tool: str,
    username: str | None,
    role: str | None,
    status: str,
    duration_ms: int,
    results: int | None = None,
    error: str | None = None,
) -> None:
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO mcp_calls (tool, username, role, status, duration_ms, results, error)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (tool, username, role, status, duration_ms, results, (error or "")[:200] or None),
            )
    except Exception:
        logger.warning("Could not store MCP call", exc_info=True)


def in_background(coroutine) -> None:
    """Run a write after the response, without holding up the caller."""
    try:
        task = asyncio.get_running_loop().create_task(coroutine)
    except RuntimeError:  # no running loop (a synchronous caller): skip quietly
        coroutine.close()
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for this event loop's background writes (tests, and shutdown)."""
    loop = asyncio.get_running_loop()
    while True:
        for task in list(_pending):
            if task.get_loop().is_closed():
                _pending.discard(task)  # its loop is gone; it can never finish
        mine = [task for task in _pending if task.get_loop() is loop and not task.done()]
        if not mine:
            return
        await asyncio.gather(*mine, return_exceptions=True)


# --- Reading -------------------------------------------------------------------------------------

# Each range is whole hours or days in the viewer's time zone, ending with the current one.
RANGES = {"24h": (24, "hour"), "7d": (7, "day"), "30d": (30, "day")}


def range_start(range_key: str, tz: str, now: datetime | None = None) -> datetime:
    count, unit = RANGES[range_key]
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    if unit == "hour":
        return local.replace(minute=0, second=0, microsecond=0) - timedelta(hours=count - 1)
    first = local.date() - timedelta(days=count - 1)
    return datetime.combine(first, time(0), tzinfo=ZoneInfo(tz))


async def _rows(conn, sql: str, params: dict) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def summary(pool, range_key: str, tz: str) -> dict[str, Any]:
    """Everything the Health page charts, for one time range, bucketed in the viewer's time zone."""
    unit = RANGES[range_key][1]
    now = datetime.now(timezone.utc)
    since = range_start(range_key, tz, now)
    params = {"since": since, "before": since - (now - since), "now": now, "tz": tz, "unit": unit}
    async with pool.connection() as conn:
        # Buckets run from the start of the first hour/day in the viewer's zone up to now.
        series = await _rows(
            conn,
            """WITH buckets AS (
                   SELECT generate_series(
                       date_trunc(%(unit)s, %(since)s AT TIME ZONE %(tz)s),
                       date_trunc(%(unit)s, %(now)s AT TIME ZONE %(tz)s),
                       CASE WHEN %(unit)s = 'hour' THEN interval '1 hour' ELSE interval '1 day' END) AS bucket
               ),
               q AS (
                   SELECT date_trunc(%(unit)s, started_at AT TIME ZONE %(tz)s) AS bucket,
                          count(*) AS questions,
                          count(*) FILTER (WHERE outcome IN ('failed', 'rate_limited')) AS problems,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms)
                              FILTER (WHERE outcome NOT IN ('failed', 'rate_limited', 'interrupted', 'cancelled'))
                              AS p50_ms
                   FROM question_metrics WHERE started_at >= %(since)s GROUP BY 1
               ),
               m AS (
                   SELECT date_trunc(%(unit)s, at AT TIME ZONE %(tz)s) AS bucket, count(*) AS calls
                   FROM mcp_calls WHERE at >= %(since)s GROUP BY 1
               )
               SELECT to_char(b.bucket, 'YYYY-MM-DD"T"HH24:MI:SS') AS t,
                      coalesce(q.questions, 0) AS questions, coalesce(q.problems, 0) AS problems,
                      q.p50_ms, coalesce(m.calls, 0) AS mcp_calls
               FROM buckets b LEFT JOIN q USING (bucket) LEFT JOIN m USING (bucket) ORDER BY b.bucket""",
            params,
        )
        totals = (
            await _rows(
                conn,
                """SELECT count(*) AS questions,
                          count(*) FILTER (WHERE outcome NOT IN ('failed', 'rate_limited', 'interrupted', 'cancelled'))
                              AS completed,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms)
                              FILTER (WHERE outcome NOT IN ('failed', 'rate_limited', 'interrupted', 'cancelled'))
                              AS p50_ms,
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms)
                              FILTER (WHERE outcome NOT IN ('failed', 'rate_limited', 'interrupted', 'cancelled'))
                              AS p95_ms,
                          avg(queue_ms) FILTER (WHERE queue_ms > 0) AS queue_avg_ms,
                          count(*) FILTER (WHERE queue_ms > 0) AS queued,
                          count(DISTINCT username) AS people,
                          avg(model_calls) FILTER (WHERE model_calls > 0) AS calls_per_question,
                          avg(input_tokens + output_tokens) FILTER (WHERE model_calls > 0) AS tokens_per_question,
                          sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                          avg(confidence) FILTER (WHERE outcome = 'answered' AND confidence IS NOT NULL)
                              AS confidence,
                          avg(sources) FILTER (WHERE outcome = 'answered' AND sources IS NOT NULL) AS sources,
                          avg((search_retries > 0)::int) FILTER (WHERE outcome IN ('answered', 'awaiting_approval'))
                              AS search_retry_share,
                          avg((answer_revisions > 0)::int) FILTER (WHERE outcome IN ('answered', 'awaiting_approval'))
                              AS revision_share,
                          avg((provider_retries > 0)::int) FILTER (WHERE outcome IN ('answered', 'awaiting_approval'))
                              AS throttled_share
                   FROM question_metrics WHERE started_at >= %(since)s""",
                params,
            )
        )[0]
        previous = (
            await _rows(
                conn,
                "SELECT count(*) AS questions FROM question_metrics"
                " WHERE started_at >= %(before)s AND started_at < %(since)s",
                params,
            )
        )[0]["questions"]
        outcomes = await _rows(
            conn,
            "SELECT outcome, count(*) AS n FROM question_metrics WHERE started_at >= %(since)s"
            " GROUP BY outcome ORDER BY n DESC",
            params,
        )
        # Time in each step per question, over questions that ran the whole pipeline.
        steps = await _rows(
            conn,
            """WITH recent AS (
                   SELECT steps FROM question_metrics
                   WHERE started_at >= %(since)s AND outcome IN ('answered', 'awaiting_approval')
                   ORDER BY started_at DESC LIMIT 2000
               )
               SELECT s.key AS step, sum((s.value->>'ms')::bigint) AS total_ms, count(*) AS questions,
                      (SELECT count(*) FROM recent) AS of_questions
               FROM recent, jsonb_each(recent.steps) s
               GROUP BY s.key""",
            params,
        )
        models = await _rows(
            conn,
            """SELECT m.key AS model, max(m.value->>'kind') AS kind, sum((m.value->>'calls')::int) AS calls,
                      sum((m.value->>'input')::bigint) AS input, sum((m.value->>'output')::bigint) AS output,
                      sum((m.value->>'cost')::double precision) AS cost, sum((m.value->>'errors')::int) AS errors,
                      count(DISTINCT q.request_id) AS questions
               FROM question_metrics q, jsonb_each(q.models) m
               WHERE q.started_at >= %(since)s GROUP BY m.key ORDER BY calls DESC""",
            params,
        )
        # Daily allowances refill continuously, so "today" means the last 24 hours.
        groq_day = await _rows(
            conn,
            """SELECT m.key AS model,
                      sum((m.value->>'input')::bigint + (m.value->>'output')::bigint) AS tokens,
                      count(DISTINCT q.request_id) AS questions
               FROM question_metrics q, jsonb_each(q.models) m
               WHERE q.started_at >= %(now)s - interval '24 hours' AND m.value->>'kind' = 'llm'
               GROUP BY m.key""",
            params,
        )
        per_question = await _rows(
            conn,
            """SELECT m.key AS model,
                      avg((m.value->>'input')::bigint + (m.value->>'output')::bigint) AS tokens
               FROM (SELECT models FROM question_metrics
                     WHERE outcome IN ('answered', 'awaiting_approval') AND model_calls > 0
                     ORDER BY started_at DESC LIMIT 50) q, jsonb_each(q.models) m
               WHERE m.value->>'kind' = 'llm' GROUP BY m.key""",
            params,
        )
        mcp = (
            await _rows(
                conn,
                """SELECT count(*) AS calls, count(DISTINCT username) AS people,
                          count(*) FILTER (WHERE tool = 'search_documents') AS searches,
                          count(*) FILTER (WHERE tool = 'read_pages') AS page_reads,
                          count(*) FILTER (WHERE tool = 'list_documents') AS listings,
                          count(*) FILTER (WHERE status = 'failed') AS failed,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
                              FILTER (WHERE tool = 'search_documents' AND status = 'ok') AS search_p50_ms
                   FROM mcp_calls WHERE at >= %(since)s""",
                params,
            )
        )[0]
        problems = await _rows(
            conn,
            """(SELECT 'question' AS kind, finished_at AS at, request_id, username, outcome AS status,
                       error_code, error_step, error_detail, traced, NULL AS tool
                FROM question_metrics WHERE started_at >= %(since)s AND outcome IN ('failed', 'rate_limited')
                ORDER BY finished_at DESC LIMIT 8)
               UNION ALL
               (SELECT 'mcp', at, NULL, username, status, NULL, NULL, error, false, tool
                FROM mcp_calls WHERE at >= %(since)s AND status = 'failed' ORDER BY at DESC LIMIT 8)
               ORDER BY at DESC LIMIT 8""",
            params,
        )
        problem_count = (
            await _rows(
                conn,
                """SELECT (SELECT count(*) FROM question_metrics
                           WHERE started_at >= %(since)s AND outcome IN ('failed', 'rate_limited'))
                        + (SELECT count(*) FROM mcp_calls WHERE at >= %(since)s AND status = 'failed') AS n""",
                params,
            )
        )[0]["n"]
        recent = await _rows(
            conn,
            "SELECT outcome, finished_at, error_code, error_detail FROM question_metrics"
            " WHERE started_at >= now() - interval '2 hours' ORDER BY finished_at DESC LIMIT 5",
            params,
        )
    return {
        "series": series,
        "totals": totals,
        "previous_questions": previous,
        "outcomes": outcomes,
        "steps": steps,
        "models": models,
        "groq_day": groq_day,
        "per_question": per_question,
        "mcp": mcp,
        "problems": problems,
        "problem_count": problem_count,
        "recent": recent,
    }


async def database_size(pool) -> int | None:
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT pg_database_size(current_database())")
            return (await cur.fetchone())[0]
    except Exception:
        return None
