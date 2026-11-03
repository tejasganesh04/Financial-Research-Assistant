import logging
import os
import subprocess
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from src.api.routes import approvals, auth, conversations, documents, health
from src.config.settings import settings

API_VERSION = "1"
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL), format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# SERVER START/STOP: Before yield prepares shared resources; after yield closes them. Runs once per worker.
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Set env-specific LangSmith project name before any LLM calls
    os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project_name

    # Attach local application logs and report configured components at startup.
    from src.services.event_log import attach_file_handler, log_runtime_components
    attach_file_handler()
    log_runtime_components()

    # Startup validation
    if not settings.OPENAI_API_KEY and not settings.FORCE_GROQ_ONLY:
        logger.error("OPENAI_API_KEY is not set! Embeddings and generation will fail.")
    if not settings.GROQ_API_KEY:
        logger.warning("GROQ_API_KEY is not set. Router/grader will fall back to OpenAI.")
    if not settings.LANGCHAIN_API_KEY:
        logger.warning("LANGCHAIN_API_KEY is not set. LangSmith tracing will be disabled.")

    # Initialize PostgresSaver checkpointer for HITL persistence
    try:
        conninfo = settings.postgres_url
        logger.info(f"Connecting to PostgreSQL at {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}")
        # Create a reusable pool of 1 to 5 PostgreSQL connections. Each database operation borrows and returns one.
        pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        # Open the pool and verify a database connection before making it available to endpoints.
        await pool.open()
        # Verify connection works
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        logger.info("PostgreSQL connection verified")

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # Run setup with autocommit connection (CREATE INDEX CONCURRENTLY requires it)
        # Ensure LangGraph's checkpoint tables exist. Autocommit supports its index-creation setup statements.
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as setup_conn:
            setup_checkpointer = AsyncPostgresSaver(setup_conn)
            await setup_checkpointer.setup()
        logger.info("Checkpointer tables created")

        # The saver reads/writes graph state and execution records through PostgreSQL; it does not execute our nodes.
        checkpointer = AsyncPostgresSaver(pool)
        # app.state holds shared server resources. It is separate from each conversation's LangGraph state.
        app.state.checkpointer = checkpointer
        app.state.pool = pool
        logger.info("PostgresSaver checkpointer initialized for HITL persistence")
    except Exception as e:
        logger.error(f"PostgresSaver init failed (HITL will be disabled): {e}", exc_info=True)
        app.state.checkpointer = None
        app.state.pool = None

    logger.info(f"RAG Agent API starting (environment={settings.ENVIRONMENT})")
    # STARTUP FINISHED: FastAPI serves requests while lifespan is paused here. Shutdown continues below.
    yield

    # Cleanup
    if app.state.pool:
        await app.state.pool.close()
    logger.info("RAG Agent API shutting down")


# Create the HTTP application and attach the startup/shutdown function; route registration follows below.
app = FastAPI(
    title="Financial Research Assistant API",
    description="Financial document questions, sources, conversations and human review",
    version="0.3.5",
    lifespan=lifespan,
)

# Translate uncaught database connection/pool failures into the same friendly HTTP 503 response.
@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(PoolTimeout)
async def database_unavailable(request, exc):
    logger.warning("Database unavailable: %s", type(exc).__name__)
    return JSONResponse(status_code=503, content={
        "detail": "Conversation storage is unavailable. Check service status before retrying."
    })


# CORS middleware handles browser cross-origin permissions. JWT checks still authorize protected requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@lru_cache(maxsize=1)
def _git_sha() -> str:
    # Prefer an explicitly supplied build ID; otherwise try local Git and report unknown when unavailable.
    env_sha = os.environ.get("GIT_SHA", "").strip()
    if env_sha and env_sha != "unknown":
        return env_sha[:12]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        return out[:12]
    except Exception:
        return "unknown"


@app.get("/version")
def version() -> dict:
    llm_mode = (
        "groq-only" if settings.FORCE_GROQ_ONLY else
        "openai-only" if settings.FORCE_OPENAI_ONLY or not settings.ANTHROPIC_API_KEY else
        "claude-and-openai"
    )
    return {"api_version": API_VERSION, "semver": app.version, "git_sha": _git_sha(), "llm_mode": llm_mode}


# OPEN WEBSITE: GET / returns React's built index.html as a file; the browser then requests its JS/CSS assets.
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


_routers = [health, auth, conversations, approvals, documents]

# React uses the versioned /v1 API.
# Route URL = /v1 + router prefix + endpoint path; for example /v1 + /conversations + /{thread_id}.
for r in _routers:
    app.include_router(r.router, prefix=f"/v{API_VERSION}")

# React uses one page and local tabs, so no catch-all route is needed.
# Missing API routes continue returning their normal 404 response.
# Serve built JS/CSS files under /assets without writing one endpoint per file. React runs in the browser.
if (FRONTEND_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="frontend-assets")


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    icon = FRONTEND_DIR / "favicon.svg"
    if not icon.is_file():
        return JSONResponse(status_code=404, content={"detail": "Frontend not built"})
    return FileResponse(icon)

