"""MCP server: tokens, role permissions, filters, page reading, rate limits and the HTTP endpoint.

No network: the document catalog, search, embeddings and reranker are replaced with fakes.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from src.config.settings import settings
from src.mcp_server import auth as mcp_auth
from src.mcp_server import knowledge, server
from src.mcp_server.knowledge import Caller, Document, RequestError
from src.services.auth_service import create_token, decode_token

DOCS = (
    Document("AMAZON_2017_10K", "AMAZON_2017_10K.pdf", "amazon", "Amazon", 2017, "10k", "public", 85),
    Document("AMAZON_2019_10K", "AMAZON_2019_10K.pdf", "amazon", "Amazon", 2019, "10k", "public", 90),
    Document("3M_2018_10K", "3M_2018_10K.pdf", "3m", "3M", 2018, "10k", "public", 160),
    Document("3M_2023Q2_10Q", "3M_2023Q2_10Q.pdf", "3m", "3M", 2023, "10q", "public", 92),
    Document("ACME_2024_BOARD", "ACME_2024_BOARD.pdf", "acme", "Acme", 2024, "board_report", "confidential", 12),
    Document("TRAVEL_POLICY_2024", "TRAVEL_POLICY_2024.pdf", "acme", "Acme", 2024, "expense_policy", "internal", 4),
)

ANALYST = Caller("analyst", "Test Analyst", "analyst")
HR = Caller("hr", "Test HR", "hr")
CEO = Caller("clevel", "Test CEO", "c_level")


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    monkeypatch.setattr(knowledge, "load_documents", lambda: DOCS)


def chunk(source, page, index, text, company="amazon", year=2017, score=0.5, **extra):
    meta = {
        "source_file": source,
        "financebench_doc_name": source.removesuffix(".pdf"),
        "page_number": page,
        "chunk_index": index,
        "company": company,
        "fiscal_year": year,
        "doc_type": "10k",
        **extra,
    }
    return {"content": text, "metadata": meta, "score": 0.1, "rerank_score": score}


@pytest.fixture
def fake_search(monkeypatch):
    calls = {}

    def hybrid_search(client, query_text, query_dense_vector, rbac_filter, top_k):
        calls["filter"], calls["query"], calls["top_k"] = rbac_filter, query_text, top_k
        return [chunk("AMAZON_2017_10K.pdf", 40, 283, "[Amazon · 10-K FY2017 · Page 40] Accounts payable 34,616")]

    monkeypatch.setattr(knowledge, "hybrid_search", hybrid_search)
    monkeypatch.setattr(knowledge, "embed_text", lambda text: [0.0])
    monkeypatch.setattr(knowledge, "get_qdrant_client", lambda: None)
    monkeypatch.setattr(knowledge, "rerank", lambda query, chunks, top_k: chunks[:top_k])
    return calls


def filter_values(search_filter) -> dict:
    return {c.key: list(getattr(c.match, "any", None) or [c.match.value]) for c in search_filter.must}


# ── Tokens ───────────────────────────────────────────────────────────────────


def test_mcp_token_round_trip():
    token, expires, token_id = mcp_auth.create_mcp_token("analyst", "Test Analyst", "analyst", "Research")
    caller, claims = mcp_auth.decode_mcp_token(token)
    assert caller == Caller("analyst", "Test Analyst", "analyst", "Research", token_id)
    assert claims["scope"] == mcp_auth.SCOPE
    assert expires - time.time() == pytest.approx(settings.MCP_TOKEN_EXPIRY_DAYS * 86_400, abs=5)


def test_app_api_rejects_mcp_tokens():
    token, _, _ = mcp_auth.create_mcp_token("admin", "Test Admin", "admin")
    with pytest.raises(HTTPException) as error:
        decode_token(token)
    assert error.value.status_code == 401


def test_mcp_rejects_app_login_tokens():
    with pytest.raises(mcp_auth.InvalidMcpToken):
        mcp_auth.decode_mcp_token(create_token("admin", "Test Admin", "admin"))


def _signed(**changes) -> str:
    now = int(time.time())
    claims = {
        "sub": "analyst",
        "role": "analyst",
        "iat": now,
        "exp": now + 60,
        "iss": mcp_auth.ISSUER,
        "aud": mcp_auth.AUDIENCE,
        "scope": mcp_auth.SCOPE,
        "jti": "abc",
    }
    claims.update(changes)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, settings.JWT_SECRET, settings.JWT_ALGORITHM)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"exp": int(time.time()) - 10}, "expired"),
        ({"scope": "conversations:write"}, "does not allow"),
        ({"role": "superuser"}, "unknown role"),
        ({"aud": "someone-else"}, "not valid"),
        ({"jti": None}, "not valid"),
    ],
)
def test_mcp_token_rejections(changes, message):
    with pytest.raises(mcp_auth.InvalidMcpToken, match=message):
        mcp_auth.decode_mcp_token(_signed(**changes))


def test_tampered_token_rejected():
    token = _signed()
    with pytest.raises(mcp_auth.InvalidMcpToken):
        mcp_auth.decode_mcp_token(token[:-2] + ("AA" if not token.endswith("AA") else "BB"))


def test_revoked_token_rejected(monkeypatch):
    token, _, token_id = mcp_auth.create_mcp_token("analyst", "A", "analyst")
    monkeypatch.setattr(settings, "MCP_REVOKED_TOKEN_IDS", [token_id])
    with pytest.raises(mcp_auth.InvalidMcpToken, match="revoked"):
        mcp_auth.decode_mcp_token(token)


@pytest.mark.parametrize("days", [0, -1, 10_000])
def test_token_lifetime_bounds(days):
    with pytest.raises(ValueError):
        mcp_auth.create_mcp_token("analyst", "A", "analyst", days=days)


async def test_token_verifier_returns_none_for_bad_tokens():
    verifier = mcp_auth.McpTokenVerifier()
    assert await verifier.verify_token("not-a-token") is None
    token, expires, _ = mcp_auth.create_mcp_token("analyst", "A", "analyst")
    access = await verifier.verify_token(token)
    assert access.client_id == "analyst" and access.scopes == [mcp_auth.SCOPE] and access.expires_at == expires


# ── Role permissions ─────────────────────────────────────────────────────────


def test_roles_see_only_permitted_documents():
    names = lambda caller: {d.name for d in knowledge.visible_documents(caller)}  # noqa: E731
    assert names(ANALYST) == {"AMAZON_2017_10K", "AMAZON_2019_10K", "3M_2018_10K", "3M_2023Q2_10Q"}
    assert names(HR) == {"TRAVEL_POLICY_2024"}
    assert names(CEO) == {d.name for d in DOCS}


def test_list_documents_groups_by_company_and_reports_access():
    listed = knowledge.list_documents(ANALYST)
    assert [c["name"] for c in listed["companies"]] == ["3M", "Amazon"]
    assert listed["document_count"] == 4
    assert listed["access"] == {"document_types": ["10k", "10q", "8k"], "confidentiality": ["public"]}
    assert "Acme" not in json.dumps(listed)


def test_list_documents_for_one_company():
    listed = knowledge.list_documents(ANALYST, company="amzn")
    assert [c["company"] for c in listed["companies"]] == ["amazon"]


def test_hidden_and_missing_companies_get_the_same_answer():
    with pytest.raises(RequestError) as hidden:
        knowledge.resolve_companies(["Acme"], knowledge.visible_documents(ANALYST))
    with pytest.raises(RequestError) as missing:
        knowledge.resolve_companies(["Enron"], knowledge.visible_documents(ANALYST))
    assert str(hidden.value).replace("'Acme'", "X") == str(missing.value).replace("'Enron'", "X")
    assert "Acme" not in str(missing.value)


def test_hidden_document_cannot_be_read():
    with pytest.raises(RequestError, match="No document named"):
        knowledge.read_pages(ANALYST, "ACME_2024_BOARD", [1])


def test_hidden_document_type_is_refused():
    with pytest.raises(RequestError, match="isn't available to you"):
        knowledge.search_documents(ANALYST, "board minutes", document_types=["board_report"])


def test_role_without_documents_cannot_search(monkeypatch):
    monkeypatch.setattr(knowledge, "load_documents", lambda: DOCS[:4])
    with pytest.raises(RequestError, match="no documents"):
        knowledge.search_documents(HR, "revenue")


def test_filings_are_hidden_from_hr():
    with pytest.raises(RequestError, match="Companies you can search: Acme."):
        knowledge.search_documents(HR, "revenue", companies=["Amazon"])


# ── Filters ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["Amazon", "amazon", "AMZN", "amazon.com", " Amazon "])
def test_company_names_tickers_and_slugs(name):
    assert knowledge.resolve_companies([name], knowledge.visible_documents(ANALYST)) == ["amazon"]


def test_fiscal_years_include_later_filings_with_a_note():
    years, notes = knowledge.resolve_years([2017], ["amazon"], knowledge.visible_documents(ANALYST))
    assert years == [2017, 2019]
    assert any("FY2019" in n for n in notes)


def test_uncovered_years_are_reported():
    years, notes = knowledge.resolve_years([2017, 2030], ["amazon"], knowledge.visible_documents(ANALYST))
    assert years == [2017, 2019]
    assert any("FY2030" in n for n in notes)


def test_no_covered_year_is_an_error_listing_available_years():
    with pytest.raises(RequestError, match="Available fiscal years: FY2017, FY2019"):
        knowledge.resolve_years([2005], ["amazon"], knowledge.visible_documents(ANALYST))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": ""},
        {"query": "x" * 1001},
        {"query": "x", "max_results": 0},
        {"query": "x", "max_results": 21},
        {"query": "x", "companies": ["a"] * 11},
        {"query": "x", "fiscal_years": [17]},
    ],
)
def test_invalid_search_requests(kwargs, fake_search):
    with pytest.raises(RequestError):
        knowledge.search_documents(ANALYST, **kwargs)
    assert "filter" not in fake_search


# ── Search ───────────────────────────────────────────────────────────────────


def test_search_always_applies_the_role_filter(fake_search):
    knowledge.search_documents(ANALYST, "accounts payable", companies=["Amazon"], fiscal_years=[2017])
    values = filter_values(fake_search["filter"])
    assert values == {
        "doc_type": ["10k", "10q", "8k"],
        "confidentiality": ["public"],
        "company": ["amazon"],
        "fiscal_year": [2017, 2019],
    }
    assert fake_search["top_k"] == settings.RETRIEVAL_TOP_K


def test_document_type_filter_narrows_within_the_role(fake_search):
    knowledge.search_documents(ANALYST, "revenue", document_types=["10-Q"])
    assert filter_values(fake_search["filter"])["doc_type"] == ["10q"]


def test_search_results_carry_citations_and_clean_text(fake_search):
    result = knowledge.search_documents(ANALYST, "accounts payable", companies=["Amazon"])
    first = result["results"][0]
    assert first["citation"] == "AMAZON_2017_10K, page 40"
    assert first["company"] == "Amazon"
    assert first["text"] == "Accounts payable 34,616"
    assert result["content_notice"] == knowledge.UNTRUSTED_NOTICE
    assert result["searched"]["fiscal_years"] == "all"


# ── Page reading ─────────────────────────────────────────────────────────────


def test_read_pages_applies_access_and_stitches_chunks(monkeypatch):
    captured = {}
    payloads = [
        {
            "page_number": 40,
            "chunk_index": 284,
            "content": "[A · B] Shares 500 Issued and outstanding",
            "statement": "Balance",
        },
        {"page_number": 40, "chunk_index": 283, "content": "[A · B] Total assets 131,310 Shares 500 Issued and"},
    ]

    class Client:
        def scroll(self, collection_name, scroll_filter, limit, with_payload, with_vectors):
            captured["filter"] = scroll_filter
            return [SimpleNamespace(payload=p) for p in payloads], None

    monkeypatch.setattr(knowledge, "get_qdrant_client", lambda: Client())
    result = knowledge.read_pages(ANALYST, "amazon_2017_10k.pdf", [40, 41])
    assert result["pages"][0]["text"] == "Total assets 131,310 Shares 500 Issued and outstanding"
    assert result["pages"][0]["statements"] == ["Balance"]
    assert any("41" in n for n in result["notes"])
    keys = {c.key for c in captured["filter"].must}
    assert {"source_file", "page_number", "doc_type", "confidentiality"} <= keys


@pytest.mark.parametrize(
    ("pages", "message"), [([], "at least one"), ([1, 2, 3, 4, 5, 6], "at most"), ([86], "outside")]
)
def test_read_pages_validation(pages, message):
    with pytest.raises(RequestError, match=message):
        knowledge.read_pages(ANALYST, "AMAZON_2017_10K", pages)


def test_merge_overlapping_removes_repeated_text():
    merged = knowledge.merge_overlapping(
        ["The quick brown fox jumps over", "fox jumps over the lazy dog"], min_overlap=5
    )
    assert merged == "The quick brown fox jumps over the lazy dog"
    assert knowledge.merge_overlapping(["alpha", "beta"]) == "alpha\nbeta"


def test_clean_text_drops_header_and_repeated_leading_overlap():
    repeated = "bilities 12,607 20,975 Commitments and contingencies (Note 7) "
    text = f"[Amazon · 10-K FY2017 · Page 40] {repeated}{repeated}{repeated}outstanding shares"
    assert knowledge.clean_text(text) == f"{repeated}outstanding shares"
    assert knowledge.clean_text("[Not a header] text") == "[Not a header] text"


# ── Server layer ─────────────────────────────────────────────────────────────


def test_rate_limiter_blocks_after_limit():
    limiter = server.RateLimiter(2)
    limiter.check("u")
    limiter.check("u")
    with pytest.raises(RequestError, match="Rate limit"):
        limiter.check("u")
    limiter.check("other-user")


def test_stdio_caller_used_without_http_token():
    server.set_stdio_caller(ANALYST)
    try:
        assert server.current_caller(None) == ANALYST
    finally:
        server.set_stdio_caller(None)


def test_no_identity_is_refused():
    with pytest.raises(server.ToolError, match="Not signed in"):
        server.current_caller(None)


async def test_tool_errors_are_clean_and_unexpected_errors_hide_details(monkeypatch):
    server.set_stdio_caller(ANALYST)
    try:

        def invalid(caller):
            raise RequestError("Fix it.")

        rejected = await server._run("t", None, invalid, limited=False, audit_details={})
        assert rejected.isError and rejected.content[0].text == "Fix it."

        def broken(caller):
            raise RuntimeError("database password is hunter2")

        failed = await server._run("t", None, broken, limited=False, audit_details={})
        assert failed.isError and "hunter2" not in failed.content[0].text

        ok = await server._run("t", None, lambda c: {"result_count": 0, "x": None}, limited=False, audit_details={})
        assert not ok.isError and ok.structuredContent == {"result_count": 0}
    finally:
        server.set_stdio_caller(None)


# ── HTTP endpoint ────────────────────────────────────────────────────────────


@pytest.fixture
def mcp_http():
    @asynccontextmanager
    async def lifespan(app):
        async with server.http_lifespan():
            yield

    app = Starlette(routes=[Route("/mcp", endpoint=server.http_endpoint)], lifespan=lifespan)
    with TestClient(app, base_url="http://localhost:8000") as client:
        yield client


HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}


def mcp_headers(token: str) -> dict:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


def test_http_requires_an_mcp_token(mcp_http):
    assert mcp_http.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
    app_token = create_token("admin", "Test Admin", "admin")
    assert mcp_http.post("/mcp", json=INIT, headers=mcp_headers(app_token)).status_code == 401


def test_http_handshake_and_tool_list(mcp_http):
    token, _, _ = mcp_auth.create_mcp_token("analyst", "Test Analyst", "analyst")
    init = mcp_http.post("/mcp", json=INIT, headers=mcp_headers(token))
    assert init.status_code == 200
    assert init.json()["result"]["serverInfo"]["name"] == "financial-research"
    listed = mcp_http.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=mcp_headers(token)
    ).json()
    assert {t["name"] for t in listed["result"]["tools"]} == {"list_documents", "search_documents", "read_pages"}


def test_http_tool_call_uses_the_token_role(mcp_http):
    token, _, _ = mcp_auth.create_mcp_token("hr", "Test HR", "hr")
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_documents", "arguments": {}}}
    result = mcp_http.post("/mcp", json=body, headers=mcp_headers(token)).json()["result"]
    assert result["structuredContent"]["caller"]["role"] == "hr"
    assert [d["document"] for c in result["structuredContent"]["companies"] for d in c["documents"]] == [
        "TRAVEL_POLICY_2024"
    ]


def test_http_rejects_unknown_host_headers(mcp_http):
    token, _, _ = mcp_auth.create_mcp_token("analyst", "A", "analyst")
    response = mcp_http.post("/mcp", json=INIT, headers={**mcp_headers(token), "Host": "attacker.example"})
    assert response.status_code in (400, 421)


# ── App integration ──────────────────────────────────────────────────────────


def test_app_serves_mcp_and_issues_tokens():
    from src.api.main import app

    assert any(getattr(r, "path", None) == "/mcp" for r in app.router.routes)
    client = TestClient(app)
    login = client.post("/v1/auth/login", json={"username": "analyst", "password": "analyst123"}).json()
    issued = client.post(
        "/v1/auth/mcp-token", json={"days": 7}, headers={"Authorization": f"Bearer {login['access_token']}"}
    )
    assert issued.status_code == 200
    body = issued.json()
    assert body["role"] == "analyst" and body["mcp_url"].endswith("/mcp") and body["scope"] == mcp_auth.SCOPE
    caller, claims = mcp_auth.decode_mcp_token(body["access_token"])
    assert caller.user_id == "analyst" and claims["exp"] - claims["iat"] == 7 * 86_400
    # An MCP token can neither use the API nor mint another token.
    mcp_token = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/v1/auth/me", headers=mcp_token).status_code == 401
    assert client.post("/v1/auth/mcp-token", headers=mcp_token).status_code == 401


def test_token_endpoint_requires_login_and_valid_lifetime():
    from src.api.main import app

    client = TestClient(app)
    assert client.post("/v1/auth/mcp-token").status_code in (401, 403)
    login = client.post("/v1/auth/login", json={"username": "analyst", "password": "analyst123"}).json()
    response = client.post(
        "/v1/auth/mcp-token", json={"days": 365}, headers={"Authorization": f"Bearer {login['access_token']}"}
    )
    assert response.status_code == 422


# ── Company names as assistants write them ───────────────────────────────────

AMERICANS = [
    Document("AMEX_2022_10K", "AMEX_2022_10K.pdf", "american_express", "American Express", 2022, "10k", "public", 9),
    Document(
        "AWK_2021_10K", "AWK_2021_10K.pdf", "american_water_works", "American Water Works", 2021, "10k", "public", 9
    ),
    Document("LMT_2022_10K", "LMT_2022_10K.pdf", "lockheed_martin", "Lockheed Martin", 2022, "10k", "public", 9),
    Document("COST_2021_10K", "COST_2021_10K.pdf", "costco", "Costco", 2021, "10k", "public", 9),
]


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Amazon.com, Inc.", "amazon"),
        ("The 3M Company", "3m"),
        ("Lockheed", "lockheed_martin"),
        ("Lockheed Martin Corporation", "lockheed_martin"),
        ("Costco Wholesale", "costco"),
        ("American Water", "american_water_works"),
        ("AXP", "american_express"),
    ],
)
def test_company_names_with_suffixes_prefixes_and_tickers(name, slug):
    assert knowledge.resolve_companies([name], list(DOCS[:4]) + AMERICANS) == [slug]


def test_ambiguous_company_name_lists_the_choices():
    with pytest.raises(RequestError, match="ambiguous: 'American' matches American Express, American Water Works"):
        knowledge.resolve_companies(["American"], AMERICANS)


@pytest.mark.parametrize("name", ["Micro", "Inc", "Apple", "", "Ama"])
def test_partial_words_and_unknown_names_do_not_match(name):
    with pytest.raises(RequestError):
        knowledge.resolve_companies([name], list(DOCS[:4]) + AMERICANS)


def test_fuzzy_matching_never_reaches_hidden_companies():
    with pytest.raises(RequestError) as error:
        knowledge.resolve_companies(["Acme Holdings Inc"], knowledge.visible_documents(ANALYST))
    assert "Acme" not in str(error.value).split("match company")[1].split("Companies you can search")[1]


def test_results_group_chunks_by_page_in_rank_order():
    ranked = [
        chunk("A.pdf", 5, 51, "best on page 5", score=0.9),
        chunk("A.pdf", 7, 70, "page 7", score=0.8),
        chunk("A.pdf", 5, 50, "earlier on page 5", score=0.7),
        chunk("A.pdf", 9, 90, "page 9", score=0.6),
    ]
    groups = knowledge.page_passages(ranked, max_results=2)
    assert [[c["metadata"]["chunk_index"] for c in g] for g in groups] == [[51, 50], [70]]
    assert knowledge._passage_text(groups[0]) == "earlier on page 5\nbest on page 5"


def test_http_declines_event_streams(mcp_http):
    token, _, _ = mcp_auth.create_mcp_token("analyst", "A", "analyst")
    response = mcp_http.get("/mcp", headers={"Accept": "text/event-stream", "Authorization": f"Bearer {token}"})
    assert response.status_code == 405


def test_audit_log_trims_caller_supplied_values(caplog):
    with caplog.at_level("INFO", logger="src.mcp_server.audit"):
        server._audit("read_pages", ANALYST, "rejected", time.monotonic(), document="x" * 5000, pages=list(range(50)))
    entry = json.loads(caplog.records[-1].getMessage())
    assert len(entry["document"]) == 200 and len(entry["pages"]) == 20 and entry["user"] == "analyst"
