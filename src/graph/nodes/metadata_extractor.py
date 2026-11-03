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
from src.services.metadata_catalog import available_metadata
from src.services.provider_errors import raise_if_rate_limited
from src.services.request_trace import record

logger = logging.getLogger(__name__)


def metadata_extractor_node(state: RAGState) -> dict:
    """One selection call on the contextualized question; no regex extraction or history fallback."""
    query = state.get("contextualized_query") or ""
    if not query:
        return {"target_companies": [], "target_fiscal_years": [],
                "metadata_filter_reason": "Empty working question.", "metadata_unavailable": False}

    catalog = available_metadata(state)
    record("metadata_catalog", companies=len(catalog),
           company_years={row["company"]: row["fiscal_years"] for row in catalog})
    llm = None
    try:
        llm = LLMFactory.get_router_llm()
        kwargs = {"method": "json_schema"} if isinstance(llm, ChatGroq) and llm.model_name.startswith("openai/gpt-oss-") else {}
        structured = llm.with_structured_output(MetadataExtraction, **kwargs)
        result = structured.invoke([
            SystemMessage(content=METADATA_EXTRACTOR_PROMPT),
            HumanMessage(content=METADATA_EXTRACTOR_USER_TEMPLATE.format(catalog=json.dumps(catalog), query=query)),
        ])
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
    # Validate the selection against company/year relationships, not unrelated global lists.
    scoped = {company: values for company, values in available.items() if not companies or company in companies}
    available_years = {year for values in scoped.values() for year in values}
    missing_years = [year for year in years if year not in available_years]
    companies_without_year = [company for company in companies if years and not available[company].intersection(years)]
    unavailable = bool(missing_companies or missing_years or companies_without_year)
    reason = selected.reason
    if unavailable:
        reason += " Requested metadata unavailable within the effective search scope."
    record("metadata_filter_selection", proposed_companies=selected.companies, proposed_fiscal_years=years,
           missing_companies=missing_companies, missing_years=missing_years,
           companies_without_requested_year=companies_without_year, reason=reason, unavailable=unavailable)
    return {"target_companies": companies, "target_fiscal_years": years,
            "metadata_filter_reason": reason, "metadata_unavailable": unavailable}
