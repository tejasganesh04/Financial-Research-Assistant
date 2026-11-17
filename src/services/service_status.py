"""What the outside services last told us, kept in memory for the admin Health page.

Nothing here makes a call of its own: Groq's rate-limit headers are read from the replies to
questions the app was answering anyway, and Voyage's state comes from real searches. A restart
forgets everything until the next question.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

_lock = threading.Lock()
STARTED_AT = datetime.now(timezone.utc)


@dataclass
class GroqModelLimits:
    requests_limit: int | None = None  # per day (Groq's x-ratelimit-*-requests)
    requests_remaining: int | None = None
    tokens_minute_limit: int | None = None  # per minute (Groq's x-ratelimit-*-tokens)
    tokens_minute_remaining: int | None = None
    updated_at: datetime | None = None
    # Groq reports daily token use only when it refuses a call: "Limit 200000, Used 199012".
    daily_limit: int | None = None
    daily_used: int | None = None
    daily_reported_at: datetime | None = None
    last_limited_at: datetime | None = None
    retry_after_seconds: int | None = None


@dataclass
class ServiceState:
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    fallback: str | None = None

    @property
    def failing(self) -> bool:
        return self.last_error_at is not None and (self.last_ok_at is None or self.last_error_at > self.last_ok_at)


@dataclass
class _Status:
    groq: dict[str, GroqModelLimits] = field(default_factory=dict)
    services: dict[str, ServiceState] = field(default_factory=dict)


_status = _Status()


def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


_DAILY = re.compile(r"tokens per day \(TPD\): Limit (\d+), Used (\d+)", re.IGNORECASE)
_WAIT = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)


def _model_of(request) -> str | None:
    try:
        return json.loads(request.content or b"{}").get("model")
    except (ValueError, AttributeError, TypeError):
        return None


def note_groq_response(model: str | None, status_code: int, headers, body: str | None = None) -> None:
    """Remember the limits Groq sent with a reply (any reply, including refusals)."""
    if not model:
        return
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    now = datetime.now(timezone.utc)
    with _lock:
        limits = _status.groq.setdefault(model, GroqModelLimits())
        if "x-ratelimit-limit-requests" in headers:
            limits.requests_limit = _int(headers.get("x-ratelimit-limit-requests"))
            limits.requests_remaining = _int(headers.get("x-ratelimit-remaining-requests"))
            limits.tokens_minute_limit = _int(headers.get("x-ratelimit-limit-tokens"))
            limits.tokens_minute_remaining = _int(headers.get("x-ratelimit-remaining-tokens"))
            limits.updated_at = now
        if status_code == 429:
            limits.last_limited_at = now
            wait = _int(headers.get("retry-after"))
            match = _DAILY.search(body or "")
            if match:
                limits.daily_limit, limits.daily_used = int(match.group(1)), int(match.group(2))
                limits.daily_reported_at = now
            if wait is None and body:
                found = _WAIT.search(body)
                if found and any(found.groups()):
                    hours, minutes, seconds = (float(g) if g else 0.0 for g in found.groups())
                    wait = int(hours * 3600 + minutes * 60 + seconds + 0.999)
            limits.retry_after_seconds = wait
    if status_code == 429:
        # The SDK waits and retries by itself; note it on the question being answered, if any.
        from src.services.request_trace import record

        record("provider_retry", provider="Groq", model=model, retry_after_seconds=wait)


def groq_limits() -> dict[str, GroqModelLimits]:
    with _lock:
        return {model: GroqModelLimits(**vars(limits)) for model, limits in _status.groq.items()}


def groq_event_hooks() -> tuple[dict, dict]:
    """httpx event hooks for Groq's sync and async clients."""

    def on_response(response):
        if response.status_code == 429:
            response.read()
        note_groq_response(_model_of(response.request), response.status_code, response.headers, _text(response))

    async def on_response_async(response):
        if response.status_code == 429:
            await response.aread()
        note_groq_response(_model_of(response.request), response.status_code, response.headers, _text(response))

    return {"response": [on_response]}, {"response": [on_response_async]}


def _text(response) -> str | None:
    if response.status_code != 429:
        return None
    try:
        return response.text[:2000]
    except Exception:  # noqa: BLE001 - a body we cannot read only loses the daily figure
        return None


def note_ok(service: str) -> None:
    with _lock:
        state = _status.services.setdefault(service, ServiceState())
        state.last_ok_at = datetime.now(timezone.utc)
        state.fallback = None


def note_failure(service: str, error: BaseException | str, fallback: str | None = None) -> None:
    with _lock:
        state = _status.services.setdefault(service, ServiceState())
        state.last_error_at = datetime.now(timezone.utc)
        state.last_error = error if isinstance(error, str) else type(error).__name__
        state.fallback = fallback


def service(name: str) -> ServiceState:
    with _lock:
        state = _status.services.get(name)
        return ServiceState(**vars(state)) if state else ServiceState()


def uptime_seconds() -> int:
    return int((datetime.now(timezone.utc) - STARTED_AT).total_seconds())


def reset() -> None:
    """For tests."""
    with _lock:
        _status.groq.clear()
        _status.services.clear()


def langsmith_enabled() -> bool:
    """Whether LangChain will send traces to LangSmith (both the switch and a key are needed)."""
    import os

    switch = os.environ.get("LANGCHAIN_TRACING_V2") or os.environ.get("LANGSMITH_TRACING") or ""
    key = os.environ.get("LANGCHAIN_API_KEY") or os.environ.get("LANGSMITH_API_KEY")
    return switch.lower() == "true" and bool(key)
