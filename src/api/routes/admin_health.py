"""The admin Health page: is everything up, how is the assistant used, and how is the AI doing.

Everything comes from this server: the per-question summaries the request recorder stores, the
MCP call log, Groq's rate-limit headers on recent replies, and quick checks of Qdrant and the
backup status file. Nothing here calls a model or costs money.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import get_pool, require_admin
from src.config.settings import settings
from src.services import health_store, service_status

router = APIRouter(prefix="/admin", tags=["admin"])

# Pipeline steps, grouped and named the way the page shows them.
STEP_GROUPS = {
    "rbac_gate": "Safety checks",
    "guardrails": "Safety checks",
    "metadata_extractor": "Understanding the question",
    "router": "Understanding the question",
    "retrieval": "Searching filings",
    "context_expander": "Searching filings",
    "query_rewriter": "Searching filings",
    "reranker": "Ranking passages",
    "grader": "Checking relevance",
    "research_agent": "Multi-step research",
    "generator": "Writing the answer",
    "response_formatter": "Writing the answer",
    "hallucination_checker": "Checking the answer",
    "hitl_gate": "Writing the answer",
}
STEP_LABELS = {
    "guardrails": "safety checks",
    "router": "routing",
    "metadata_extractor": "routing",
    "retrieval": "search",
    "reranker": "ranking",
    "grader": "relevance check",
    "query_rewriter": "search",
    "generator": "answer writing",
    "hallucination_checker": "answer check",
    "research_agent": "research",
}
# Measured on the free Groq tier (Nov 2026), used until this server has its own figures.
DEFAULT_TOKENS_PER_QUESTION = {"openai/gpt-oss-20b": 11_700, "openai/gpt-oss-120b": 14_500}

_langsmith_cache: dict[str, Any] = {"at": None, "value": None}


def _provider(model: str, kind: str) -> str:
    if kind in ("embedding", "rerank") or model.startswith(("voyage", "rerank")):
        return "Voyage"
    if model.startswith("claude"):
        return "Anthropic"
    if model in (settings.GROQ_LIGHT_MODEL, settings.GROQ_ANSWER_MODEL) or "/" in model or model.startswith("llama"):
        return "Groq"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "OpenAI"
    return "Other"


def _number(value) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _ago(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    seconds = max(0, int((datetime.now(timezone.utc) - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def _duration(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def _wait(seconds: int | None) -> str:
    if not seconds:
        return "shortly"
    return f"in about {seconds} s" if seconds < 90 else f"in about {math.ceil(seconds / 60)} min"


# --- Checks --------------------------------------------------------------------------------------


def _qdrant_points() -> int:
    from qdrant_client import QdrantClient

    client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=3)
    try:
        info = client.get_collection(settings.QDRANT_COLLECTION)
        return info.points_count or 0
    finally:
        client.close()


async def _search_check() -> dict:
    try:
        points = await asyncio.wait_for(asyncio.to_thread(_qdrant_points), timeout=4)
    except Exception:
        return {"state": "down", "detail": "Search database not responding"}
    if not points:
        return {"state": "down", "detail": "The search collection is empty"}
    embed = service_status.service("voyage_embed")
    if embed.failing and embed.last_error_at and _recent(embed.last_error_at, 3600):
        return {"state": "warn", "detail": f"Voyage embeddings failing ({_ago(embed.last_error_at)})"}
    return {"state": "ok", "detail": f"{points:,} passages", "points": points}


def _recent(moment: datetime, seconds: int) -> bool:
    return (datetime.now(timezone.utc) - moment).total_seconds() <= seconds


def _reranker_check() -> dict:
    if settings.RERANKER_PROVIDER != "voyage":
        return {"state": "ok", "detail": "Local model"}
    state = service_status.service("voyage_rerank")
    if state.failing and state.last_error_at and _recent(state.last_error_at, 3600):
        fallback = f" · using {state.fallback}" if state.fallback else ""
        return {"state": "warn", "detail": f"Voyage unavailable{fallback}"}
    used = f" · used {_ago(state.last_ok_at)}" if state.last_ok_at else ""
    return {"state": "ok", "detail": f"Voyage {settings.VOYAGE_RERANK_MODEL}{used}"}


def _backup_check() -> dict:
    if not settings.BACKUP_STATUS_FILE:
        return {"state": "off", "detail": "Not monitored here"}
    try:
        data = json.loads(Path(settings.BACKUP_STATUS_FILE).read_text())
        at = datetime.fromisoformat(str(data["at"]).replace("Z", "+00:00"))
    except FileNotFoundError:
        return {"state": "warn", "detail": "No backup recorded yet"}
    except Exception:
        return {"state": "warn", "detail": "Backup status unreadable"}
    hours = (datetime.now(timezone.utc) - at).total_seconds() / 3600
    size = data.get("bytes")
    sized = f" · {size / 1e6:.1f} MB" if isinstance(size, (int, float)) else ""
    state = "ok" if hours < 26 else "warn" if hours < 50 else "down"
    return {"state": state, "detail": f"Last {_ago(at)}{sized}", "at": at.isoformat()}


def _langsmith() -> dict:
    project = os.environ.get("LANGCHAIN_PROJECT") or settings.langchain_project_name
    return {"enabled": service_status.langsmith_enabled(), "project": project}


def _langsmith_links(project: str) -> dict | None:
    """The project's web address and the pattern for run links (looked up once, then cached)."""
    cached = _langsmith_cache
    if cached["value"] is not None and cached["value"].get("project") == project:
        return cached["value"]
    if cached["at"] is not None and time.monotonic() - cached["at"] < 300:
        return None  # a recent lookup failed (no traces yet, or LangSmith unreachable)
    cached["at"] = time.monotonic()
    from langsmith import Client
    from langsmith import utils as ls_utils

    client = Client(timeout_ms=4000)
    session = client.read_project(project_name=project)
    host = ls_utils.get_host_url(None, client.api_url)
    base = f"{host}/o/{session.tenant_id}/projects/p/{session.id}"
    cached["value"] = {"project": project, "project_url": base, "run_url": base + "/r/{run_id}?poll=true"}
    return cached["value"]


# --- Groq allowance ------------------------------------------------------------------------------


def _groq(summary: dict) -> dict | None:
    limits = service_status.groq_limits()
    used = {row["model"]: int(row["tokens"] or 0) for row in summary["groq_day"]}
    per_question = {row["model"]: _number(row["tokens"]) for row in summary["per_question"]}
    models = [m for m in {*used, *limits} if _provider(m, "llm") == "Groq"]
    if settings.FORCE_GROQ_ONLY:
        models = sorted({*models, settings.GROQ_LIGHT_MODEL, settings.GROQ_ANSWER_MODEL})
    if not models:
        return None
    now = datetime.now(timezone.utc)
    rows = []
    for model in sorted(models):
        info = limits.get(model, service_status.GroqModelLimits())
        limit = info.daily_limit or settings.GROQ_DAILY_TOKEN_LIMIT
        remaining = limit - used.get(model, 0)
        if info.daily_reported_at and info.daily_used is not None:
            # Groq's own figure covers every use of the key; it refills at limit / 24 h.
            refill = (now - info.daily_reported_at).total_seconds() * limit / 86400
            remaining = min(remaining, (limit - info.daily_used) + refill)
        remaining = int(max(0, min(limit, remaining)))
        cost = per_question.get(model) or DEFAULT_TOKENS_PER_QUESTION.get(model) or 12_000
        # Per-minute refusals are routine and Groq's client retries them within seconds; only a
        # long wait (the daily allowance) means questions are being turned away.
        long_wait = info.retry_after_seconds is None or info.retry_after_seconds > 60
        limited_now = bool(
            info.last_limited_at and long_wait and _recent(info.last_limited_at, max(info.retry_after_seconds or 0, 60))
        )
        rows.append(
            {
                "model": model,
                "daily_limit": limit,
                "used_24h": used.get(model, 0),
                "remaining": remaining,
                "share_left": remaining / limit if limit else None,
                "tokens_per_question": round(cost),
                "questions_left": int(remaining // cost),
                "requests_remaining": info.requests_remaining,
                "requests_limit": info.requests_limit,
                "minute_tokens_remaining": info.tokens_minute_remaining,
                "minute_tokens_limit": info.tokens_minute_limit,
                "limited_now": limited_now,
                "retry_after_seconds": info.retry_after_seconds if limited_now else None,
                "updated_at": info.updated_at.isoformat() if info.updated_at else None,
            }
        )
    # The pipeline needs every model, so the scarcest one decides.
    tightest = min(rows, key=lambda r: r["questions_left"])
    return {
        "models": rows,
        "questions_left": tightest["questions_left"],
        "share_left": tightest["share_left"],
        "limiting_model": tightest["model"],
        "estimated": True,
    }


def _answers_check(summary: dict, groq: dict | None) -> dict:
    if settings.FORCE_GROQ_ONLY and not settings.GROQ_API_KEY:
        return {"state": "down", "detail": "No Groq API key configured"}
    recent = summary["recent"]
    if recent and recent[0]["outcome"] == "rate_limited":
        limited = [r for r in (groq or {}).get("models", []) if r["limited_now"]]
        waits = [r["retry_after_seconds"] for r in limited if r["retry_after_seconds"]]
        if limited or _recent(recent[0]["finished_at"], 600):
            return {"state": "warn", "detail": f"Rate-limited · try again {_wait(max(waits, default=None))}"}
    failures = [r for r in recent if r["outcome"] == "failed"]
    if recent and recent[0]["outcome"] == "failed" and len(failures) >= 2:
        return {"state": "down", "detail": "Recent questions are failing"}
    if groq and groq["questions_left"] == 0:
        return {"state": "warn", "detail": "Groq's daily allowance is used up"}
    if groq and groq["questions_left"] < 2:
        return {"state": "warn", "detail": "Groq's daily allowance is nearly used"}
    return {"state": "ok", "detail": "Answering normally"}


# --- Assembly ------------------------------------------------------------------------------------


def _steps(rows: list[dict]) -> list[dict]:
    groups: dict[str, float] = {}
    of = max((int(r["of_questions"] or 0) for r in rows), default=0)
    for row in rows:
        group = STEP_GROUPS.get(row["step"])
        if group and of:
            groups[group] = groups.get(group, 0) + float(row["total_ms"] or 0) / of
    total = sum(groups.values())
    return [
        {"label": label, "avg_ms": round(ms), "share": ms / total if total else 0}
        for label, ms in sorted(groups.items(), key=lambda item: -item[1])
        if ms >= 1
    ]


def _models(rows: list[dict], questions: int) -> list[dict]:
    out = []
    for row in rows:
        provider = _provider(row["model"], row["kind"])
        out.append(
            {
                "model": row["model"],
                "kind": row["kind"],
                "provider": provider,
                "calls": int(row["calls"] or 0),
                "input_tokens": int(row["input"] or 0),
                "output_tokens": int(row["output"] or 0),
                "errors": int(row["errors"] or 0),
                "questions": int(row["questions"] or 0),
                # Groq's free tier and Voyage's free allowance are not billed; list prices would mislead.
                "cost_usd": _number(row["cost"]) if provider in ("OpenAI", "Anthropic") else 0.0,
                "free_tier": provider in ("Groq", "Voyage"),
            }
        )
    return out


def _problems(rows: list[dict], links: dict | None) -> list[dict]:
    out = []
    for row in rows:
        if row["kind"] == "mcp":
            out.append(
                {
                    "kind": "mcp",
                    "at": row["at"].isoformat(),
                    "title": f"MCP {row['tool'].replace('_', ' ')} failed",
                    "detail": row["error_detail"] or "Search unavailable",
                    "username": row["username"],
                }
            )
            continue
        step = STEP_LABELS.get(row["error_step"] or "", (row["error_step"] or "").replace("_", " "))
        if row["status"] == "rate_limited":
            title = f"Rate-limited during {step}" if step else "Rate-limited"
        else:
            title = f"Failed during {step}" if step else "Question failed"
        out.append(
            {
                "kind": "question",
                "at": row["at"].isoformat(),
                "title": title,
                "detail": row["error_detail"],
                "username": row["username"],
                "request_id": row["request_id"],
                "trace_url": links["run_url"].format(run_id=row["request_id"]) if links and row["traced"] else None,
            }
        )
    return out


def _round(value, digits=0):
    value = _number(value)
    return None if value is None else round(value, digits) if digits else round(value)


@router.get("/health")
async def health(
    range_key: str = Query("24h", alias="range", pattern="^(24h|7d|30d)$"),
    tz: str = Query("UTC", max_length=64),
    pool=Depends(get_pool),
    _admin=Depends(require_admin),
):
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail="Unknown time zone.") from None

    summary, search, database_bytes = await asyncio.gather(
        health_store.summary(pool, range_key, tz), _search_check(), health_store.database_size(pool)
    )
    totals = summary["totals"]
    questions = int(totals["questions"] or 0)
    groq = _groq(summary)
    tracing = _langsmith()
    links = None
    if tracing["enabled"]:
        try:
            links = await asyncio.wait_for(asyncio.to_thread(_langsmith_links, tracing["project"]), timeout=6)
        except Exception:
            links = None
    tracing["project_url"] = links["project_url"] if links else None

    from src.api.main import _git_sha

    uptime = service_status.uptime_seconds()
    checks = [
        {"key": "website", "label": "Website", "state": "ok", "detail": f"Up {_duration(uptime)}"},
        {"key": "answers", "label": "Answers", **_answers_check(summary, groq)},
        {"key": "search", "label": "Search", **search},
        {"key": "reranker", "label": "Reranker", **_reranker_check()},
        {"key": "backups", "label": "Backups", **_backup_check()},
    ]
    worst = next((c for state in ("down", "warn") for c in checks if c["state"] == state), None)
    overall = {
        "state": worst["state"] if worst else "ok",
        "message": f"{worst['label']}: {worst['detail']}" if worst else "All systems working",
    }

    models = _models(summary["models"], questions)
    llm_cost = sum(m["cost_usd"] or 0 for m in models if m["kind"] == "llm")
    completed = int(totals["completed"] or 0)
    mcp = summary["mcp"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "range": range_key,
        "overall": overall,
        "checks": checks,
        "release": {
            "version": _git_sha(),
            "environment": settings.ENVIRONMENT,
            "started_at": service_status.STARTED_AT.isoformat(),
            "database_bytes": database_bytes,
            "queue_size": settings.MAX_CONCURRENT_QUESTIONS,
        },
        "series": [
            {
                "t": row["t"],
                "questions": int(row["questions"]),
                "problems": int(row["problems"]),
                "p50_ms": _round(row["p50_ms"]),
                "mcp_calls": int(row["mcp_calls"]),
            }
            for row in summary["series"]
        ],
        "questions": {
            "total": questions,
            "previous": int(summary["previous_questions"] or 0),
            "completed": completed,
            "people": int(totals["people"] or 0),
            "outcomes": {row["outcome"]: int(row["n"]) for row in summary["outcomes"]},
        },
        "latency": {
            "p50_ms": _round(totals["p50_ms"]),
            "p95_ms": _round(totals["p95_ms"]),
            "queued": int(totals["queued"] or 0),
            "queue_avg_ms": _round(totals["queue_avg_ms"]),
            "steps": _steps(summary["steps"]),
        },
        "ai": {
            "tokens_per_question": _round(totals["tokens_per_question"]),
            "calls_per_question": _round(totals["calls_per_question"], 1),
            "input_tokens": int(totals["input_tokens"] or 0),
            "output_tokens": int(totals["output_tokens"] or 0),
            "cost_usd": round(llm_cost, 4),
            "cost_per_question": round(llm_cost / completed, 4) if completed else None,
            "confidence": _round(totals["confidence"], 2),
            "sources": _round(totals["sources"], 1),
            "search_retry_share": _round(totals["search_retry_share"], 3),
            "revision_share": _round(totals["revision_share"], 3),
            "models": models,
        },
        "groq": groq,
        "mcp": {
            "calls": int(mcp["calls"] or 0),
            "people": int(mcp["people"] or 0),
            "searches": int(mcp["searches"] or 0),
            "page_reads": int(mcp["page_reads"] or 0),
            "listings": int(mcp["listings"] or 0),
            "failed": int(mcp["failed"] or 0),
            "search_p50_ms": _round(mcp["search_p50_ms"]),
        },
        "problems": {"total": int(summary["problem_count"] or 0), "recent": _problems(summary["problems"], links)},
        "langsmith": tracing,
    }
