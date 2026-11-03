"""Meaning-based metadata selection and deterministic validation; no provider calls."""
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from src.graph.edges import route_after_metadata_extraction
from src.graph.nodes.metadata_extractor import metadata_extractor_node
from src.models.schemas import MetadataExtraction

CATALOG = [
    {"company": "microsoft", "aliases": ["msft"], "fiscal_years": [2016, 2023]},
    {"company": "3m", "aliases": ["mmm"], "fiscal_years": [2022, 2023]},
    {"company": "jpmorgan", "aliases": ["jpm"], "fiscal_years": [2022]},
]


@pytest.mark.parametrize("query,companies,years,expected_companies,unavailable", [
    ("MSFT revenue in FY2016", ["MSFT"], [2016], ["microsoft"], False),
    ("Compare Microsoft and 3M in FY2023", ["microsoft", "3m"], [2023], ["microsoft", "3m"], False),
    ("Compare Microsoft FY2016 and FY2023", ["microsoft"], [2016, 2023], ["microsoft"], False),
    ("Which companies have debt maturing in 2030?", [], [], [], False),
    ("Microsoft FY2023 filing about debt maturing in 2030", ["microsoft"], [2023], ["microsoft"], False),
    ("Use only Microsoft's FY2035 filing", ["microsoft"], [2035], ["microsoft"], True),
    ("Microsoft filings from FY2010 to FY2012", ["microsoft"], [2010, 2012], ["microsoft"], True),
    ("Apple revenue in FY2023", ["apple"], [2023], [], True),
    ("JPMorgan FY2016 filing", ["jpmorgan"], [2016], ["jpmorgan"], True),
    ("Compare Microsoft and MissingCo", ["microsoft", "missingco"], [], ["microsoft"], True),
])
def test_selected_filters_validated_against_catalog(query, companies, years, expected_companies, unavailable):
    model = MagicMock()
    model.with_structured_output.return_value.invoke.return_value = MetadataExtraction(
        companies=companies, fiscal_years=years, reason="Only the stated company and filing periods apply.",
    )
    with patch("src.graph.nodes.metadata_extractor.available_metadata", return_value=CATALOG), patch(
        "src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm", return_value=model
    ):
        result = metadata_extractor_node({"contextualized_query": query, "messages": [HumanMessage(content="PRIVATE OLD HISTORY")]})
    assert result["target_companies"] == expected_companies
    assert result["target_fiscal_years"] == years
    assert result["metadata_unavailable"] is unavailable
    assert route_after_metadata_extraction(result) == ("unavailable" if unavailable else "continue")
    prompt = model.with_structured_output.return_value.invoke.call_args.args[0]
    assert isinstance(prompt[0], SystemMessage) and '"fiscal_years": [2016, 2023]' not in prompt[0].content
    assert isinstance(prompt[1], HumanMessage) and query in prompt[1].content
    assert '"fiscal_years": [2016, 2023]' in prompt[1].content
    assert "PRIVATE OLD HISTORY" not in " ".join(str(m.content) for m in prompt)


@pytest.mark.parametrize("failure", [RuntimeError("Model unavailable"), {"companies": "not a list"}])
def test_selection_failure_does_not_fall_back_to_unrestricted_search(failure):
    model = MagicMock()
    if isinstance(failure, Exception):
        model.with_structured_output.return_value.invoke.side_effect = failure
    else:
        model.with_structured_output.return_value.invoke.return_value = failure
    with patch("src.graph.nodes.metadata_extractor.available_metadata", return_value=CATALOG), patch(
        "src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm", return_value=model
    ), pytest.raises(RuntimeError, match="selection is unavailable"):
        metadata_extractor_node({"contextualized_query": "Microsoft revenue"})


def test_catalog_failure_stops_before_model_call():
    with patch("src.graph.nodes.metadata_extractor.available_metadata", side_effect=RuntimeError("Catalog unavailable")), patch(
        "src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm"
    ) as model, pytest.raises(RuntimeError, match="Catalog unavailable"):
        metadata_extractor_node({"contextualized_query": "Microsoft revenue"})
    model.assert_not_called()


def test_empty_query_skips_model_and_catalog():
    with patch("src.graph.nodes.metadata_extractor.available_metadata") as catalog, patch(
        "src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm"
    ) as model:
        result = metadata_extractor_node({})
    assert result["target_companies"] == result["target_fiscal_years"] == []
    assert not result["metadata_unavailable"]
    catalog.assert_not_called()
    model.assert_not_called()
