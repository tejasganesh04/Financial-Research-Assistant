"""Ask the small model for justified company/year filters, then validate against real metadata."""

import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from src.config.prompts import METADATA_EXTRACTOR_PROMPT, METADATA_EXTRACTOR_USER_TEMPLATE
from src.models.schemas import MetadataExtraction
from src.models.state import RAGState
from src.services.company_registry import canonical_company_slug
from src.services.llm_factory import LLMFactory
from src.services.llm_retry import retry_llm_call
from src.services.metadata_catalog import available_metadata
from src.services.provider_errors import raise_if_rate_limited
from src.services.request_trace import record

logger = logging.getLogger(__name__)

# A 10-K shows its own year and the two before it; 10-Qs and releases show one earlier year.
COVERAGE_YEARS = 2


def metadata_extractor_node(state: RAGState) -> dict:
    """One selection call on the contextualized question; no regex extraction or history fallback."""
    query = state.get("contextualized_query") or ""
    if not query:
        return {
            "target_companies": [],
            "target_fiscal_years": [],
            "metadata_filter_reason": "Empty working question.",
            "metadata_unavailable": False,
        }

    catalog = available_metadata(state)
    record(
        "metadata_catalog",
        companies=len(catalog),
        company_years={row["company"]: row["fiscal_years"] for row in catalog},
    )
    llm = None
    try:
        llm = LLMFactory.get_router_llm()
        kwargs = (
            {"method": "json_schema"}
            if isinstance(llm, ChatGroq) and llm.model_name.startswith("openai/gpt-oss-")
            else {}
        )
        structured = llm.with_structured_output(MetadataExtraction, **kwargs)
        messages = [
            SystemMessage(content=METADATA_EXTRACTOR_PROMPT),
            HumanMessage(content=METADATA_EXTRACTOR_USER_TEMPLATE.format(catalog=json.dumps(catalog), query=query)),
        ]
        # Retry transient failures; never fall back to unfiltered search.
        result = retry_llm_call(lambda: structured.invoke(messages), label="metadata extractor")
        # Validate even a provider/test returning a dictionary rather than a Pydantic object.
        selected = result if isinstance(result, MetadataExtraction) else MetadataExtraction.model_validate(result)
    except Exception as error:
        raise_if_rate_limited(error, "metadata_extractor", llm=llm)
        logger.warning("Metadata filter selection failed: %s", type(error).__name__)
        raise RuntimeError("Metadata filter selection is unavailable. Please try again.") from error

    available = {row["company"]: set(row["fiscal_years"]) for row in catalog}
    companies = []
    missing_companies = []
    for name in selected.companies:
        slug = name.strip().lower()
        if slug not in available:
            slug = canonical_company_slug(name)
        if slug not in available:
            missing_companies.append(name)
        elif slug not in companies:
            companies.append(slug)
    years = list(dict.fromkeys(selected.fiscal_years))
    # Filings report their own year plus earlier comparison years, so a requested year can be
    # answered from filings dated up to COVERAGE_YEARS later. Search every covering filing.
    scope = companies if selected.companies else list(available)

    def covering(company: str, year: int) -> set[int]:
        return {filed for filed in available[company] if year <= filed <= year + COVERAGE_YEARS}

    allowed_years = sorted({filed for company in scope for year in years for filed in covering(company, year)})
    uncovered_years = [year for year in years if not any(covering(company, year) for company in scope)]
    companies_without_coverage = [
        company for company in companies if years and not any(covering(company, year) for year in years)
    ]
    # Refuse only when nothing requested can exist; partial gaps are left for the answer to report.
    unavailable = bool(selected.companies and not companies) or bool(years and not allowed_years)
    reason = selected.reason
    if unavailable:
        reason += " Requested metadata unavailable within the effective search scope."
    elif missing_companies or uncovered_years or companies_without_coverage:
        reason += " Some requested companies or years are not covered; searching the covered filings."
    record(
        "metadata_filter_selection",
        proposed_companies=selected.companies,
        proposed_fiscal_years=years,
        searched_fiscal_years=allowed_years,
        missing_companies=missing_companies,
        uncovered_years=uncovered_years,
        companies_without_coverage=companies_without_coverage,
        reason=reason,
        unavailable=unavailable,
    )
    return {
        "target_companies": companies,
        "target_fiscal_years": allowed_years,
        "metadata_filter_reason": reason,
        "metadata_unavailable": unavailable,
    }
