"""Meaning-based metadata selection and deterministic validation; no provider calls."""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from src.graph.edges import route_after_metadata_extraction
from src.graph.nodes.metadata_extractor import metadata_extractor_node
from src.models.schemas import MetadataExtraction

CATALOG = [
    {"company": "microsoft", "name": "Microsoft", "aliases": ["msft"], "fiscal_years": [2016, 2023]},
    {"company": "3m", "name": "3M", "aliases": ["mmm"], "fiscal_years": [2022, 2023]},
    {"company": "jpmorgan", "name": "JPMorgan", "aliases": ["jpm"], "fiscal_years": [2022]},
]


def select(companies, years, query="question"):
    """Run the node with a fixed model selection over CATALOG."""
    model = MagicMock()
    model.with_structured_output.return_value.invoke.return_value = MetadataExtraction(
        companies=companies, fiscal_years=years, reason="Selected filters."
    )
    with (
        patch("src.graph.nodes.metadata_extractor.available_metadata", return_value=CATALOG),
        patch("src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm", return_value=model),
    ):
        return metadata_extractor_node({"contextualized_query": query}), model


@pytest.mark.parametrize(
    "query,companies,years,expected_companies,searched_years,unavailable",
    [
        ("MSFT revenue in FY2016", ["MSFT"], [2016], ["microsoft"], [2016], False),
        ("Compare Microsoft and 3M in FY2023", ["microsoft", "3m"], [2023], ["microsoft", "3m"], [2023], False),
        ("Compare Microsoft FY2016 and FY2023", ["microsoft"], [2016, 2023], ["microsoft"], [2016, 2023], False),
        # A company is required; questions across all companies are not searched.
        ("Which companies have debt maturing in 2030?", [], [], [], [], True),
        ("Microsoft FY2023 filing about debt maturing in 2030", ["microsoft"], [2023], ["microsoft"], [2023], False),
        # A year without its own filing is covered by a filing up to two years later.
        ("Microsoft FY2015 revenue", ["microsoft"], [2015], ["microsoft"], [2016], False),
        ("3M FY2020 to FY2022 average margin", ["3m"], [2020, 2021, 2022], ["3m"], [2022, 2023], False),
        # Partial coverage still searches what exists.
        (
            "Compare Microsoft and JPMorgan in FY2016",
            ["microsoft", "jpmorgan"],
            [2016],
            ["microsoft", "jpmorgan"],
            [2016],
            False,
        ),
        # No year: the latest available filing year.
        ("Compare Microsoft and MissingCo", ["microsoft", "missingco"], [], ["microsoft"], [2023], False),
        # Refuse only when nothing requested can be covered.
        ("Use only Microsoft's FY2035 filing", ["microsoft"], [2035], ["microsoft"], [], True),
        ("Microsoft filings from FY2010 to FY2012", ["microsoft"], [2010, 2012], ["microsoft"], [], True),
        ("Apple revenue in FY2023", ["apple"], [2023], [], [], True),
        ("JPMorgan FY2016 filing", ["jpmorgan"], [2016], ["jpmorgan"], [], True),
    ],
)
def test_selected_filters_validated_against_catalog(
    query, companies, years, expected_companies, searched_years, unavailable
):
    model = MagicMock()
    model.with_structured_output.return_value.invoke.return_value = MetadataExtraction(
        companies=companies,
        fiscal_years=years,
        reason="Only the stated company and filing periods apply.",
    )
    with (
        patch("src.graph.nodes.metadata_extractor.available_metadata", return_value=CATALOG),
        patch("src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm", return_value=model),
    ):
        result = metadata_extractor_node(
            {"contextualized_query": query, "messages": [HumanMessage(content="PRIVATE OLD HISTORY")]}
        )
    assert result["target_companies"] == expected_companies
    assert result["target_fiscal_years"] == searched_years
    assert result["metadata_unavailable"] is unavailable
    assert route_after_metadata_extraction(result) == ("unavailable" if unavailable else "retrieval")
    assert bool(result["unavailable_message"]) is unavailable
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
    with (
        patch("src.graph.nodes.metadata_extractor.available_metadata", return_value=CATALOG),
        patch("src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm", return_value=model),
        pytest.raises(RuntimeError, match="selection is unavailable"),
    ):
        metadata_extractor_node({"contextualized_query": "Microsoft revenue"})


@pytest.mark.parametrize(
    "outcomes,calls,succeeds",
    [
        (
            [TimeoutError("Request timed out"), MetadataExtraction(companies=["microsoft"], reason="Named company")],
            2,
            True,
        ),
        ([TimeoutError("Request timed out")] * 3, 3, False),
    ],
)
def test_transient_selection_failures_are_retried_without_relaxing_filters(outcomes, calls, succeeds):
    model = MagicMock()
    invoke = model.with_structured_output.return_value.invoke
    invoke.side_effect = outcomes
    with (
        patch("src.graph.nodes.metadata_extractor.available_metadata", return_value=CATALOG),
        patch("src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm", return_value=model),
        patch("src.services.llm_retry.time.sleep"),
    ):
        if succeeds:
            assert metadata_extractor_node({"contextualized_query": "Microsoft revenue"})["target_companies"] == [
                "microsoft"
            ]
        else:
            with pytest.raises(RuntimeError, match="selection is unavailable"):
                metadata_extractor_node({"contextualized_query": "Microsoft revenue"})
    assert invoke.call_count == calls


def test_catalog_failure_stops_before_model_call():
    with (
        patch("src.graph.nodes.metadata_extractor.available_metadata", side_effect=RuntimeError("Catalog unavailable")),
        patch("src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm") as model,
        pytest.raises(RuntimeError, match="Catalog unavailable"),
    ):
        metadata_extractor_node({"contextualized_query": "Microsoft revenue"})
    model.assert_not_called()


def test_empty_query_skips_model_and_catalog():
    with (
        patch("src.graph.nodes.metadata_extractor.available_metadata") as catalog,
        patch("src.graph.nodes.metadata_extractor.LLMFactory.get_router_llm") as model,
    ):
        result = metadata_extractor_node({})
    assert result["target_companies"] == result["target_fiscal_years"] == []
    assert not result["metadata_unavailable"]
    catalog.assert_not_called()
    model.assert_not_called()


def test_messages_list_only_what_this_user_can_access():
    result, _ = select([], [])
    assert result["unavailable_message"] == (
        "I couldn't find a company available to you in your question. "
        "Companies you can ask about: 3M, JPMorgan, Microsoft."
    )
    result, _ = select(["jpmorgan"], [2016])
    assert result["unavailable_message"] == (
        "FY2016 isn't available within your access for JPMorgan. Available years for JPMorgan: FY2022."
    )
    assert result["scope_notes"] == []


def test_partial_scope_and_default_year_are_explained():
    result, _ = select(["microsoft", "apple"], [])
    assert result["scope_notes"] == [
        "Apple isn't available within your access, so it isn't included.",
        "No year was given, so the latest available filings were used: Microsoft FY2023.",
    ]
    result, _ = select(["microsoft", "jpmorgan"], [2016])
    assert result["scope_notes"] == [
        "JPMorgan has no filings for the requested years within your access, so it isn't included."
    ]


def test_display_names_never_reach_the_model():
    _, model = select(["microsoft"], [2023])
    prompt = model.with_structured_output.return_value.invoke.call_args.args[0][1].content
    assert '"name"' not in prompt and '"documents"' not in prompt


def test_research_questions_take_the_research_path():
    result, _ = select(["microsoft"], [2023])
    assert route_after_metadata_extraction({**result, "query_complexity": "research_required"}) == "research_required"
