"""Unit tests for GET /v1/documents/{filename}.

Path-traversal guard, RBAC, and 404 paths are stubbed via patches on
the Qdrant lookup helper so the test runs without a live Qdrant.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.services.auth_service import create_token


def _token(role: str) -> str:
    return create_token(user_id=role, name=f"Test {role}", role=role, department="x")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point DOCUMENTS_ROOT at an empty tmp dir so we control the FS surface
    monkeypatch.setattr("src.config.settings.settings.DOCUMENTS_ROOT", str(tmp_path))
    return TestClient(app), tmp_path


def test_path_traversal_blocked(client):
    c, _ = client
    for bad in ["../etc/passwd", "..\\evil.pdf", "/etc/passwd", ".secret.pdf"]:
        resp = c.get(f"/v1/documents/{bad}", headers={"Authorization": f"Bearer {_token('admin')}"})
        # Either rejected as bad request before we hit RBAC, or 404
        assert resp.status_code in (400, 404), f"{bad} should not be 200"


def test_non_pdf_rejected(client):
    c, _ = client
    resp = c.get("/v1/documents/file.txt", headers={"Authorization": f"Bearer {_token('admin')}"})
    assert resp.status_code == 400


def test_unauth_blocked(client):
    c, _ = client
    resp = c.get("/v1/documents/file.pdf")
    assert resp.status_code in (401, 403)


@patch("src.api.routes.documents._lookup_document_meta")
def test_404_when_not_indexed(mock_lookup, client):
    c, _ = client
    mock_lookup.return_value = None
    resp = c.get("/v1/documents/unknown.pdf", headers={"Authorization": f"Bearer {_token('admin')}"})
    assert resp.status_code == 404


@patch("src.api.routes.documents._lookup_document_meta")
def test_403_for_role_lacking_doc_type(mock_lookup, client):
    """Analyst sees only doc_type=10k/public. A confidential board report should 403."""
    c, _ = client
    mock_lookup.return_value = {"doc_type": "board_report", "confidentiality": "confidential"}
    resp = c.get(
        "/v1/documents/board_minutes_2023.pdf",
        headers={"Authorization": f"Bearer {_token('analyst')}"},
    )
    assert resp.status_code == 403


@patch("src.api.routes.documents._lookup_document_meta")
def test_admin_wildcard_allows_any_doc(mock_lookup, client):
    """Admin has ["*"] for both axes — should pass RBAC; 404 because file isn't on disk."""
    c, tmp = client
    mock_lookup.return_value = {"doc_type": "board_report", "confidentiality": "confidential"}
    resp = c.get(
        "/v1/documents/board_minutes_2023.pdf",
        headers={"Authorization": f"Bearer {_token('admin')}"},
    )
    # RBAC passed but the file isn't stored → 404 in plain words, without naming the server.
    assert resp.status_code == 404
    assert resp.json()["detail"] == "The original document isn't available to open right now."


@patch("src.api.routes.documents._lookup_document_meta")
def test_200_when_rbac_ok_and_file_present(mock_lookup, client):
    c, tmp = client
    mock_lookup.return_value = {"doc_type": "10k", "confidentiality": "public"}
    # Write a real PDF placeholder so FileResponse can stream it
    (tmp / "report.pdf").write_bytes(b"%PDF-1.4\nminimal stub\n%%EOF")
    resp = c.get(
        "/v1/documents/report.pdf",
        headers={"Authorization": f"Bearer {_token('analyst')}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "inline" in resp.headers["content-disposition"]


# ── GET /v1/documents: what each role can search ───────────────────────────────


def _doc(source, company, doc_type, level="public"):
    from src.mcp_server.knowledge import Document

    return Document(
        name=source.removesuffix(".pdf"),
        source_file=source,
        company=company,
        company_name=company.title(),
        fiscal_year=2022,
        doc_type=doc_type,
        confidentiality=level,
        pages=10,
    )


CATALOG = (
    _doc("MSFT_2022_10K.pdf", "microsoft", "10k"),
    _doc("MSFT_2023Q1_10Q.pdf", "microsoft", "10q"),
    _doc("AMZN_2022_10K.pdf", "amazon", "10k"),
    _doc("AMZN_2023_8K.pdf", "amazon", "8k"),
)


@pytest.fixture
def catalog(monkeypatch):
    from src.mcp_server import knowledge

    monkeypatch.setattr(knowledge, "load_documents", lambda: CATALOG)


def _library(client, role):
    c, _ = client
    response = c.get("/v1/documents", headers={"Authorization": f"Bearer {_token(role)}"})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_filings_role_sees_its_counts_and_has_nothing_extra_to_request(client, catalog):
    library = _library(client, "analyst")
    assert (library["documents"], library["companies"]) == (4, 2)
    assert [d["label"] for d in library["doc_types"]] == ["10-K", "10-Q", "8-K"]
    assert library["doc_types"][0]["documents"] == 2
    assert library["requestable"] == []


def test_hr_has_no_documents_and_may_request_the_filings(client, catalog):
    library = _library(client, "hr")
    assert (library["documents"], library["companies"], library["doc_types"]) == (0, 0, [])
    assert library["requestable"] == [
        {
            "key": "filings",
            "label": "SEC filings",
            "detail": "10-K, 10-Q and 8-K reports",
            "documents": 4,
            "doc_type": ["filings"],
            "confidentiality": [],
        }
    ]


def test_a_confidential_group_names_the_level_to_request(client, monkeypatch):
    from src.mcp_server import knowledge

    monkeypatch.setattr(
        knowledge, "load_documents", lambda: (*CATALOG, _doc("BOARD_2023.pdf", "acme", "board_report", "confidential"))
    )
    option = next(o for o in _library(client, "finance")["requestable"] if o["key"] == "board_report")
    assert option["doc_type"] == ["board_report"] and option["confidentiality"] == ["confidential"]
    assert option["label"] == "Board reports" and "confidential documents" in option["detail"]


def test_the_requested_access_is_accepted_by_the_escalation_parser(client, catalog):
    from src.services.escalation import parse_escalation

    option = _library(client, "hr")["requestable"][0]
    line = "/escalate doc_type=" + ",".join(option["doc_type"])
    question, scope = parse_escalation(f"{line}\nMicrosoft revenue?", "hr")
    assert question == "Microsoft revenue?" and scope["escalated"] and "10k" in scope["doc_types"]


def test_admins_see_everything_and_are_offered_no_requests(client, catalog):
    library = _library(client, "admin")
    assert library["documents"] == 4 and library["requestable"] == []


def test_an_unavailable_document_list_is_a_plain_503(client, monkeypatch):
    from src.mcp_server import knowledge

    def down():
        raise ConnectionError("qdrant down")

    monkeypatch.setattr(knowledge, "load_documents", down)
    c, _ = client
    response = c.get("/v1/documents", headers={"Authorization": f"Bearer {_token('analyst')}"})
    assert response.status_code == 503
    assert response.json()["detail"] == "The document list is unavailable right now."
