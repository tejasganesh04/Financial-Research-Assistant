"""One LangGraph node for questions the router labels `research_required`.

The helpers below are ordinary Python functions, not separately checkpointed
graph nodes. The flow inside research_agent_node() is:

  1. Plan: ask the small model for qualifiers, required information and up to
     five search sub-questions. No documents have been searched at this step.
  2. Gather: search, rerank and grade each sub-question sequentially using a
     copied state. Keep the parent's access and company/year restrictions.
  3. Check: ask whether the evidence is useful enough, or whether a specific
     follow-up search would help. Run at most two check/follow-up rounds.
     Partial evidence is allowed; "sufficient" does not mean nothing is missing.
  4. Synthesize: ask the large model to organize the evidence into findings.
     A separate generator node writes the final answer afterward, and the
     hallucination checker compares that answer with the original chunks.

Current Groq-only profile: GPT-OSS 20B plans, grades and checks sufficiency;
GPT-OSS 120B synthesizes. The retained paid profile uses GPT-4o-mini for those
small-model tasks and Claude Sonnet 4.6 for synthesis when Groq fast path is off.
LLMFactory and environment settings select the actual models.

Only this node's returned dictionary updates graph state:
  - relevant_chunks: unique accepted chunks from all searches
  - grading_results: agent_curated diagnostic markers, not routing instructions
  - agent_synthesis: findings for the separate answer generator
  - agent_turns_used: planning/check/synthesis count, excluding grader calls
  - agent_sub_questions: initial planned sub-questions, excluding follow-ups
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from pydantic import BaseModel, Field

from src.config.prompts import (
    AGENT_SYNTHESIZER_SYSTEM_PROMPT,
    AGENT_SYNTHESIZER_USER_TEMPLATE,
    DECOMPOSE_SYSTEM_PROMPT,
    DECOMPOSE_USER_TEMPLATE,
    SUFFICIENCY_SYSTEM_PROMPT,
    SUFFICIENCY_USER_TEMPLATE,
)
from src.graph.nodes.grader import grader_node
from src.graph.nodes.reranker import reranker_node
from src.graph.nodes.retrieval import retrieval_node
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory
from src.services.provider_errors import raise_if_rate_limited
from src.services.request_trace import observe_step

logger = logging.getLogger(__name__)


MAX_LLM_TURNS = 5  # Legacy constant; currently unused, so it does not enforce a total-call cap.
MAX_SUB_QUESTIONS = 5  # Keep at most five initial sub-questions returned by the planner.
MAX_FOLLOWUP_ROUNDS = 2  # At most two sufficiency checks, each able to request one more search.


# ────────────────────────────────────────────────────────────
# Pydantic schemas for structured output (agent-internal only)
# ────────────────────────────────────────────────────────────


class _Decomposition(BaseModel):
    """The planner's structured result: conditions, needed inputs and search questions."""

    qualifiers: list[str] = Field(
        description="Question qualifiers that change WHAT counts as a correct answer "
                    "(e.g. 'exclude M&A', 'organic', 'year over year direction'). "
                    "Use ['no qualifiers'] if none."
    )
    required_quantities: list[str] = Field(
        description="The 2+ inputs the question's answer requires (current assets, "
                    "current liabilities, operating income FY-1, etc)."
    )
    sub_questions: list[str] = Field(
        description="2–4 atomic sub-questions, each retrievable from a single section."
    )


class _SufficiencyVerdict(BaseModel):
    """The evidence check's decision, missing information and suggested follow-up."""

    decision: str = Field(description="'sufficient' or 'need_more'")
    missing_quantity: str | None = Field(default=None)
    follow_up_question: str | None = Field(default=None)
    reason: str = Field(description="One sentence explaining the verdict.")


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────


def _system_block(prompt_text: str, llm) -> SystemMessage:
    """Create a system message; add a cache marker only for the Anthropic client."""
    if isinstance(llm, ChatAnthropic):
        return SystemMessage(content=[{
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        }])
    return SystemMessage(content=prompt_text)


def _chunk_id(chunk: dict) -> tuple[str, Any]:
    """Identify duplicate chunks by source/index, falling back to chunk or object ID."""
    meta = chunk.get("metadata", {})
    return (
        meta.get("source_file", "?"),
        meta.get("chunk_index", chunk.get("id", id(chunk))),
    )


def _evidence_summary(chunks: list[dict], max_chars_per: int = 400) -> str:
    """Give the evidence checker source/page labels and the first 400 characters per chunk.

    This shortens the prompt; it is a text excerpt, not a model-generated
    summary. Useful information later in a chunk may be omitted.
    """
    if not chunks:
        return "(no evidence collected yet)"
    parts = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("metadata", {})
        src = meta.get("source_file", "?")
        page = meta.get("page_number", "?")
        text = (c.get("raw_content") or c.get("content", ""))[:max_chars_per]
        parts.append(f"[{i}] {src} p{page}: {text}")
    return "\n".join(parts)


def _full_evidence(chunks: list[dict]) -> str:
    """Full chunk text for the synthesizer (no truncation)."""
    if not chunks:
        return "(no evidence collected)"
    parts = []
    for i, c in enumerate(chunks, 1):
        meta = c.get("metadata", {})
        src = meta.get("source_file", "?")
        page = meta.get("page_number", "?")
        text = c.get("raw_content") or c.get("content", "")
        parts.append(f"[Source {i}: {src}, Page {page}]\n{text}")
    return "\n\n---\n\n".join(parts)


def _retrieve_and_grade_for_subq(state: RAGState, sub_question: str) -> list[dict]:
    """Call the existing search/rerank/grade functions with a local copy of state.

    The sub-question becomes retrieval_query for search and reranking. The
    grader still judges against the parent's contextualized_query. Access and
    company/year restrictions are preserved; empty results do not widen them.
    These direct function calls do not traverse the graph's query-rewrite loop.
    """
    # Make a shallow sub-state with the sub-question as the retrieval query.
    sub_state = dict(state)
    sub_state["retrieval_query"] = sub_question

    # 1) retrieval
    retrieval_out = observe_step("retrieval", retrieval_node, sub_state)
    sub_state["retrieved_chunks"] = retrieval_out.get("retrieved_chunks", [])
    if not sub_state["retrieved_chunks"]:
        return []

    # 2) reranker
    rerank_out = observe_step("reranker", reranker_node, sub_state)
    sub_state["reranked_chunks"] = rerank_out.get("reranked_chunks", [])
    sub_state["candidate_diagnostics"] = rerank_out.get("candidate_diagnostics", [])
    if not sub_state["reranked_chunks"]:
        return []

    # 3) Grade against the original working question: GPT-OSS 20B in Groq-only mode.
    # The retained paid profile uses GPT-4o-mini when Groq fast path is off.
    # Grader concurrency is configured inside grader.py, not by this helper.
    grade_out = observe_step("grader", grader_node, sub_state)
    return grade_out.get("relevant_chunks", []) or []


# ────────────────────────────────────────────────────────────
# LLM-driven steps
# ────────────────────────────────────────────────────────────


def _decompose(query: str, target_companies: list[str], target_fiscal_years: list[int]) -> _Decomposition:
    """Ask the small model for a plan; return it to the calling Python function.

    Current Groq-only mode uses openai/gpt-oss-20b; the retained paid default
    uses gpt-4o-mini. Both request at most 2,048 output tokens for this task.
    The input is the question, selected companies/years and planning prompt,
    with no retrieved document chunks. LLMFactory selects the model.
    """
    llm = LLMFactory.get_research_decompose_llm()
    # Request the three fields in _Decomposition and parse the response into that object.
    # GPT-OSS supports native JSON schemas; tool calling can emit an undeclared 'json' tool.
    options = {}
    if isinstance(llm, ChatGroq) and llm.model_name.startswith("openai/gpt-oss-"):
        options["method"] = "json_schema"
    structured = llm.with_structured_output(_Decomposition, **options)
    prompt = DECOMPOSE_USER_TEMPLATE.format(
        query=query,
        target_companies=target_companies or "(not specified)",
        target_fiscal_years=target_fiscal_years or "(not specified)",
    )
    try:
        result: _Decomposition = structured.invoke([
            _system_block(DECOMPOSE_SYSTEM_PROMPT, llm),
            HumanMessage(content=prompt),
        ])
        # Keep only the first five search questions; this does not enforce a minimum.
        result.sub_questions = result.sub_questions[:MAX_SUB_QUESTIONS]
        return result
    except Exception as exc:  # noqa: BLE001
        raise_if_rate_limited(exc, "research.decompose", llm=llm)
        logger.warning(f"Decompose failed, falling back to single-question agent: {exc}")
        return _Decomposition(
            qualifiers=["no qualifiers"],
            required_quantities=[query],
            sub_questions=[query],
        )


def _judge_sufficiency(
    query: str,
    decomposition: _Decomposition,
    chunks: list[dict],
) -> _SufficiencyVerdict:
    """Ask whether to proceed with the evidence or make a targeted follow-up search.

    Uses GPT-OSS 20B in current Groq-only mode, or GPT-4o-mini in the retained
    paid default. The prompt allows partial evidence and prefers proceeding
    unless a specific new search would help. A non-rate-limit invocation error
    also returns "sufficient"; the final answer still goes through grounding.
    """
    llm = LLMFactory.get_research_sufficiency_llm()
    options = {}
    if isinstance(llm, ChatGroq) and llm.model_name.startswith("openai/gpt-oss-"):
        options["method"] = "json_schema"
    structured = llm.with_structured_output(_SufficiencyVerdict, **options)
    decomp_text = (
        f"Qualifiers: {decomposition.qualifiers}\n"
        f"Required quantities: {decomposition.required_quantities}\n"
        f"Sub-questions: {decomposition.sub_questions}"
    )
    prompt = SUFFICIENCY_USER_TEMPLATE.format(
        query=query,
        decomposition=decomp_text,
        n_chunks=len(chunks),
        evidence_summary=_evidence_summary(chunks),
    )
    try:
        verdict: _SufficiencyVerdict = structured.invoke([
            _system_block(SUFFICIENCY_SYSTEM_PROMPT, llm),
            HumanMessage(content=prompt),
        ])
        return verdict
    except Exception as exc:  # noqa: BLE001
        raise_if_rate_limited(exc, "research.sufficiency", llm=llm)
        logger.warning(f"Sufficiency judge failed, defaulting to sufficient: {exc}")
        return _SufficiencyVerdict(
            decision="sufficient",
            reason=f"judge_error: {type(exc).__name__}",
        )


def _synthesize(
    query: str,
    decomposition: _Decomposition,
    chunks: list[dict],
) -> str:
    """Organize full evidence and the plan into findings, not the final user answer.

    Uses GPT-OSS 120B in current Groq-only mode, or Claude Sonnet 4.6 in the
    retained paid default. On a non-rate-limit invocation error, return a
    short notice; the separate generator still receives the raw chunks.
    """
    llm = LLMFactory.get_research_synthesize_llm()
    decomp_text = (
        f"Qualifiers: {decomposition.qualifiers}\n"
        f"Required quantities: {decomposition.required_quantities}\n"
        f"Sub-questions executed: {decomposition.sub_questions}"
    )
    prompt = AGENT_SYNTHESIZER_USER_TEMPLATE.format(
        query=query,
        decomposition=decomp_text,
        n_chunks=len(chunks),
        evidence=_full_evidence(chunks),
    )
    try:
        result = llm.invoke([
            _system_block(AGENT_SYNTHESIZER_SYSTEM_PROMPT, llm),
            HumanMessage(content=prompt),
        ])
        synthesis = result.content if isinstance(result.content, str) else str(result.content)
    except Exception as exc:  # noqa: BLE001
        raise_if_rate_limited(exc, "research.synthesize", llm=llm)
        logger.warning(f"Synthesizer failed, falling back to chunk concatenation: {exc}")
        synthesis = "## Research findings\n\n(synthesizer error — main generator will use raw chunks)"
    return synthesis


# ────────────────────────────────────────────────────────────
# Top-level node
# ────────────────────────────────────────────────────────────


def research_agent_node(state: RAGState) -> dict:
    """Plan, collect evidence, check for gaps, then return findings to LangGraph.

    Search up to five initial questions, then allow up to two follow-up rounds.
    agent_turns_used counts only the planner, sufficiency checks and synthesizer
    (at most four calls on this normal path), not embeddings or per-chunk grades.
    MAX_LLM_TURNS is unused; there is no five-call cap on all provider work.
    """
    query = state.get("contextualized_query", "")
    if not query:
        return {"relevant_chunks": [], "agent_synthesis": None}

    target_companies = state.get("target_companies") or []
    target_fiscal_years = state.get("target_fiscal_years") or []

    # 1) observe_step times _decompose(query, companies, years) and returns its plan.
    # decomp is a local _Decomposition object; no graph state update happens here.
    decomp = observe_step("research.decompose", _decompose, query, target_companies, target_fiscal_years)
    sub_questions = decomp.sub_questions
    logger.info(
        f"Agent decomposed into {len(sub_questions)} sub-questions; "
        f"qualifiers={decomp.qualifiers}; required={decomp.required_quantities}"
    )
    from src.services.event_log import emit
    emit("research_agent.decompose", n_sub_questions=len(sub_questions),
         qualifiers=list(decomp.qualifiers), required_quantities=list(decomp.required_quantities),
         sub_questions=list(sub_questions))

    # 2) Search/rerank/grade each planned question, keeping each accepted chunk once.
    chunks_by_id: dict[tuple, dict] = {}
    for subq in sub_questions:
        for c in _retrieve_and_grade_for_subq(state, subq):
            cid = _chunk_id(c)
            if cid not in chunks_by_id:
                chunks_by_id[cid] = c

    logger.info(f"Agent collected {len(chunks_by_id)} unique chunks across {len(sub_questions)} sub-questions")

    # 3) At most two check/follow-up rounds. After the final follow-up, there is no extra check.
    sufficiency_history: list[dict] = []
    for round_idx in range(MAX_FOLLOWUP_ROUNDS):
        chunks = list(chunks_by_id.values())
        verdict = observe_step("research.sufficiency", _judge_sufficiency, query, decomp, chunks)
        sufficiency_history.append({
            "decision": verdict.decision,
            "missing": verdict.missing_quantity,
            "follow_up": verdict.follow_up_question,
            "reason": verdict.reason,
        })
        if verdict.decision == "sufficient":
            logger.info(f"Agent: sufficient after round {round_idx} ({verdict.reason})")
            emit("research_agent.sufficiency", decision="sufficient", round=round_idx,
                 reason=verdict.reason, n_chunks_so_far=len(chunks_by_id))
            break
        if not verdict.follow_up_question:
            logger.info(f"Agent: 'need_more' but no follow-up — exiting loop")
            emit("research_agent.sufficiency", decision="need_more_no_followup",
                 round=round_idx, reason=verdict.reason, n_chunks_so_far=len(chunks_by_id))
            break
        logger.info(f"Agent follow-up [{round_idx}]: {verdict.follow_up_question} ({verdict.reason})")
        emit("research_agent.sufficiency", decision="need_more", round=round_idx,
             follow_up=verdict.follow_up_question, missing=verdict.missing_quantity,
             reason=verdict.reason)
        for c in _retrieve_and_grade_for_subq(state, verdict.follow_up_question):
            cid = _chunk_id(c)
            if cid not in chunks_by_id:
                chunks_by_id[cid] = c

    # 4) If nothing was accepted, return no evidence; builder.py routes to no_info_response.
    # Otherwise prepare findings for the separate generator node.
    relevant_chunks = list(chunks_by_id.values())
    if not relevant_chunks:
        return {"relevant_chunks": [], "agent_synthesis": None, "grading_results": [],
                "agent_turns_used": 1 + len(sufficiency_history), "agent_sub_questions": sub_questions}
    synthesis = observe_step("research.synthesize", _synthesize, query, decomp, relevant_chunks)

    # Diagnostic count: planner + sufficiency checks + synthesizer.
    # Embedding/grader calls and any SDK retries are excluded from this count.
    turns_used = 1 + len(sufficiency_history) + 1

    logger.info(
        f"Agent done: {len(relevant_chunks)} chunks, {turns_used} planning/check/synthesis calls, "
        f"sub_questions={sub_questions}"
    )
    emit("research_agent.synthesize", n_chunks=len(relevant_chunks), turns_used=turns_used,
         synthesis_chars=len(synthesis) if synthesis else 0)

    # Diagnostic markers for already curated chunks. The edge in builder.py sends
    # nonempty research evidence directly to generator; these markers do not choose the route.
    grading_results = [{"agent_curated": True} for _ in relevant_chunks]

    # 5) This returned dictionary is the update LangGraph merges into its shared state.
    return {
        "relevant_chunks": relevant_chunks,
        "grading_results": grading_results,
        "agent_synthesis": synthesis,
        "agent_turns_used": turns_used,
        "agent_sub_questions": sub_questions,
    }
