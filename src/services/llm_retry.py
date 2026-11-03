"""Retry transient model failures with bounded backoff.

Temporary connection, timeout and service errors can be retried. Provider rate
limits surface immediately so the API can report the model and workflow stage."""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from src.services.provider_errors import is_rate_limited

T = TypeVar("T")

logger = logging.getLogger(__name__)

# Default policy. Tuned for OpenAI tier-1 burst behavior (most transient
# hiccups recover within the first 2-4s window).
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (2.0, 4.0, 8.0)

# Substrings that mark an exception as transient (case-insensitive). We match
# on str(exc) rather than exception type so we catch transient errors regardless
# of which client library wraps them (httpx, openai, langchain all surface
# these slightly differently).
_TRANSIENT_MARKERS = (
    "connection error",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connectionerror",
    "remote disconnected",
    "remoteprotocolerror",
    "read timeout",
    "readtimeout",
    "request timed out",
    "timed out",
    "timeout",
    "rate limit",
    "ratelimit",
    "rate_limit",
    "429",
    "500 server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "internal server error",
    "service unavailable",
    "gateway timeout",
    "ssl",
    "eof occurred",
    "broken pipe",
    "apiconnectionerror",
    "apitimeouterror",
)


def is_transient_error(exc: BaseException) -> bool:
    """Return True if the exception looks like a retryable transient failure."""
    msg = (str(exc) or "").lower()
    type_name = type(exc).__name__.lower()
    if any(m in msg for m in _TRANSIENT_MARKERS):
        return True
    if any(m in type_name for m in ("timeout", "connection", "ratelimit", "apierror")):
        return True
    return False


def retry_llm_call(
    fn: Callable[[], T],
    *,
    label: str = "llm call",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
) -> T:
    """Call `fn()` with bounded retry-on-transient-failure.

    Args:
        fn: Zero-arg callable that performs the LLM invocation. Must be safe
            to call repeatedly (no externally-visible side-effects between
            attempts).
        label: Short human-readable identifier used in retry log lines, so
            ops can distinguish "grader chunk 3" retries from "query rewriter"
            retries.
        max_attempts: Total attempts including the first call.
        backoff_seconds: Per-retry sleep durations. If shorter than
            `max_attempts - 1`, the last value repeats.

    Returns:
        Whatever `fn()` returns on success.

    Raises:
        The original exception if all attempts fail, OR a non-transient
        exception immediately on first occurrence.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            # Surface provider limits instead of repeatedly calling an exhausted API.
            if is_rate_limited(exc):
                raise
            last_exc = exc
            if not is_transient_error(exc):
                # Permanent failure — don't waste time retrying.
                raise
            if attempt >= max_attempts - 1:
                # Out of retries.
                logger.warning(
                    f"{label}: transient failure on attempt {attempt + 1}/{max_attempts}, "
                    f"giving up: {type(exc).__name__}: {str(exc)[:200]}"
                )
                raise
            delay = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
            logger.warning(
                f"{label}: transient failure on attempt {attempt + 1}/{max_attempts}, "
                f"retrying in {delay:.1f}s: {type(exc).__name__}: {str(exc)[:200]}"
            )
            time.sleep(delay)
    # Defensive — loop should always either return or raise.
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label}: retry loop exited without returning or raising")
