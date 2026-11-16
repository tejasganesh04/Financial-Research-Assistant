import logging

from langchain_core.messages import AIMessage

from src.config.prompts import NO_INFO_RESPONSE
from src.config.settings import settings
from src.models.state import RAGState
from src.services.metadata_catalog import available_metadata

logger = logging.getLogger(__name__)

# The generator's exact wording when its evidence cannot answer the question.
REFUSAL = "I don't have enough information"
DOC_LABELS = {"10k": "10-K", "10q": "10-Q", "8k": "8-K"}


def is_refusal(answer: str) -> bool:
    text = answer.strip()
    return text.startswith(REFUSAL) and len(text) < 300


def scope_note(state: RAGState) -> str:
    notes = state.get("scope_notes") or []
    return "\n\n**Note:** " + " ".join(notes) if notes else ""


def calculation_note(state: RAGState) -> str:
    """Say how calculated figures were produced, so a reader knows what to check."""
    if not any(item.get("steps") for item in state.get("calculations") or []):
        return ""
    return (
        "\n\n**Calculations:** worked out with an exact calculator from the figures cited above. "
        "Check that those figures are the right ones for your question."
    )


def not_found_message(state: RAGState) -> str:
    """Say what was searched, limited to the documents this user can access."""
    companies = state.get("target_companies") or []
    try:
        rows = [row for row in available_metadata(state) if row["company"] in companies]
    except Exception:  # noqa: BLE001 - the generic message is still correct without the document list.
        rows = []
    if not rows:
        return NO_INFO_RESPONSE
    searched = "; ".join(
        f"{row['name']} ({', '.join(f'FY{year} {DOC_LABELS.get(kind, kind)}' for year, kind in row['documents'])})"
        for row in rows
    )
    return (
        f"I couldn't find this in the filings available to you for {searched}. "
        "Some details are disclosed only in other filings, such as proxy statements, which aren't included."
    )


def source_references(chunks: list[dict]) -> list[dict]:
    sources = []
    seen = set()
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source_file = meta.get("source_file", "Unknown")
        # Keep distinct pages from the same filing; the first retrieved page
        # may not contain the fact cited by the generated answer.
        source_key = (source_file, meta.get("page_number"))
        if source_key not in seen:
            seen.add(source_key)
            sources.append(
                {
                    "file": source_file,
                    "page": meta.get("page_number"),
                    "section": meta.get("section_header", ""),
                    "doc_type": meta.get("doc_type", ""),
                }
            )

    return sources


def response_formatter_node(state: RAGState) -> dict:
    """Format the final response with citations and metadata."""
    answer = state.get("generated_answer", "")
    chunks = state.get("relevant_chunks", [])
    hallucination_score = state.get("hallucination_score", 0.0)
    hallucination_status = state.get("hallucination_status", "unknown")

    sources = source_references(chunks)

    if is_refusal(answer):
        # Nothing to verify or cite in a refusal; say what was searched instead.
        answer, sources = not_found_message(state) + scope_note(state), []
    else:
        # Add disclaimer if hallucination check was uncertain
        if answer and (hallucination_status != "grounded" or hallucination_score < settings.HALLUCINATION_THRESHOLD):
            answer = (
                "**Note:** This answer could not be fully verified against source documents. "
                "Please verify the information independently.\n\n" + answer
            )
        answer += calculation_note(state) + scope_note(state)

    metadata = {
        "sources": sources,
        "confidence": hallucination_score,
        "chunks_used": len(chunks),
        "hallucination_status": hallucination_status,
        "calculations": [step for item in state.get("calculations") or [] for step in item.get("steps", [])],
    }

    return {
        "final_response": answer,
        "messages": [
            AIMessage(
                content=answer,
                additional_kwargs={
                    "sources": sources,
                    "confidence": hallucination_score,
                    "request_id": state.get("request_id"),
                },
            )
        ],
        "response_metadata": metadata,
    }
