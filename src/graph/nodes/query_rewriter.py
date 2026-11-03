import logging

from langchain_core.messages import HumanMessage, SystemMessage

from src.config.prompts import QUERY_REWRITER_PROMPT, QUERY_REWRITER_USER_TEMPLATE
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory
from src.services.llm_retry import retry_llm_call
from src.services.provider_errors import raise_if_rate_limited

logger = logging.getLogger(__name__)


def query_rewriter_node(state: RAGState) -> dict:
    """Rewrite the query based on grading feedback to improve retrieval.

    The rewrite call is wrapped in `retry_llm_call` so a transient OpenAI
    hiccup doesn't fall through to "use the original query" (which silently
    wastes a retrieval retry). Only retries on transient errors; permanent
    failures still pass through to the original-query fallback.
    """
    query = state.get("contextualized_query", "")
    grading_results = state.get("grading_results", [])
    retry_count = state.get("retrieval_retry_count", 0)

    # Build feedback from irrelevant chunks
    feedback_lines = [r["reason"] for r in grading_results if not r.get("relevant")]
    feedback = "\n".join(feedback_lines[:3]) if feedback_lines else "Chunks were not relevant to the question."

    llm = None
    try:
        llm = LLMFactory.get_router_llm()
        prompt = QUERY_REWRITER_USER_TEMPLATE.format(query=query, feedback=feedback)

        def _invoke():
            return llm.invoke([
                SystemMessage(content=QUERY_REWRITER_PROMPT),
                HumanMessage(content=prompt),
            ])

        result = retry_llm_call(_invoke, label="query rewriter")
        rewritten = result.content.strip()
        logger.info(f"Query rewritten (attempt {retry_count + 1}): '{query[:50]}' -> '{rewritten[:50]}'")
        return {
            "retrieval_query": rewritten,
            "retrieval_retry_count": retry_count + 1,
        }
    except Exception as e:
        raise_if_rate_limited(e, "query_rewriter", llm=llm)
        logger.error(f"Query rewriting failed after retries: {e}")
        return {
            "retrieval_query": query,
            "retrieval_retry_count": retry_count + 1,
        }
