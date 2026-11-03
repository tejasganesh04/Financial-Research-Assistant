"""Serving the UI must preserve the API and work without a frontend build."""
from fastapi.testclient import TestClient

from src.api import main


def test_root_serves_built_react_page(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text('<html><body><div id="root"></div></body></html>')
    monkeypatch.setattr(main, "FRONTEND_DIR", tmp_path)
    response = TestClient(main.app).get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-cache"


def test_api_still_works_and_unknown_api_is_not_frontend_html(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "FRONTEND_DIR", tmp_path)
    client = TestClient(main.app)
    assert client.get("/v1/health").json() == {"status": "ok"}
    assert client.get("/v1/does-not-exist").status_code == 404
    assert client.get("/").json()["name"] == "Financial Research Assistant API"
    assert client.get("/favicon.svg").status_code == 404
