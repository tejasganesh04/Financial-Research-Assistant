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
    CORS_ORIGINS: list[str] = ["*"]

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

    # --- Auth ---
    JWT_SECRET: str = "dev-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = 24

    # --- Calculator tool for the answer model (exact arithmetic) ---
    CALCULATOR_ENABLED: bool = True
    # Model turns that may call the calculator before the answer must be written.
    CALCULATOR_MAX_ROUNDS: int = 4

    # --- MCP server (document search for external AI assistants, at /mcp) ---
    MCP_ENABLED: bool = True
    # Address clients use to reach this API; shown when a token is created.
    MCP_PUBLIC_URL: str = "http://localhost:8000"
    MCP_TOKEN_EXPIRY_DAYS: int = 30
    MCP_TOKEN_MAX_DAYS: int = 90
    # Searches and page reads per user per minute (each search makes one embedding and one rerank call).
    MCP_RATE_LIMIT_PER_MINUTE: int = 30
    # Host headers accepted at /mcp (protection against DNS rebinding). Add your domain when hosting.
    MCP_ALLOWED_HOSTS: list[str] = ["localhost:*", "127.0.0.1:*", "[::1]:*"]
    MCP_ALLOWED_ORIGINS: list[str] = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]
    # Token ids (the token_id shown when a token is created) that are no longer accepted.
    MCP_REVOKED_TOKEN_IDS: list[str] = []

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
        return self

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
