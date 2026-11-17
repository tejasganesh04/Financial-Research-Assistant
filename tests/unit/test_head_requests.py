"""Uptime monitors often send HEAD; the public, read-only routes answer it without a body."""

import pytest
from fastapi.testclient import TestClient

from src.api.main import SPA_PATHS, app


@pytest.mark.parametrize("path", ["/", "/v1/health", "/version", *SPA_PATHS])
def test_head_is_answered_like_get(path):
    client = TestClient(app)
    head = client.head(path)
    get = client.get(path)
    # 404 for pages when the interface is not built (as in the backend CI job); never 405.
    assert head.status_code == get.status_code
    assert head.status_code in (200, 404)
    assert head.content == b""
    assert head.headers.get("content-type") == get.headers.get("content-type")


def test_head_is_not_allowed_where_get_is_not():
    client = TestClient(app)
    assert client.head("/v1/admin/users").status_code in (401, 405)
