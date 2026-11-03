"""The reduced API exposes only the supported features; no provider calls."""
from fastapi.testclient import TestClient

from src.api.main import app
from src.config.rbac_config import get_permissions


def test_supported_route_groups_and_no_legacy_aliases():
    paths = {route.path for route in app.routes}
    assert "/v1/auth/login" in paths
    assert "/v1/conversations/{thread_id}/messages" in paths
    assert "/v1/conversations/{thread_id}/messages/stream" in paths
    assert "/v1/approvals/{thread_id}/approve" in paths
    assert "/v1/approvals/{thread_id}/reject" in paths
    for prefix in ("/auth", "/chat", "/threads", "/hitl", "/v1/chat", "/v1/threads",
                   "/v1/hitl", "/v1/admin", "/v1/ingest", "/v1/warm"):
        assert not any(path == prefix or path.startswith(prefix + "/") for path in paths)


def test_permissions_are_available_without_database(monkeypatch):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("No role DB lookup")))
    assert get_permissions("analyst")["allowed_confidentiality"] == ["public"]
    assert "board_report" not in get_permissions("finance")["allowed_doc_types"]
    assert "confidential" in get_permissions("c_level")["allowed_confidentiality"]


def test_health_does_not_need_login():
    assert TestClient(app).get("/v1/health").json() == {"status": "ok"}
