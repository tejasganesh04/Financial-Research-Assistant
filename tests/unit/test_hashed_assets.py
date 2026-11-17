"""Built interface files are cached for a year; the page itself is always revalidated."""

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from src.api.main import HashedAssets


def test_hashed_assets_are_cached_for_a_year(tmp_path):
    (tmp_path / "index-abc123.js").write_text("console.log(1)")
    client = TestClient(Starlette(routes=[Mount("/assets", HashedAssets(directory=tmp_path))]))
    found = client.get("/assets/index-abc123.js")
    assert found.status_code == 200
    assert found.headers["cache-control"] == "public, max-age=31536000, immutable"
    missing = client.get("/assets/missing.js")
    assert missing.status_code == 404
    assert "immutable" not in missing.headers.get("cache-control", "")
