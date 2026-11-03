import logging

from src.models.state import RAGState
from langchain_core.messages import AIMessage
from src.config.settings import settings

logger = logging.getLogger(__name__)


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
            sources.append({
                "file": source_file,
                "page": meta.get("page_number"),
                "section": meta.get("section_header", ""),
                "doc_type": meta.get("doc_type", ""),
            })

    return sources


def response_formatter_node(state: RAGState) -> dict:
    """Format the final response with citations and metadata."""
    answer = state.get("generated_answer", "")
    chunks = state.get("relevant_chunks", [])
    hallucination_score = state.get("hallucination_score", 0.0)
    hallucination_status = state.get("hallucination_status", "unknown")

    sources = source_references(chunks)

    # Add disclaimer if hallucination check was uncertain
    if answer and (hallucination_status != "grounded" or hallucination_score < settings.HALLUCINATION_THRESHOLD):
        answer = (
            "**Note:** This answer could not be fully verified against source documents. "
            "Please verify the information independently.\n\n" + answer
        )

    metadata = {
        "sources": sources,
        "confidence": hallucination_score,
        "chunks_used": len(chunks),
        "hallucination_status": hallucination_status,
    }

    return {
        "final_response": answer,
        "messages": [AIMessage(content=answer, additional_kwargs={"sources": sources, "confidence": hallucination_score,
                                                                "request_id": state.get("request_id")})],
        "response_metadata": metadata,
    }
