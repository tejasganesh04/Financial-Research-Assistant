import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.config.prompts import QUERY_CONTEXTUALIZER_PROMPT, QUERY_CONTEXTUALIZER_USER_TEMPLATE
from src.models.state import RAGState
from src.services.guardrails_service import (
    check_injection_llm,
    check_injection_llm_guard,
)
from src.services.llm_factory import LLMFactory
from src.services.provider_errors import raise_if_rate_limited

logger = logging.getLogger(__name__)


def _format_history(messages: list, max_turns: int = 3) -> str:
    """Format the last N turns of conversation (excluding the current message) for the contextualizer."""
    prior = messages[:-1]
    if not prior:
        return ""
    # Keep only the last max_turns*2 messages (human+ai pairs)
    recent = prior[-(max_turns * 2):]
    lines = []
    for msg in recent:
        if isinstance(msg, HumanMessage):
            lines.append(f"User: {msg.content}")
        elif isinstance(msg, AIMessage):
            # Truncate long AI responses to keep the context window small
            content = msg.content[:400] + ("..." if len(msg.content) > 400 else "")
            lines.append(f"Assistant: {content}")
    return "\n".join(lines)


def _contextualize_query(query: str, messages: list) -> str:
    """Rewrite a follow-up query as a standalone question using prior conversation.

    Returns the original query if there's no prior conversation or the rewrite fails.
    """
    if len(messages) <= 1:
        return query

    history = _format_history(messages)
    if not history:
        return query

    llm = None
    try:
        llm = LLMFactory.get_router_llm()
        prompt = QUERY_CONTEXTUALIZER_USER_TEMPLATE.format(history=history, query=query)
        result = llm.invoke([
            SystemMessage(content=QUERY_CONTEXTUALIZER_PROMPT),
            HumanMessage(content=prompt),
        ])
        rewritten = result.content.strip().strip('"').strip("'")
        if rewritten and rewritten.lower() != query.lower():
            logger.info("Contextualized a follow-up question")
            return rewritten
        return query
    except Exception as e:
        raise_if_rate_limited(e, "guardrails.contextualization", llm=llm)
        logger.warning(f"Query contextualization failed, using original: {e}")
        return query


def guardrails_node(state: RAGState) -> dict:
    """Run two-model prompt injection detection and query contextualization.

    The API sanitizes incoming questions before constructing graph input.
    This node does not scan or redact personal information.

    Layer 1 is the local LLM Guard classifier. Layer 2 asks the routing model
    for another opinion on borderline input or when the local scanner is unavailable.

    After safety checks pass, coreferential follow-ups (e.g., "what about Microsoft?")
    are rewritten using conversation history so downstream retrieval/generation
    can work with a standalone query.
    """
    messages = state.get("messages", [])
    if not messages:
        return {"guardrail_status": "clean", "contextualized_query": ""}

    query = messages[-1].content

    blocked = {"guardrail_status": "injection_detected", "contextualized_query": query}

    # --- Layer 1: LLM Guard (local model) ---
    decision = check_injection_llm_guard(query)
    if decision == "block":
        logger.warning("Injection blocked by Layer 1 (LLM Guard)")
        return blocked

    # --- Layer 2: Another opinion for borderline input or a missing/failed local scan. ---
    if decision == "review":
        is_injection, confidence = check_injection_llm(query)
        if is_injection and confidence >= 0.7:
            logger.warning(f"Injection blocked by Layer 2 (LLM, conf={confidence:.2f})")
            return blocked

    # --- Query contextualization (resolve coreferences in multi-turn conversations) ---
    contextualized = _contextualize_query(query, messages)

    return {
        "guardrail_status": "clean",
        "contextualized_query": contextualized,
    }
