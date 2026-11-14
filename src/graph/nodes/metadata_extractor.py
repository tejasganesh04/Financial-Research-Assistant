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
            "unavailable_message": "",
            "scope_notes": [],
        }

    catalog = available_metadata(state)
    record(
        "metadata_catalog",
        companies=len(catalog),
        company_years={row["company"]: row["fiscal_years"] for row in catalog},
    )
    model_catalog = [{key: row[key] for key in ("company", "aliases", "fiscal_years")} for row in catalog]
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
            HumanMessage(
                content=METADATA_EXTRACTOR_USER_TEMPLATE.format(catalog=json.dumps(model_catalog), query=query)
            ),
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
    names = {row["company"]: row["name"] for row in catalog}
    companies = []
    missing_companies = []
    for name in selected.companies:
        slug = name.strip().lower()
        if slug not in available:
            slug = canonical_company_slug(name)
        if slug not in available:
            missing_companies.append(name.strip())
        elif slug not in companies:
            companies.append(slug)
    years = list(dict.fromkeys(selected.fiscal_years))
    # Every message describes only what this user's access scope contains.
    company_list = ", ".join(sorted(names.values())) or "none"

    def covering(company: str, year: int) -> set[int]:
        # Filings report their own year plus earlier comparison years.
        return {filed for filed in available[company] if year <= filed <= year + COVERAGE_YEARS}

    notes = [f"{display(name)} isn't available within your access, so it isn't included." for name in missing_companies]
    uncovered_years: list[int] = []
    unavailable_message = ""
    if not companies:
        # A company is required: none named, unknown, or outside this user's access.
        unavailable_message = (
            f"I couldn't find a company available to you in your question. Companies you can ask about: {company_list}."
        )
        allowed_years = []
    elif years:
        allowed_years = sorted({filed for company in companies for year in years for filed in covering(company, year)})
        uncovered_years = [year for year in years if not any(covering(company, year) for company in companies)]
        if not allowed_years:
            unavailable_message = (
                f"{fiscal_years(years)} isn't available within your access for {join_names(companies, names)}. "
                + " ".join(
                    f"Available years for {names[c]}: {fiscal_years(sorted(available[c])) or 'none'}."
                    for c in companies
                )
            )
        elif uncovered_years:
            notes.append(f"{fiscal_years(uncovered_years)} isn't available within your access, so it isn't included.")
        notes += [
            f"{names[c]} has no filings for the requested years within your access, so it isn't included."
            for c in companies
            if allowed_years and not any(covering(c, year) for year in years)
        ]
    else:
        # No year given: search each company's latest available filing year and say so.
        latest = {c: max(available[c]) for c in companies if available[c]}
        allowed_years = sorted(set(latest.values()))
        if latest:
            notes.append(
                "No year was given, so the latest available filings were used: "
                + ", ".join(f"{names[c]} FY{year}" for c, year in latest.items())
                + "."
            )
    unavailable = bool(unavailable_message)
    reason = selected.reason + (
        " Requested metadata unavailable within the effective search scope." if unavailable else ""
    )
    record(
        "metadata_filter_selection",
        proposed_companies=selected.companies,
        proposed_fiscal_years=years,
        searched_fiscal_years=allowed_years,
        missing_companies=missing_companies,
        uncovered_years=uncovered_years,
        scope_notes=notes,
        reason=reason,
        unavailable=unavailable,
    )
    return {
        "target_companies": companies,
        "target_fiscal_years": allowed_years,
        "metadata_filter_reason": reason,
        "metadata_unavailable": unavailable,
        "unavailable_message": unavailable_message,
        "scope_notes": [] if unavailable else notes,
    }


def display(name: str) -> str:
    """Readable form of a company name the model returned, such as 'apple' or 'mcdonalds'."""
    name = name.replace("_", " ").strip()
    return name.title() if name.islower() else name


def fiscal_years(years: list[int]) -> str:
    return ", ".join(f"FY{year}" for year in years)


def join_names(companies: list[str], names: dict[str, str]) -> str:
    return " and ".join(names[c] for c in companies)
