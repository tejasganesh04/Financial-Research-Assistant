"""Draft or revise an answer from source evidence using the configured answer model."""

import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from src.config.prompts import (
    GENERATOR_REVISION_TEMPLATE,
    GENERATOR_SYSTEM_PROMPT,
    GENERATOR_USER_TEMPLATE,
)
from src.models.state import RAGState
from src.services.guardrails_service import detect_pii
from src.services.llm_factory import LLMFactory
from src.services.provider_errors import raise_if_rate_limited

logger = logging.getLogger(__name__)


def _format_prior_qa(messages: list, max_turns: int = 2) -> str:
    """Phase 3.8 conversation memory: format the most recent (human, ai) pairs
    as plain text so the generator can resolve references like "compare to that"
    or "in 2022 instead". Excludes the current turn (the latest HumanMessage
    is the one being answered now).
    """
    if not messages:
        return ""
    pairs: list[tuple[str, str]] = []
    last_human: str | None = None
    for msg in messages[:-1]:  # skip the current-turn HumanMessage
        cls_name = type(msg).__name__
        content = detect_pii(str(getattr(msg, "content", "") or ""))[0].strip()
        if cls_name == "HumanMessage":
            last_human = content
        elif cls_name == "AIMessage" and last_human is not None:
            pairs.append((last_human, content[:600]))
            last_human = None
    if not pairs:
        return ""
    recent = pairs[-max_turns:]
    lines = []
    for q, a in recent:
        lines.append(f"User asked earlier: {q}")
        lines.append(f"You answered: {a}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_context(chunks: list[dict]) -> str:
    """Format relevant chunks into a context string with source attribution."""
    parts = []
    for i, chunk in enumerate(chunks, 1):
        meta = chunk.get("metadata", {})
        source = meta.get("source_file", "Unknown")
        page = meta.get("page_number", "?")
        section = meta.get("section_header", "")
        header = f"[Source {i}: {source}, Page {page}]"
        if section:
            header += f" Section: {section}"
        # Use raw_content if available (contextual prefix stripped), else content
        chunk_text = chunk.get("raw_content") or chunk.get("content", "")
        parts.append(f"{header}\n{chunk_text}")
    return "\n\n---\n\n".join(parts)


def _build_system_message(llm) -> SystemMessage:
    """Return a SystemMessage with Anthropic cache_control set when supported.

    The stable instructions include revision rules; draft/feedback stay in the
    human message. Anthropic receives an ephemeral cache marker, subject to its
    caching requirements. Other providers receive plain string system content.
    """
    if isinstance(llm, ChatAnthropic):
        return SystemMessage(content=[
            {
                "type": "text",
                "text": GENERATOR_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ])
    return SystemMessage(content=GENERATOR_SYSTEM_PROMPT)


def _log_cache_stats(response) -> None:
    """Emit a compact cache-hit/miss log line when Anthropic reports usage."""
    meta = getattr(response, "response_metadata", {}) or {}
    usage = meta.get("usage", {})
    if not usage:
        return
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    input_tokens = usage.get("input_tokens", 0) or 0
    if cache_read or cache_create:
        total_input = cache_read + cache_create + input_tokens
        hit_pct = (cache_read / total_input * 100) if total_input else 0
        logger.info(
            f"Generator cache: read={cache_read}, created={cache_create}, "
            f"uncached={input_tokens} (hit {hit_pct:.0f}%)"
        )


async def generator_node(state: RAGState) -> dict:
    """Draft from evidence, or revise a failed draft using the checker's feedback."""
    query = state.get("contextualized_query", "")
    chunks = state.get("relevant_chunks", [])
    agent_synthesis = state.get("agent_synthesis")

    # Empty-evidence responses are handled by no_info_node before generation.

    raw_context = _format_context(chunks)
    if agent_synthesis:
        context = (
            f"## Research-agent synthesis (structured findings):\n\n"
            f"{agent_synthesis}\n\n"
            f"---\n\n"
            f"## Raw retrieved chunks (for verification):\n\n"
            f"{raw_context}"
        )
    else:
        context = raw_context

    prior_qa = _format_prior_qa(state.get("messages") or [], max_turns=2)
    if prior_qa:
        context = (
            f"## Recent conversation (for context):\n\n{prior_qa}\n\n"
            f"---\n\n{context}"
        )
    user_prompt = GENERATOR_USER_TEMPLATE.format(context=context, query=query)

    # First attempts keep the normal prompt. Only a failed-check retry receives the prior draft and feedback.
    feedback = state.get("hallucination_feedback", "")
    previous_answer = state.get("generated_answer", "")
    is_revision = bool(
        state.get("hallucination_status") == "hallucinated"
        and state.get("generation_retry_count", 0) > 0
        and feedback
        and previous_answer
    )
    if is_revision:
        user_prompt += GENERATOR_REVISION_TEMPLATE.format(previous_answer=previous_answer, feedback=feedback)

    llm = None
    try:
        llm = LLMFactory.get_generator_llm()
        messages = [
            _build_system_message(llm),
            HumanMessage(content=user_prompt),
        ]
        result = await llm.ainvoke(messages)
        if not result.content:
            raise RuntimeError("The answer provider returned no response.")
        _log_cache_stats(result)
        logger.info(f"Generated answer: {len(result.content)} chars")
        from src.config.settings import settings as _settings
        from src.services.event_log import emit
        emit("generator", answer_chars=len(result.content),
             n_context_chunks=len(chunks) if chunks else 0,
             revision=is_revision,
             model=getattr(llm, "model_name", None) or getattr(llm, "model", _settings.GENERATOR_MODEL))
        return {
            "generated_answer": result.content,
        }
    except Exception as e:
        raise_if_rate_limited(e, "generator", llm=llm)
        logger.error(f"Generation failed: {e}")
        raise RuntimeError("The answer provider is unavailable. Please try again later.") from e
