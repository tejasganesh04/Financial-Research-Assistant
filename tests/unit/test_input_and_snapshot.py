"""Input privacy and snapshot compatibility checks; no provider calls."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.graph.nodes.guardrails import _contextualize_query
from src.services.guardrails_service import detect_pii


def test_company_person_false_positive_is_exempt_but_actual_person_and_email_are_not():
    text = "Nike revenue for John Smith, john@example.com"
    person = SimpleNamespace(entity_type="PERSON", start=17, end=27, score=0.9)
    email = SimpleNamespace(entity_type="EMAIL_ADDRESS", start=29, end=len(text), score=1)
    company = SimpleNamespace(entity_type="PERSON", start=0, end=4, score=0.9)
    analyzer = MagicMock()
    analyzer.analyze.return_value = [company, person, email]
    anonymizer = MagicMock()
    anonymizer.anonymize.return_value.text = "Nike revenue for <PERSON>, <EMAIL_ADDRESS>"
    with patch("src.services.guardrails_service._get_presidio_engines", return_value=(analyzer, anonymizer)):
        redacted, entities = detect_pii(text)
    assert anonymizer.anonymize.call_args.kwargs["analyzer_results"] == [email, person]
    assert "Nike" in redacted
    assert {e["type"] for e in entities} == {"PERSON", "EMAIL_ADDRESS"}


def test_contextualization_uses_already_sanitized_history():
    llm = MagicMock()
    llm.invoke.return_value.content = "Microsoft revenue?"
    with patch(
        "src.graph.nodes.guardrails.LLMFactory.get_router_llm", return_value=llm
    ):
        _contextualize_query("What about Microsoft?", [HumanMessage(content="<EMAIL_ADDRESS>"),
            AIMessage(content="Earlier answer"), HumanMessage(content="What about Microsoft?")])
    prompt = llm.invoke.call_args.args[0][-1].content
    assert "secret@example.com" not in prompt
    assert "<EMAIL_ADDRESS>" in prompt


def load_seed_module():
    path = Path(__file__).resolve().parents[2] / "scripts/seed_from_hf.py"
    spec = importlib.util.spec_from_file_location("snapshot_seed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("change", ["model", "dim", "distance", "provider", "sparse"])
def test_incompatible_snapshot_is_rejected_before_restore(tmp_path, change):
    seed = load_seed_module()
    manifest = {"dense_embedding": {"model": "voyage/voyage-finance-2", "dim": 1024,
        "distance": "Cosine", "provider": "Voyage AI"},
        "sparse_embedding": {"encoder": "qdrant-fastembed-bm25"}}
    if change == "sparse":
        manifest["sparse_embedding"]["encoder"] = "different-encoder"
    else:
        manifest["dense_embedding"][change] = {"model": "different-model", "dim": 1536,
            "distance": "Dot", "provider": "different-provider"}[change]
    file = tmp_path / "manifest.json"
    file.write_text(json.dumps(manifest))
    with patch.object(seed.settings, "EMBEDDING_MODEL", "voyage-finance-2"), patch.object(seed.settings, "EMBEDDING_PROVIDER", "voyage"):
        with pytest.raises(ValueError):
            seed._verify_manifest(file, 1024)


def test_missing_snapshot_manifest_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        load_seed_module()._verify_manifest(tmp_path / "missing.json", 1024)


def test_matching_snapshot_is_accepted(tmp_path):
    seed = load_seed_module()
    manifest = {"dense_embedding": {"model": "voyage/voyage-finance-2", "dim": 1024,
        "distance": "Cosine", "provider": "Voyage AI"},
        "sparse_embedding": {"encoder": "qdrant-fastembed-bm25"}}
    file = tmp_path / "manifest.json"
    file.write_text(json.dumps(manifest))
    with patch.object(seed.settings, "EMBEDDING_MODEL", "voyage-finance-2"), patch.object(seed.settings, "EMBEDDING_PROVIDER", "voyage"):
        assert seed._verify_manifest(file, 1024) == manifest




@pytest.mark.parametrize("model", ["openai/gpt-oss-20b", "llama-3.3-70b-versatile"])
def test_groq_router_parses_native_and_tool_output_without_calling_provider(model):
    from langchain_groq import ChatGroq
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from src.graph.nodes.router import router_node
    llm = ChatGroq(model=model, api_key="offline-placeholder")
    decision = {"intent": "retrieval", "reason": "Comparison", "complexity": "research_required"}
    message = AIMessage(content=json.dumps(decision)) if model.startswith("openai/gpt-oss-") else AIMessage(content="", tool_calls=[{
        "name": "RouterDecision", "args": decision,
        "id": "offline-tool-call", "type": "tool_call"
    }])
    result = ChatResult(generations=[ChatGeneration(message=message)])
    with patch("src.graph.nodes.router.LLMFactory.get_router_llm", return_value=llm), patch.object(
        ChatGroq, "_generate", return_value=result
    ):
        assert router_node({"contextualized_query": "Compare Microsoft revenue"}) == {
            "query_intent": "retrieval", "query_complexity": "research_required"}


@pytest.mark.parametrize("dimension", [1024, 3])
def test_restore_refuses_existing_or_incompatible_collection_before_deleting(tmp_path, dimension):
    import pyarrow as pa
    import pyarrow.parquet as pq
    seed = load_seed_module()
    file = tmp_path / "chunks.parquet"
    pq.write_table(pa.table({"chunk_id": ["sample"], "content": ["sample"],
        "dense_vector": [[0.0] * dimension], "sparse_indices": [[1]], "sparse_values": [[1.0]]}), file)
    client = MagicMock()
    client.collection_exists.return_value = True
    with patch("qdrant_client.QdrantClient", return_value=client), patch.object(seed.settings, "EMBEDDING_DIMENSIONS", 1024):
        with pytest.raises(ValueError):
            seed._restore(file, "existing", 1)
    client.delete_collection.assert_not_called()
    client.upsert.assert_not_called()


@pytest.mark.parametrize("year", ["fy2016", "FY 2016", "fiscal year 2016", "2016"])
def test_fiscal_year_false_person_detection_preserves_year_and_redacts_real_pii(year):
    text = f"microsoft cog {year} for John Smith"
    year_start = text.index(year)
    person_start = text.index("John Smith")
    false_person = SimpleNamespace(entity_type="PERSON", start=year_start, end=year_start+len(year), score=0.9)
    actual_person = SimpleNamespace(entity_type="PERSON", start=person_start, end=len(text), score=0.9)
    analyzer, anonymizer = MagicMock(), MagicMock()
    analyzer.analyze.return_value = [false_person, actual_person]
    anonymizer.anonymize.return_value.text = f"microsoft cog {year} for <PERSON>"
    with patch("src.services.guardrails_service._get_presidio_engines", return_value=(analyzer, anonymizer)):
        redacted, entities = detect_pii(text)
    assert year in redacted
    assert anonymizer.anonymize.call_args.kwargs["analyzer_results"] == [actual_person]
    assert len(entities) == 1
