from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Environment ---
    ENVIRONMENT: str = "dev"
    LOG_LEVEL: str = "INFO"

    # Question-sized input bounds per-request work; the turn cap bounds each saved conversation.
    MAX_MESSAGE_CHARACTERS: int = 2000
    MAX_CONVERSATION_QUESTIONS: int = 20

    # --- CORS ---
    # The interface is served by this API (same origin), so no cross-origin access is needed; the
    # Vite dev server proxies /v1. List origins here only for a separately hosted frontend.
    CORS_ORIGINS: list[str] = []

    # --- Model providers ---
    OPENAI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    VOYAGE_API_KEY: str = ""

    # Provider profiles. Groq-only: every model call uses Groq (the default demo profile in
    # .env.example). OpenAI-only: every call uses OPENAI_FALLBACK_MODEL. Otherwise each task
    # uses the model named below.
    FORCE_OPENAI_ONLY: bool = False
    FORCE_GROQ_ONLY: bool = False
    GROQ_LIGHT_MODEL: str = "openai/gpt-oss-20b"
    GROQ_ANSWER_MODEL: str = "openai/gpt-oss-120b"
    # In the per-task profile, send routing and grading to Groq (ROUTER_MODEL, GRADER_MODEL);
    # false sends them to OPENAI_FALLBACK_MODEL instead.
    USE_GROQ_FAST_PATH: bool = True

    # --- LangSmith ---
    LANGCHAIN_TRACING_V2: bool = True
    LANGCHAIN_PROJECT: str = "rag-agent-dev"
    LANGCHAIN_API_KEY: str = ""

    # --- Monitoring (admin Health page) ---
    # Per-question JSONL and Markdown reports under REQUEST_TRACE_DIR, for local debugging and the
    # FinanceBench scripts. Production turns them off: the database summary and LangSmith cover it.
    REQUEST_TRACE_FILES: bool = True
    # Days to keep the per-question and MCP-call summaries behind the Health page.
    METRICS_RETENTION_DAYS: int = 90
    # Groq's free tier allows about this many tokens per model per day (it refills continuously).
    GROQ_DAILY_TOKEN_LIMIT: int = 200_000
    # JSON written by deploy/backup.sh after each nightly backup ({"at": ..., "bytes": ...}).
    # Empty: backups are not monitored here.
    BACKUP_STATUS_FILE: str = ""

    # --- Public address ---
    # Where people and MCP clients reach this app (https in production). OAuth issuer and the MCP
    # resource URL ({PUBLIC_URL}/mcp) are derived from it.
    PUBLIC_URL: str = "http://localhost:8000"

    # --- Auth ---
    JWT_SECRET: str = "dev-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    # Access tokens (web and MCP) are short-lived and checked by signature only, so changing a
    # password, signing someone out or deactivating them takes effect within this many minutes.
    ACCESS_TOKEN_MINUTES: int = 60
    # Refresh tokens rotate on every use. A session ends after this many idle days, or this many
    # days after sign-in, whichever comes first.
    SESSION_IDLE_DAYS: int = 7
    SESSION_MAX_DAYS: int = 30
    # A refresh token used twice ends its session, except within this many seconds (two browser
    # tabs renewing at once).
    REFRESH_REUSE_GRACE_SECONDS: int = 30
    # Send the sign-in cookie over HTTPS only. Defaults to on in production.
    COOKIE_SECURE: bool | None = None
    # Sign-in protection: lock an account after this many failed attempts in a row, and limit
    # attempts per IP address.
    LOGIN_MAX_FAILURES: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15
    LOGIN_ATTEMPTS_PER_MINUTE: int = 10
    # Questions to the in-app assistant per user (each one makes several model calls). 0 = no limit.
    # Questions answered at the same time by this API process; others wait their turn and see
    # "Waiting for a free slot". 0 means no limit. Free model tiers (such as Groq's) refuse
    # bursts, so a small number keeps simultaneous users from failing with rate limits.
    MAX_CONCURRENT_QUESTIONS: int = 0
    QUESTIONS_PER_HOUR: int = 60
    QUESTIONS_PER_DAY: int = 300

    @property
    def cookie_secure(self) -> bool:
        return self.ENVIRONMENT == "production" if self.COOKIE_SECURE is None else self.COOKIE_SECURE

    @property
    def public_url(self) -> str:
        return self.PUBLIC_URL.rstrip("/")

    # Load the local question-screening models in the background at start-up, so the first question
    # does not wait for them (the first start on a new server downloads them).
    WARM_MODELS_ON_START: bool = True

    # --- Calculator tool for the answer model (exact arithmetic) ---
    CALCULATOR_ENABLED: bool = True
    # Model turns that may call the calculator before the answer must be written.
    CALCULATOR_MAX_ROUNDS: int = 4

    # --- MCP server (document search for external AI assistants, at /mcp) ---
    # Clients sign in with OAuth (the same accounts as the web app); see docs/mcp-server.md.
    MCP_ENABLED: bool = True
    # Searches and page reads per user per minute (each search makes one embedding and one rerank call).
    MCP_RATE_LIMIT_PER_MINUTE: int = 30
    # New OAuth client registrations per IP address per hour (each Claude Code install registers once).
    MCP_CLIENT_REGISTRATIONS_PER_HOUR: int = 20
    # Where clients may receive sign-in codes, besides localhost (Claude Code and other local clients):
    # https hosts of hosted clients, such as Claude's web and desktop apps. Subdomains are included.
    MCP_REDIRECT_HOSTS: list[str] = ["claude.ai", "claude.com"]
    # Host headers accepted at /mcp (protection against DNS rebinding). Add your domain when hosting.
    MCP_ALLOWED_HOSTS: list[str] = ["localhost:*", "127.0.0.1:*", "[::1]:*"]
    MCP_ALLOWED_ORIGINS: list[str] = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]

    @property
    def mcp_resource_url(self) -> str:
        return f"{self.public_url}/mcp"

    # --- Qdrant ---
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "financial_docs"

    DOCUMENTS_ROOT: str = "data/sample"

    # --- PostgreSQL ---
    POSTGRES_DB: str = "rag_agent"
    POSTGRES_USER: str = "rag_user"
    POSTGRES_PASSWORD: str = "devpassword"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # --- Agent Thresholds ---
    HALLUCINATION_THRESHOLD: float = 0.7
    GRADING_MIN_RELEVANT_CHUNKS: int = 1
    MAX_RETRIEVAL_RETRIES: int = 2
    MAX_GENERATION_RETRIES: int = 2
    # Retrieval: cast a wide net (hybrid dense+BM25 with RRF) so the reranker has
    # enough variety to pick from.
    RETRIEVAL_TOP_K: int = 100
    # Reranker: narrows the candidates to the final set that feeds the grader + generator.
    # "voyage" calls Voyage's reranker and falls back to the local BGE cross-encoder on failure;
    # "local" always uses the local model. rerank-2.5 (the measured setup) is billed per token;
    # rerank-3 is Voyage's current model.
    RERANKER_PROVIDER: str = "voyage"
    # When Voyage fails, rank with the local BGE cross-encoder (about 2 GB to download and to hold in
    # memory). False keeps the search order instead, so a small server is not pushed out of memory.
    RERANKER_LOCAL_FALLBACK: bool = True
    VOYAGE_RERANK_MODEL: str = "rerank-2.5"
    RERANKER_TOP_K: int = 12
    # Add statement wording for named financial concepts to the search and reranking text.
    SEARCH_STATEMENT_TERMS: bool = True
    # How the final chunks are chosen: "rerank" (reranker order), "fusion" (reranker rank fused with
    # the dense-search rank, which helps the local model) or "protect" (dense top 3 plus reranker order).
    RERANK_SELECTION: str = "rerank"
    RERANK_FUSION_K: int = 10
    # Characters of neighbouring-chunk text added before answering. Free Groq allows 8,000 tokens
    # per minute per request, so the Groq profile uses a smaller budget.
    NEIGHBOUR_CHAR_BUDGET: int = 12_000
    # Rule-based check of company and year on reranked chunks, before the grader.
    ENABLE_DETERMINISTIC_VALIDATOR: bool = True
    # Must match the knowledge base's vectors.
    EMBEDDING_PROVIDER: str = "voyage"
    EMBEDDING_MODEL: str = "voyage-finance-2"
    EMBEDDING_DIMENSIONS: int = 1024

    # --- Per-task models (used when neither provider profile is forced) ---
    ROUTER_MODEL: str = "llama-3.3-70b-versatile"
    GRADER_MODEL: str = "llama-3.3-70b-versatile"
    GENERATOR_MODEL: str = "claude-sonnet-4-6"
    HALLUCINATION_MODEL: str = "claude-sonnet-4-6"
    HIGH_STAKES_HALLUCINATION_MODEL: str = "claude-sonnet-4-6"
    RESEARCH_AGENT_DECOMPOSE_MODEL: str = "gpt-4o-mini"
    RESEARCH_AGENT_SUFFICIENCY_MODEL: str = "gpt-4o-mini"
    RESEARCH_AGENT_SYNTHESIZE_MODEL: str = "claude-sonnet-4-6"
    # Used by the OpenAI-only profile, and when an Anthropic or Groq key is missing.
    OPENAI_FALLBACK_MODEL: str = "gpt-4o-mini"

    @property
    def langchain_project_name(self) -> str:
        """Return env-specific LangSmith project name."""
        if self.LANGCHAIN_PROJECT != "rag-agent-dev":
            return self.LANGCHAIN_PROJECT
        env_suffix = {"dev": "dev", "staging": "staging", "production": "prod"}
        return f"rag-agent-{env_suffix.get(self.ENVIRONMENT, self.ENVIRONMENT)}"

    @model_validator(mode="after")
    def validate_production_settings(self):
        """Enforce safety checks for production environment."""
        if self.ENVIRONMENT == "production":
            if self.JWT_SECRET == "dev-secret-change-in-production":
                raise ValueError("JWT_SECRET must be changed from default in production!")
            if self.POSTGRES_PASSWORD == "devpassword":
                raise ValueError("POSTGRES_PASSWORD must be changed from default in production!")
            if "*" in self.CORS_ORIGINS:
                raise ValueError("CORS_ORIGINS must not be wildcard '*' in production!")
            if len(self.JWT_SECRET) < 32:
                raise ValueError("JWT_SECRET must be at least 32 characters in production!")
            if not self.PUBLIC_URL.startswith("https://"):
                raise ValueError("PUBLIC_URL must be an https:// address in production!")
        return self

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
