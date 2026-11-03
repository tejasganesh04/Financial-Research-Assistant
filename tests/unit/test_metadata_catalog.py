"""Catalog reads/cache and effective-scope isolation; no Qdrant server or providers."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services import metadata_catalog as catalog


@pytest.fixture(autouse=True)
def reset_catalog_cache():
    cached_loader = catalog.load_metadata_catalog
    cached_loader.cache_clear()
    yield
    cached_loader.cache_clear()


def test_paginated_metadata_only_read_is_cached(monkeypatch):
    client = MagicMock()
    public = {"company": "microsoft", "fiscal_year": 2016, "doc_type": "10k", "confidentiality": "public"}
    later = {**public, "fiscal_year": "2023"}
    client.scroll.side_effect = [([SimpleNamespace(payload=public), SimpleNamespace(payload=public)], "next"),
                                 ([SimpleNamespace(payload=later), SimpleNamespace(payload={})], None)]
    monkeypatch.setattr(catalog, "get_qdrant_client", lambda: client)
    assert len(catalog.load_metadata_catalog()) == 2
    assert len(catalog.load_metadata_catalog()) == 2
    assert client.scroll.call_count == 2
    for call in client.scroll.call_args_list:
        assert call.kwargs["with_vectors"] is False
        assert "content" not in call.kwargs["with_payload"]
    assert client.scroll.call_args_list[1].kwargs["offset"] == "next"


def test_failed_catalog_read_is_not_cached(monkeypatch):
    client = MagicMock()
    client.scroll.side_effect = [RuntimeError("database unavailable"), ([], None)]
    monkeypatch.setattr(catalog, "get_qdrant_client", lambda: client)
    with pytest.raises(RuntimeError, match="catalog is unavailable"):
        catalog.load_metadata_catalog()
    assert catalog.load_metadata_catalog() == ()
    assert client.scroll.call_count == 2


def test_catalog_respects_normal_role_and_explicit_escalation(monkeypatch):
    monkeypatch.setattr(catalog, "load_metadata_catalog", lambda: (
        ("microsoft", 2016, "10k", "public"),
        ("microsoft", 2023, "board_report", "confidential"),
        ("acme", 2023, "expense_policy", "internal"),
    ))
    normal = catalog.available_metadata({"user_role": "analyst"})
    assert [(r["company"], r["fiscal_years"]) for r in normal] == [("microsoft", [2016])]
    hr = catalog.available_metadata({"user_role": "hr"})
    assert [r["company"] for r in hr] == ["acme"]
    expanded = catalog.available_metadata({"user_role": "analyst", "access_scope": {
        "doc_types": ["10k", "board_report"], "confidentiality": ["public", "confidential"],
    }})
    assert expanded[0]["fiscal_years"] == [2016, 2023]
    assert len(catalog.available_metadata({"user_role": "admin"})) == 2
