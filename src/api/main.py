import asyncio
import logging
import os
import subprocess
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from functools import lru_cache
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from src.api.routes import admin, approvals, auth, conversations, documents, health, oauth
from src.config.settings import settings
from src.services import user_store

API_VERSION = "1"
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL), format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Set env-specific LangSmith project name before any LLM calls
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project_name

    from src.services.event_log import attach_file_handler, log_runtime_components

    attach_file_handler()
    log_runtime_components()

    if not settings.OPENAI_API_KEY and not settings.FORCE_GROQ_ONLY:
        logger.error("OPENAI_API_KEY is not set! Embeddings and generation will fail.")
    if not settings.GROQ_API_KEY:
        logger.warning("GROQ_API_KEY is not set. Router/grader will fall back to OpenAI.")
    if not settings.LANGCHAIN_API_KEY:
        logger.warning("LANGCHAIN_API_KEY is not set. LangSmith tracing will be disabled.")

    # Initialize PostgresSaver checkpointer for HITL persistence
    try:
        conninfo = settings.postgres_url
        logger.info(
            f"Connecting to PostgreSQL at {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
        )
        pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open()
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        logger.info("PostgreSQL connection verified")

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # Run setup with autocommit connection (CREATE INDEX CONCURRENTLY requires it)
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as setup_conn:
            setup_checkpointer = AsyncPostgresSaver(setup_conn)
            await setup_checkpointer.setup()
        logger.info("Checkpointer tables created")

        checkpointer = AsyncPostgresSaver(pool)
        app.state.checkpointer = checkpointer
        app.state.pool = pool
        logger.info("PostgresSaver checkpointer initialized for HITL persistence")
    except Exception as e:
        logger.error(f"PostgresSaver init failed (HITL will be disabled): {e}", exc_info=True)
        app.state.checkpointer = None
        app.state.pool = None

    housekeeping = None
    if app.state.pool is not None:
        await user_store.ensure_schema(app.state.pool)
        await user_store.purge_expired(app.state.pool)
        if await user_store.count_accounts(app.state.pool) == 0:
            logger.warning("No user accounts exist yet. Create them with: make users")
        housekeeping = asyncio.create_task(_housekeeping(app.state.pool))
    warming = asyncio.create_task(_warm_models()) if settings.WARM_MODELS_ON_START else None

    logger.info(f"RAG Agent API starting (environment={settings.ENVIRONMENT})")
    async with AsyncExitStack() as stack:
        if settings.MCP_ENABLED:
            from src.mcp_server.auth import provider
            from src.mcp_server.server import http_lifespan

            provider.pool = app.state.pool
            await stack.enter_async_context(http_lifespan())
            logger.info("MCP server available at /mcp (OAuth issuer %s)", settings.public_url)
        yield

    if warming is not None and not warming.done():
        warming.cancel()
    if housekeeping is not None:
        housekeeping.cancel()
        with suppress(asyncio.CancelledError):
            await housekeeping

    if app.state.pool:
        await app.state.pool.close()
    logger.info("RAG Agent API shutting down")


async def _warm_models() -> None:
    import anyio

    from src.services.model_loading import warm_local_models

    try:
        await anyio.to_thread.run_sync(warm_local_models, abandon_on_cancel=True)
    except Exception:
        logger.exception("Could not preload the question-screening models; they load on the first question")


async def _housekeeping(pool) -> None:
    """Delete expired sessions, codes and counters every hour."""
    while True:
        await asyncio.sleep(3600)
        try:
            await user_store.purge_expired(pool)
        except Exception:
            logger.exception("Auth housekeeping failed")


_PRODUCTION = settings.ENVIRONMENT == "production"

app = FastAPI(
    title="Financial Research Assistant API",
    description="Financial document questions, sources, conversations and human review",
    version="0.4.0",
    lifespan=lifespan,
    # The interactive API docs load scripts from a CDN; keep them to development.
    docs_url=None if _PRODUCTION else "/docs",
    redoc_url=None if _PRODUCTION else "/redoc",
    openapi_url=None if _PRODUCTION else "/openapi.json",
)


# Report database outages as 503 rather than 500.
@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(PoolTimeout)
async def database_unavailable(request, exc):
    logger.warning("Database unavailable: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "Conversation storage is unavailable. Check service status before retrying."},
    )


if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# The interface's own scripts and styles only; no framing, no third-party connections.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
    "object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # The app never uses these browser features; say so, so nothing injected can either.
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if _PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@lru_cache(maxsize=1)
def _git_sha() -> str:
    env_sha = os.environ.get("GIT_SHA", "").strip()
    if env_sha and env_sha != "unknown":
        return env_sha[:12]
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        return out[:12]
    except Exception:
        return "unknown"


@app.get("/version")
def version() -> dict:
    llm_mode = (
        "groq-only"
        if settings.FORCE_GROQ_ONLY
        else "openai-only"
        if settings.FORCE_OPENAI_ONLY or not settings.ANTHROPIC_API_KEY
        else "claude-and-openai"
    )
    return {"api_version": API_VERSION, "semver": app.version, "git_sha": _git_sha(), "llm_mode": llm_mode}


@app.get("/", response_model=None)
def root():
    """Serve the built interface, or API information before the frontend is built."""
    index = FRONTEND_DIR / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return {
        "name": "Financial Research Assistant API",
        "api_version": API_VERSION,
        "semver": app.version,
        "try": {
            "health": f"/v{API_VERSION}/health",
            "version": "/version",
            "openapi_docs": "/docs",
            "openapi_redoc": "/redoc",
        },
        "maintainer": "Tejas Ganesh",
    }


_routers = [health, auth, admin, oauth, conversations, approvals, documents]

for r in _routers:
    app.include_router(r.router, prefix=f"/v{API_VERSION}")

# Document search for external AI assistants (Claude Code, Claude Desktop, ...); see src/mcp_server.
# OAuth discovery, registration, authorization and token endpoints sit at the root, as MCP clients
# expect them there.
if settings.MCP_ENABLED:
    from starlette.routing import Route

    from src.mcp_server.server import http_endpoint, mcp, oauth_routes

    mcp._mcp_server.version = app.version  # report this app's version, not the SDK's
    app.router.routes.append(Route("/mcp", endpoint=http_endpoint, include_in_schema=False))
    app.router.routes.extend(oauth_routes())


class HashedAssets(StaticFiles):
    """Built files carry a content hash in their names, so browsers may keep them for a year."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if (FRONTEND_DIR / "assets").is_dir():
    app.mount("/assets", HashedAssets(directory=FRONTEND_DIR / "assets"), name="frontend-assets")


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    icon = FRONTEND_DIR / "favicon.svg"
    if not icon.is_file():
        return JSONResponse(status_code=404, content={"detail": "Frontend not built"})
    return FileResponse(icon)


# Pages of the single-page interface; each serves the same index.html and the app picks the view.
SPA_PATHS = (
    "/sign-in",
    "/approvals",
    "/admin",
    "/admin/users",
    "/admin/activity",
    "/account",
    "/connect",
    "/oauth/consent",
)


def _spa_page():
    index = FRONTEND_DIR / "index.html"
    if not index.is_file():
        return JSONResponse(status_code=404, content={"detail": "Frontend not built"})
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


for _path in SPA_PATHS:
    app.add_api_route(_path, _spa_page, methods=["GET"], include_in_schema=False, response_model=None)
