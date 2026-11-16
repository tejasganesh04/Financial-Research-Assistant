"""Unit tests for the auth routes that need no database: /v1/auth/me and the no-database answers.

Sign-in, renewal and administration against PostgreSQL are in tests/integration/test_accounts.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.services.auth_service import create_token


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app.state, "pool", None, raising=False)
    return TestClient(app)


# ── Without the account database ─────────────────────────────────────────────


def test_sign_in_without_the_database_is_a_clear_503(client):
    resp = client.post("/v1/auth/login", json={"username": "finance", "password": "x"})
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["detail"]


def test_there_is_no_sign_up_or_manual_mcp_token_endpoint(client):
    assert client.post("/v1/auth/signup", json={}).status_code in (404, 405)
    assert client.post("/v1/auth/register", json={}).status_code in (404, 405)
    assert client.post("/v1/auth/mcp-token").status_code in (404, 405)


# ── GET /v1/auth/me ──────────────────────────────────────────────────────────


def _token(role: str = "finance", user_id: str = "finance", department: str = "FP&A") -> str:
    return create_token(user_id=user_id, name=f"Test {role.title()}", role=role, department=department)


def test_me_returns_identity_and_permissions(client):
    resp = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "finance"
    assert body["role"] == "finance"
    perms = body["permissions"]
    assert "10k" in perms["allowed_doc_types"]
    assert "max_results" not in perms
    # The approval policy is explicit and shared across roles.
    assert "approval_policy" in perms


def test_me_requires_auth(client):
    resp = client.get("/v1/auth/me")
    assert resp.status_code == 401


def test_me_rejects_invalid_token(client):
    resp = client.get("/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
