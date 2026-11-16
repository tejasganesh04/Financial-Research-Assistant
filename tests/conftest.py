"""Shared test fixtures."""

import pytest

from src.models.state import RAGState


@pytest.fixture(autouse=True)
def isolated_request_reports(tmp_path, monkeypatch):
    """Tests never write diagnostic reports into the real demo logs."""
    monkeypatch.setenv("REQUEST_TRACE_DIR", str(tmp_path / "traces"))


@pytest.fixture(autouse=True)
def no_question_quotas(monkeypatch):
    """Question quotas need the account database; tests that cover them set their own limits."""
    from src.config.settings import settings

    monkeypatch.setattr(settings, "QUESTIONS_PER_HOUR", 0)
    monkeypatch.setattr(settings, "QUESTIONS_PER_DAY", 0)


@pytest.fixture
def base_state() -> RAGState:
    """A clean initial state for testing individual nodes."""
    return {
        "messages": [],
        "user_id": "test_user",
        "user_role": "finance",
        "allowed_doc_types": ["10k", "invoice", "expense_policy"],
        "guardrail_status": "clean",
        "contextualized_query": "What was Apple's total revenue in 2023?",
        "query_intent": "",
        "retrieved_chunks": [],
        "retrieval_query": "",
        "relevant_chunks": [],
        "grading_results": [],
        "generated_answer": "",
        "hallucination_status": "",
        "hallucination_score": 0.0,
        "requires_human_approval": False,
        "human_decision": None,
        "retrieval_retry_count": 0,
        "generation_retry_count": 0,
        "final_response": "",
        "response_metadata": {},
    }


@pytest.fixture
def sample_chunks() -> list[dict]:
    """Sample retrieved chunks for testing."""
    return [
        {
            "content": "Apple Inc. reported total net revenue of $383.3 billion for fiscal year 2023.",
            "metadata": {
                "doc_type": "10k",
                "company": "Apple Inc.",
                "source_file": "apple_10k_2023.pdf",
                "page_number": 42,
                "section_header": "Item 6. Selected Financial Data",
                "confidentiality": "public",
            },
            "score": 0.92,
        },
        {
            "content": "Operating expenses increased by 2% year-over-year to $54.8 billion.",
            "metadata": {
                "doc_type": "10k",
                "company": "Apple Inc.",
                "source_file": "apple_10k_2023.pdf",
                "page_number": 43,
                "section_header": "Item 7. Management Discussion",
                "confidentiality": "public",
            },
            "score": 0.85,
        },
        {
            "content": "Services revenue grew 9% to $85.2 billion, driven by App Store and iCloud.",
            "metadata": {
                "doc_type": "10k",
                "company": "Apple Inc.",
                "source_file": "apple_10k_2023.pdf",
                "page_number": 44,
                "section_header": "Item 7. Revenue Breakdown",
                "confidentiality": "public",
            },
            "score": 0.81,
        },
    ]


AUTH_TABLES = (
    "oauth_codes, oauth_requests, oauth_clients, auth_refresh_tokens, auth_sessions, auth_events, "
    "rate_limit_counters, auth_schema_version, app_users"
)


@pytest.fixture
async def auth_pool(monkeypatch):
    """A fresh account database in the isolated TEST_POSTGRES_URL, wired into the app and MCP provider."""
    import os

    from psycopg_pool import AsyncConnectionPool

    from src.services import user_store

    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("An isolated test PostgreSQL URL (TEST_POSTGRES_URL) was not supplied")
    if not url.rsplit("/", 1)[-1].split("?")[0].endswith("_test"):
        pytest.fail("TEST_POSTGRES_URL must name a database ending in _test; it is wiped by these tests")
    pool = AsyncConnectionPool(url, open=False, kwargs={"autocommit": True, "prepare_threshold": 0})
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {AUTH_TABLES} CASCADE")
    await user_store.ensure_schema(pool)

    from src.api.main import app
    from src.api.routes.auth import login_limiter
    from src.mcp_server.auth import provider

    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    monkeypatch.setattr(provider, "pool", pool)
    login_limiter.reset()
    yield pool
    await pool.close()
