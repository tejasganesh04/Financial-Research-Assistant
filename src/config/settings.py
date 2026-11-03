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

    # --- LLM Providers ---
    OPENAI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    FIREWORKS_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    VOYAGE_API_KEY: str = ""
    ABACI_NLP_API_KEY: str = ""

    # --- External eval services ---
    # Patronus AI hosted fuzzy-match judge for FinanceBench external comparability.
    # Free tier covers 150 samples per month (sufficient for one FinanceBench run).
    PATRONUS_API_KEY: str = ""
    # Override: force all LLM calls through OpenAI (used to bypass Groq free-tier rate
    # limits during eval runs). Default false = prod hybrid Groq + OpenAI + Anthropic.
    FORCE_OPENAI_ONLY: bool = False
    # Temporary, opt-in Groq mode: all LLM stages use Groq, with no paid-provider fallback.
    FORCE_GROQ_ONLY: bool = False
    GROQ_LIGHT_MODEL: str = "openai/gpt-oss-20b"
    GROQ_ANSWER_MODEL: str = "openai/gpt-oss-120b"
    # Use Groq for the high-volume "fast path" nodes (router, grader, query rewriter).
    # Set false to send those to OpenAI even when GROQ_API_KEY is set — required for
    # FinanceBench eval runs where 1200+ grader calls easily blow through the free-tier
    # 100k tokens-per-day cap. Default true preserves the production latency profile.
    USE_GROQ_FAST_PATH: bool = True

    # --- LangSmith ---
    LANGCHAIN_TRACING_V2: bool = True
    LANGCHAIN_PROJECT: str = "rag-agent-dev"
    LANGCHAIN_API_KEY: str = ""

    # --- Auth ---
    JWT_SECRET: str = "dev-secret-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = 24

    # --- Qdrant ---
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "financial_docs"

    DOCUMENTS_ROOT: str = "data/sample"

    LITELLM_URL: str = ""

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
    RETRIEVAL_TOP_K: int = 50
    # Reranker: cross-encoder narrows to the final set that feeds the grader + generator.
    RERANKER_TOP_K: int = 8
    # Non-LLM post-reranker validation
    ENABLE_DETERMINISTIC_VALIDATOR: bool = True
    EMBEDDING_PROVIDER: str = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536

    # --- LLM Models ---
    # Kept on Groq for latency-critical classification / routing
    ROUTER_MODEL: str = "llama-3.3-70b-versatile"
    GRADER_MODEL: str = "llama-3.3-70b-versatile"
    GENERATOR_MODEL: str = "claude-sonnet-4-6"
    HALLUCINATION_MODEL: str = "claude-sonnet-4-6"
    HIGH_STAKES_HALLUCINATION_MODEL: str = "claude-sonnet-4-6"  # ↓ from opus-4-7 (saves ~$0.30/eval avg)
    RESEARCH_AGENT_DECOMPOSE_MODEL: str = "gpt-4o-mini"        # ↓ from sonnet-4-6 (saves ~$0.55/eval)
    RESEARCH_AGENT_SUFFICIENCY_MODEL: str = "gpt-4o-mini"      # ↓ from sonnet-4-6 (saves ~$0.55/eval)
    RESEARCH_AGENT_SYNTHESIZE_MODEL: str = "claude-sonnet-4-6"  # KEPT (Haiku regression)
    # Legacy OpenAI fallback model — used when Anthropic fails or FORCE_OPENAI_ONLY is on
    OPENAI_FALLBACK_MODEL: str = "gpt-4o-mini"

    USE_LLAMA_GRADER: bool = False
    LLAMA_GRADER_PROVIDER: str = "openrouter"  # "openrouter" | "fireworks"
    LLAMA_GRADER_MODEL_OPENROUTER: str = "meta-llama/llama-3.3-70b-instruct"
    LLAMA_GRADER_MODEL_FIREWORKS: str = "accounts/fireworks/models/llama-v3p3-70b-instruct"

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
