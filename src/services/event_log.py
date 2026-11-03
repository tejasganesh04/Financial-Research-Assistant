"""Write structured pipeline events and log the loaded runtime components."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]

# Set by the caller (eval runner sets fb_id; API sets thread_id). Read by emit().
current_fb_id: ContextVar[str | None] = ContextVar("current_fb_id", default=None)

_lock = threading.Lock()
_log_file = None
_log_path: Path | None = None
_text_log_path: Path | None = None
_run_id: str | None = None


def _init() -> None:
    """Lazy-init the JSONL log file on first emit. Idempotent."""
    global _log_file, _log_path, _text_log_path, _run_id
    if _log_file is not None:
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _run_id = f"run_{ts}"
    env_path = os.environ.get("EVENT_LOG_PATH", "").strip()
    if env_path:
        _log_path = Path(env_path)
        _log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        logs_dir = Path(os.environ.get("LOGS_DIR", ROOT / "logs"))
        logs_dir.mkdir(parents=True, exist_ok=True)
        _log_path = logs_dir / f"{_run_id}.jsonl"
    _text_log_path = _log_path.with_suffix(".log")
    _log_file = open(_log_path, "a", buffering=1)


def emit(stage: str, **fields: Any) -> None:
    """Write a structured event. Always includes ts/run_id/fb_id/stage."""
    # These calls already contain selected decisions, not full graph state.
    # Runtime boot information belongs in the process log only.
    from src.services.request_trace import record
    if stage != "runtime_components":
        record("decision." + stage, **fields)
    _init()
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": _run_id,
        "stage": stage,
        "fb_id": current_fb_id.get(),
        **fields,
    }
    line = json.dumps(rec, default=str)
    with _lock:
        _log_file.write(line + "\n")


def get_log_path() -> Path | None:
    _init()
    return _log_path


def get_text_log_path() -> Path | None:
    _init()
    return _text_log_path


def get_run_id() -> str | None:
    _init()
    return _run_id


def attach_file_handler(level: int = logging.INFO) -> None:
    """Add a FileHandler to the root logger so existing logger.info calls are
    captured to logs/run_<timestamp>.log in addition to stderr. Idempotent."""
    _init()
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "_event_log_attached", False):
            return  # already attached
    fh = logging.FileHandler(_text_log_path)
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
    fh._event_log_attached = True  # mark so we don't double-attach
    root.addHandler(fh)


def _safe_get(obj: Any, *attrs: str) -> Any:
    for a in attrs:
        v = getattr(obj, a, None)
        if v not in (None, ""):
            return v
    return None


def log_runtime_components() -> dict:
    """Report configured models, service availability and collection compatibility at startup."""
    from src.config.settings import settings

    banner: dict[str, Any] = {
        "git": {},
        "settings": {},
        "env_relevant": {},
        "components_loaded": {},
        "external": {},
    }

    # Git state — prefer GIT_SHA env (set by Dockerfile ARG → ENV) before
    # subprocess. 0.1.5 fixed `src/api/main.py:_git_sha()` to use the env var
    # but this second call site was missed; the container has no .git/, so
    # subprocess failed every boot and the audit log carried git={'error': ...}
    # for every run_id. 0.1.6: same pattern here.
    env_sha = os.environ.get("GIT_SHA", "").strip()
    if env_sha and env_sha != "unknown":
        banner["git"] = {
            "sha": env_sha[:12],
            "dirty": False,         # build-time sha; no working-tree concept inside container
            "n_dirty_files": 0,
            "source": "env",
        }
    else:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
            dirty = subprocess.check_output(
                ["git", "status", "--short"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
            banner["git"] = {
                "sha": sha[:12],
                "dirty": bool(dirty),
                "n_dirty_files": len([line for line in dirty.splitlines() if line.strip()]),
                "source": "subprocess",
            }
        except Exception as exc:
            banner["git"] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}

    # Pydantic settings snapshot
    for k in (
        "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS",
        "QDRANT_HOST", "QDRANT_PORT", "QDRANT_COLLECTION",
        "GENERATOR_MODEL", "HALLUCINATION_MODEL", "HIGH_STAKES_HALLUCINATION_MODEL",
        "ROUTER_MODEL", "GRADER_MODEL",
        "RESEARCH_AGENT_DECOMPOSE_MODEL", "RESEARCH_AGENT_SUFFICIENCY_MODEL",
        "RESEARCH_AGENT_SYNTHESIZE_MODEL",
        "USE_LLAMA_GRADER", "LLAMA_GRADER_PROVIDER", "USE_GROQ_FAST_PATH",
        "FORCE_OPENAI_ONLY", "OPENAI_FALLBACK_MODEL",
        "FORCE_GROQ_ONLY", "GROQ_LIGHT_MODEL", "GROQ_ANSWER_MODEL",
        "RETRIEVAL_TOP_K", "RERANKER_TOP_K",
        "ENABLE_DETERMINISTIC_VALIDATOR",
        "MAX_RETRIEVAL_RETRIES", "MAX_GENERATION_RETRIES",
        "GRADING_MIN_RELEVANT_CHUNKS",
    ):
        banner["settings"][k] = getattr(settings, k, None)

    # Selected os.environ — these are the keys that bypass pydantic-settings
    # and have historically been silent-bug surfaces.
    for k in (
        "RERANKER_DEVICE", "GRADER_PARALLELISM",
        "RESULT_CACHE_REDIS_HOST", "RESULT_CACHE_REDIS_PORT",
        "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING",
        "EVENT_LOG_PATH", "LOGS_DIR",
    ):
        v = os.environ.get(k)
        banner["env_relevant"][k] = v if v else "(unset)"

    try:
        from src.services.reranker_service import get_reranker
        rk = get_reranker()
        banner["components_loaded"]["reranker"] = {
            "class": type(rk).__name__,
        }
    except Exception as exc:
        banner["components_loaded"]["reranker"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # Grader LLM — which model is actually wired up
    try:
        from src.services.llm_factory import LLMFactory
        grader = LLMFactory.get_grader_llm()
        banner["components_loaded"]["grader_llm"] = {
            "class": type(grader).__name__,
            "model": _safe_get(grader, "model_name", "model"),
            "base_url": str(_safe_get(grader, "openai_api_base", "base_url") or "(provider-default)"),
        }
    except Exception as exc:
        banner["components_loaded"]["grader_llm"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    try:
        from qdrant_client import QdrantClient

        from src.services.vector_store import get_collection_fingerprint
        qc = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=5)
        info = qc.get_collection(settings.QDRANT_COLLECTION)
        qdrant_info = {
            "endpoint": f"{settings.QDRANT_HOST}:{settings.QDRANT_PORT}",
            "collection": settings.QDRANT_COLLECTION,
            "points": info.points_count,
            "status": str(info.status),
        }

        # Track 2 hardening: read the actual vector dim from the collection
        # config and compare to settings.EMBEDDING_DIMENSIONS. Catches the
        # silent-failure class the fingerprint check missed for pre-fingerprint
        # collections — qdrant returns HTTP 400 on every retrieval, the
        # exception is swallowed by retrieval_node's broad try/except, and the
        # pipeline cascades to no_info refusals.
        try:
            vectors_cfg = info.config.params.vectors
            if isinstance(vectors_cfg, dict):
                # Named vectors (modern): {"dense": VectorParams(size=N, ...), ...}
                stored_dim = next(iter(vectors_cfg.values())).size
            else:
                # Legacy single-vector schema
                stored_dim = vectors_cfg.size
            qdrant_info["stored_dim"] = stored_dim
            if stored_dim != settings.EMBEDDING_DIMENSIONS:
                qdrant_info["dim_mismatch"] = {
                    "stored": stored_dim,
                    "live": settings.EMBEDDING_DIMENSIONS,
                    "live_provider": settings.EMBEDDING_PROVIDER,
                    "live_model": settings.EMBEDDING_MODEL,
                }
        except Exception as dim_exc:  # noqa: BLE001
            qdrant_info["stored_dim"] = f"probe_failed: {type(dim_exc).__name__}"

        fp = get_collection_fingerprint(qc, settings.QDRANT_COLLECTION)
        if fp is None:
            qdrant_info["fingerprint"] = "unknown (collection pre-dates fingerprinting)"
        else:
            stored = (fp.get("embedding_provider"), fp.get("embedding_model"), fp.get("embedding_dim"))
            live = (settings.EMBEDDING_PROVIDER, settings.EMBEDDING_MODEL, settings.EMBEDDING_DIMENSIONS)
            if stored == live:
                qdrant_info["fingerprint"] = f"match ({fp.get('embedding_provider')}/{fp.get('embedding_model')}, dim={fp.get('embedding_dim')})"
            else:
                qdrant_info["fingerprint"] = {
                    "status": "MISMATCH",
                    "stored": {"provider": stored[0], "model": stored[1], "dim": stored[2]},
                    "live": {"provider": live[0], "model": live[1], "dim": live[2]},
                }
        banner["external"]["qdrant"] = qdrant_info
    except Exception as exc:
        banner["external"]["qdrant"] = {
            "endpoint": f"{settings.QDRANT_HOST}:{settings.QDRANT_PORT}",
            "collection": settings.QDRANT_COLLECTION,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }

    # Redis — ping + key count (the rag-cache used by reranker/grader/query-emb caches)
    try:
        import redis
        host = os.environ.get("RESULT_CACHE_REDIS_HOST", "localhost")
        port = int(os.environ.get("RESULT_CACHE_REDIS_PORT", "6379"))
        db = int(os.environ.get("RESULT_CACHE_REDIS_DB", "1"))
        r = redis.Redis(host=host, port=port, db=db, socket_connect_timeout=2)
        n_keys = r.dbsize()
        banner["external"]["redis"] = {
            "endpoint": f"{host}:{port}/{db}",
            "db": db,
            "connected": True,
            "n_keys": n_keys,
        }
    except Exception as exc:
        banner["external"]["redis"] = {
            "endpoint": f"{os.environ.get('RESULT_CACHE_REDIS_HOST', 'localhost')}:{os.environ.get('RESULT_CACHE_REDIS_PORT', '6379')}",
            "connected": False,
            "error": f"{type(exc).__name__}: {str(exc)[:120]}",
        }

    # Emit the structured event (one line, fully queryable via jq)
    emit("runtime_components", **banner)

    # Print human-readable summary to stderr for the boot log
    sep = "=" * 78
    lines = [
        sep,
        f"[Pipeline boot] run_id={_run_id}  event_log={_log_path}",
        sep,
        f"git:         {banner['git'].get('sha', '?')} ({'dirty +' + str(banner['git'].get('n_dirty_files', '?')) if banner['git'].get('dirty') else 'clean'})",
        f"reranker:    {banner['components_loaded'].get('reranker', {})}",
        f"grader:      {banner['components_loaded'].get('grader_llm', {})}",
        f"generator:   {settings.GROQ_ANSWER_MODEL if settings.FORCE_GROQ_ONLY else settings.GENERATOR_MODEL}  "
        f"hallu: {settings.GROQ_ANSWER_MODEL if settings.FORCE_GROQ_ONLY else settings.HALLUCINATION_MODEL}",
        f"embedding:   {banner['settings'].get('EMBEDDING_PROVIDER')}/{banner['settings'].get('EMBEDDING_MODEL')} (dim={banner['settings'].get('EMBEDDING_DIMENSIONS')})",
        f"qdrant:      {banner['external'].get('qdrant', {})}",
        f"redis:       {banner['external'].get('redis', {})}",
        f"flags:       USE_LLAMA_GRADER={banner['settings'].get('USE_LLAMA_GRADER')}  "
        f"FORCE_GROQ_ONLY={settings.FORCE_GROQ_ONLY}  "
        f"USE_GROQ_FAST_PATH={banner['settings'].get('USE_GROQ_FAST_PATH')}  "
        f"RETRIEVAL_TOP_K={banner['settings'].get('RETRIEVAL_TOP_K')}",
        sep,
    ]
    for line in lines:
        logger.warning(line)

    qdrant_state = banner["external"].get("qdrant", {})
    fp = qdrant_state.get("fingerprint")
    if isinstance(fp, dict) and fp.get("status") == "MISMATCH":
        stored, live = fp["stored"], fp["live"]
        logger.warning(sep)
        logger.warning(
            "WARNING: collection '%s' was created with %s/%s (dim=%s)",
            qdrant_state.get("collection"), stored["provider"], stored["model"], stored["dim"],
        )
        logger.warning(
            "WARNING: current EMBEDDING_PROVIDER is %s (%s, dim=%s)",
            live["provider"], live["model"], live["dim"],
        )
        logger.warning("WARNING: queries will return garbage or fail with VectorDimensionError.")
        logger.warning("WARNING: re-ingest the corpus with the new embedding to fix.")
        logger.warning(sep)

    # Hard-fail boot if the collection's vector dim doesn't match the runtime
    # embedding dim. This is THE bug that consistently masqueraded as "first
    # query returns no_info refusal" — every retrieval call HTTP 400s but the
    # exception gets swallowed silently. Better to refuse to start than to
    # return garbage answers all day.
    dim_mismatch = qdrant_state.get("dim_mismatch")
    if isinstance(dim_mismatch, dict):
        logger.critical(sep)
        logger.critical(
            "FATAL: collection '%s' stores %d-dim vectors but runtime is configured for %d-dim (%s/%s).",
            qdrant_state.get("collection"),
            dim_mismatch["stored"],
            dim_mismatch["live"],
            dim_mismatch["live_provider"],
            dim_mismatch["live_model"],
        )
        logger.critical(
            "FATAL: every retrieval call will fail with HTTP 400 from Qdrant and the pipeline will silently degrade to no_info refusals."
        )
        logger.critical("FATAL: fix by either:")
        logger.critical("FATAL:   (a) re-ingesting with the matching embedding:")
        logger.critical(
            "FATAL:       curl -X DELETE http://%s:%s/collections/%s && python scripts/seed_qdrant.py --sample",
            settings.QDRANT_HOST, settings.QDRANT_PORT, qdrant_state.get("collection"),
        )
        logger.critical("FATAL:   (b) editing .env to match the existing collection:")
        logger.critical("FATAL:       EMBEDDING_PROVIDER=<provider> / EMBEDDING_MODEL=<model> / EMBEDDING_DIMENSIONS=%d", dim_mismatch["stored"])
        logger.critical(sep)
        raise SystemExit(
            f"Embedding dimension mismatch: collection={dim_mismatch['stored']}-dim, "
            f"runtime={dim_mismatch['live']}-dim. Refusing to start. See boot log for fix."
        )

    return banner
