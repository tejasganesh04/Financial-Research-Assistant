"""Verify evidence support, with a bounded retry on failure or uncertainty."""

import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from src.config.prompts import (
    HALLUCINATION_CHECK_SYSTEM_PROMPT,
    HALLUCINATION_CHECK_USER_TEMPLATE,
)
from src.config.settings import settings
from src.models.schemas import HallucinationCheck
from src.models.state import RAGState
from src.services.escalation import is_escalated
from src.services.llm_factory import LLMFactory
from src.services.provider_errors import raise_if_rate_limited

logger = logging.getLogger(__name__)


def _build_system_message(llm) -> SystemMessage:
    """Build the checker instructions; add an Anthropic cache marker when supported."""
    if isinstance(llm, ChatAnthropic):
        return SystemMessage(content=[
            {
                "type": "text",
                "text": HALLUCINATION_CHECK_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ])
    return SystemMessage(content=HALLUCINATION_CHECK_SYSTEM_PROMPT)


def hallucination_checker_node(state: RAGState) -> dict:
    """Check if the generated answer is grounded in the retrieved sources."""
    answer = state.get("generated_answer", "")
    chunks = state.get("relevant_chunks", [])

    if not answer or not chunks:
        return {"hallucination_status": "unverified", "hallucination_score": 0.0,
                "hallucination_feedback": "",
                "generation_retry_count": settings.MAX_GENERATION_RETRIES}

    sources = "\n\n---\n\n".join(
        f"[{chunk.get('metadata', {}).get('source_file', 'Unknown')}]\n"
        f"{chunk.get('raw_content') or chunk.get('content', '')}"
        for chunk in chunks
    )

    user_prompt = HALLUCINATION_CHECK_USER_TEMPLATE.format(sources=sources, answer=answer)
    retry_count = state.get("generation_retry_count", 0)

    llm = None
    try:
        high_stakes = is_escalated(state)
        llm = (LLMFactory.get_high_stakes_hallucination_llm() if high_stakes
               else LLMFactory.get_hallucination_llm())
        structured_llm = llm.with_structured_output(HallucinationCheck)
        result: HallucinationCheck = structured_llm.invoke([
            _build_system_message(llm),
            HumanMessage(content=user_prompt),
        ])

        logger.info(f"Hallucination check: grounded={result.grounded}, score={result.score:.2f}")
        from src.services.event_log import emit
        emit("hallucination", grounded=result.grounded, score=result.score,
             retry_count=retry_count, will_retry=not result.grounded or result.score < settings.HALLUCINATION_THRESHOLD)

        is_grounded = result.grounded and result.score >= settings.HALLUCINATION_THRESHOLD
        return {
            "hallucination_status": "grounded" if is_grounded else "hallucinated",
            "hallucination_score": result.score,
            # Save actionable feedback only when the check failed. The generator verifies it against sources.
            "hallucination_feedback": "" if is_grounded else result.explanation.strip(),
            "generation_retry_count": retry_count if is_grounded else retry_count + 1,
        }
    except Exception as e:
        raise_if_rate_limited(e, "hallucination_checker", llm=llm)
        logger.error(f"Hallucination check failed, marking unverified: {type(e).__name__}")
        return {
            "hallucination_status": "unverified",
            "hallucination_score": 0.0,
            "hallucination_feedback": "",  # An unavailable checker has no grounded criticism to offer.
            "generation_retry_count": retry_count + 1,
        }
