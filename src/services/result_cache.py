"""Redis caches for query embeddings, reranking, and grading.

Uses database 1 by default so it stays separate from other Redis data. New
entries last seven days unless RAG_RESULT_CACHE_TTL_SECONDS or a per-call TTL
sets another positive expiry. Existing entries keep their original expiry.
Cache failures fall back to computing a fresh result.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Key prefix conventions — change the version segment to invalidate a layer
# without flushing the whole DB.
_KEY_PREFIX = {
    "query-emb": "rag-cache:query-emb:v1:",
    "reranker": "rag-cache:reranker:v1:",
    "grader": "rag-cache:grader:v1:",
}

# Default TTL: 7 days. Production deployments wanting tighter freshness can
# override via the RAG_RESULT_CACHE_TTL_SECONDS env var.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def _cache_ttl(ttl_seconds: int | None) -> int:
    """An explicit override wins; otherwise read the configured positive TTL."""
    if ttl_seconds is None:
        try:
            ttl_seconds = int(os.environ.get("RAG_RESULT_CACHE_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
        except ValueError:
            ttl_seconds = 0
    if ttl_seconds <= 0:
        logger.warning("Invalid result-cache TTL; using the seven-day default")
        return DEFAULT_TTL_SECONDS
    return ttl_seconds


class _CacheClient:
    """Thin wrapper that owns the redis-py connection and exposes get/set
    helpers that never raise. Lazily connects on first call so the import
    of this module is free for code paths that don't touch caching.
    """

    def __init__(self) -> None:
        self._client = None
        self._init_attempted = False
        self._enabled = self._read_enabled_flag()

    @staticmethod
    def _read_enabled_flag() -> bool:
        # Default ON — the caches fail-open so this is safe even when Redis
        # isn't running. Setting RAG_RESULT_CACHE_ENABLED=0 lets you disable
        # all three layers from the env without code changes (useful for
        # debugging suspected cache poisoning).
        v = os.environ.get("RAG_RESULT_CACHE_ENABLED", "1").strip().lower()
        return v not in {"0", "false", "no", ""}

    def _connect(self):
        try:
            import redis

            host = os.environ.get("RESULT_CACHE_REDIS_HOST", "localhost")
            port = int(os.environ.get("RESULT_CACHE_REDIS_PORT", "6379"))
            db = int(os.environ.get("RESULT_CACHE_REDIS_DB", "1"))
            self._client = redis.Redis(
                host=host,
                port=port,
                db=db,
                socket_connect_timeout=0.2,
                socket_timeout=0.5,
                decode_responses=False,  # we encode JSON ourselves
            )
            # Smoke ping — a dead Redis will surface here, not on first use.
            self._client.ping()
            logger.info(
                "Result cache connected to redis://%s:%s db=%s", host, port, db
            )
        except Exception as e:
            logger.warning(
                "Result cache disabled — could not connect to Redis: %s",
                type(e).__name__,
            )
            self._client = None

    def _ensure_client(self):
        if self._init_attempted:
            return
        self._init_attempted = True
        if self._enabled:
            self._connect()

    def get(self, key: str) -> Any | None:
        if not self._enabled:
            return None
        self._ensure_client()
        if self._client is None:
            return None
        try:
            raw = self._client.get(key)
        except Exception as e:
            logger.warning("Result cache GET failed (%s): %s", type(e).__name__, e)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as e:
            logger.warning("Result cache JSON decode failed for %s: %s", key, e)
            return None

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        if not self._enabled:
            return
        self._ensure_client()
        if self._client is None:
            return
        try:
            payload = json.dumps(value)
        except (TypeError, ValueError) as e:
            logger.warning("Result cache JSON encode failed for %s: %s", key, e)
            return
        try:
            self._client.set(key, payload, ex=_cache_ttl(ttl_seconds))
        except Exception as e:
            logger.warning("Result cache SET failed (%s): %s", type(e).__name__, e)


# Module-level singleton — connection pool is reused across calls.
_client = _CacheClient()


def _hash_key(*parts: str) -> str:
    """Stable SHA-256 of the joined parts. Parts are separated by a sentinel
    that's vanishingly unlikely to appear in inputs, so collisions between
    e.g. ``("foo", "bar")`` and ``("foob", "ar")`` are impossible.
    """
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00\x01\x02")
    return h.hexdigest()


def get_or_compute(
    cache_name: str,
    key_parts: tuple[str, ...],
    compute: Callable[[], T],
    ttl_seconds: int | None = None,
) -> T:
    """Cache-through helper. Looks up a cached value, computes + stores it
    on miss, returns the value either way. ``compute()`` is only invoked
    on a true miss.
    """
    if cache_name not in _KEY_PREFIX:
        raise KeyError(
            f"Unknown cache_name {cache_name!r}; expected one of {list(_KEY_PREFIX)}"
        )
    key = _KEY_PREFIX[cache_name] + _hash_key(*key_parts)
    cached = _client.get(key)
    from src.services.request_trace import record
    record("local_cache", cache=cache_name, hit=cached is not None)
    if cached is not None:
        return cached
    value = compute()
    _client.set(key, value, ttl_seconds=ttl_seconds)
    return value


def clear_for_tests() -> None:
    """Reset the module-level singleton — used by tests that need a fresh
    connection state. Don't call from production code.
    """
    global _client
    _client = _CacheClient()
