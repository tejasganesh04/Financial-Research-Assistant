import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from threading import Event

from langchain_core.messages import HumanMessage, SystemMessage

from src.config.prompts import GRADER_PROMPT, GRADER_USER_TEMPLATE
from src.config.settings import settings
from src.models.schemas import GradeResult
from src.models.state import RAGState
from src.services.candidate_validator import validate_candidates
from src.services.llm_factory import LLMFactory
from src.services.llm_retry import retry_llm_call
from src.services.provider_errors import is_rate_limited, raise_if_rate_limited

logger = logging.getLogger(__name__)

import os as _os

_GRADER_PARALLELISM = int(_os.environ.get("GRADER_PARALLELISM", "1" if settings.FORCE_GROQ_ONLY else "8"))


def _entity_match(chunk: dict, target_companies: list[str]) -> bool:
    """Do not send another company's chunk for model grading."""
    if not target_companies:
        return True
    chunk_company = (chunk.get("metadata") or {}).get("company")
    return chunk_company in target_companies


def grader_node(state: RAGState) -> dict:
    """Grade each reranked chunk for relevance. Filter to relevant-only.

    Two-stage filtering:
      1. Deterministic entity check — reject chunks whose `company` metadata
         doesn't match `target_companies`. Zero LLM cost.
      2. LLM topic-relevance grading on the survivors.

    Reads from `reranked_chunks` (cross-encoder top-K from the hybrid candidate
    pool) so we only spend LLM calls on chunks that already passed the cheaper
    reranker filter. Falls back to `retrieved_chunks` if the reranker produced
    nothing (defensive).
    """
    query = state.get("contextualized_query", "")
    chunks = state.get("reranked_chunks") or state.get("retrieved_chunks", [])
    target_companies = state.get("target_companies") or []
    target_fiscal_years = state.get("target_fiscal_years") or []

    if not chunks:
        return {
            "relevant_chunks": [],
            "grading_results": [],
        }

    candidate_diagnostics: list[dict] = []
    # Selected filters are always enforced, even if optional diagnostics are disabled.
    if settings.ENABLE_DETERMINISTIC_VALIDATOR or target_companies or target_fiscal_years:
        chunks, validator_diag = validate_candidates(
            candidates=chunks,
            target_companies=target_companies,
            target_fiscal_years=target_fiscal_years,
        )
        candidate_diagnostics.extend(validator_diag)

    if not chunks:
        return {"relevant_chunks": [], "grading_results": [], "candidate_diagnostics": candidate_diagnostics}

    llm = LLMFactory.get_grader_llm()
    structured_llm = llm.with_structured_output(GradeResult)

    from src.services.result_cache import _KEY_PREFIX, _client, _hash_key

    grader_cache_prefix = _KEY_PREFIX["grader"]
    grader_model = getattr(llm, "model_name", None) or getattr(llm, "model", settings.GRADER_MODEL)

    # Pre-pass (sequential, no LLM): apply stages 1, 2, and the cache lookup
    # to partition chunks into auto-decided (entity-reject / cache-hit) vs needs-LLM-grading. Index each chunk's outcome
    # into a dict so we can rejoin in original order after the parallel LLM
    # stage completes.
    pending_indices: list[int] = []
    decisions: dict[int, dict] = {}  # chunk_id -> {relevant, reason}
    rejected_by_entity = 0
    cache_hits = 0

    for i, chunk in enumerate(chunks):
        # Stage 1: cheap metadata-based entity check
        if not _entity_match(chunk, target_companies):
            rejected_by_entity += 1
            chunk_co = (chunk.get("metadata") or {}).get("company", "?")
            decisions[i] = {
                "relevant": False,
                "reason": f"Entity mismatch: chunk is from '{chunk_co}', query targets {target_companies}",
            }
            continue

        # Stage 2.5: result-cache lookup. Skip the LLM call when we've
        # already evaluated this exact (model, query, chunk) tuple.
        chunk_text = chunk.get("content", "")
        cache_key = grader_cache_prefix + _hash_key(grader_model, query, chunk_text)
        cached = _client.get(cache_key)
        if isinstance(cached, dict) and "relevant" in cached:
            decisions[i] = {
                "relevant": bool(cached["relevant"]),
                "reason": cached.get("reason", "cache hit"),
            }
            cache_hits += 1
            continue

        # Stage 3 candidate — needs an LLM call
        pending_indices.append(i)

    # Stage 3 (parallel): LLM topic-relevance grading on the survivors.
    # Each call wrapped in retry_llm_call so transient OpenAI hiccups
    # (connection resets, rate-limit bursts, momentary 5xx) don't silently
    # turn into false-irrelevant verdicts — which previously corrupted ~1
    # FinanceBench question per run as a refusal in the cache.
    stop_grading = Event()

    def _grade_one(idx: int) -> tuple[int, dict]:
        if stop_grading.is_set():
            # The sibling's rate-limit exception aborts this entire node; these skipped results are never released.
            return idx, {"relevant": False, "reason": "Grading cancelled after a provider rate limit"}
        chunk = chunks[idx]
        chunk_text = chunk["content"]
        prompt = GRADER_USER_TEMPLATE.format(query=query, chunk=chunk_text)

        def _invoke() -> GradeResult:
            return structured_llm.invoke([
                SystemMessage(content=GRADER_PROMPT),
                HumanMessage(content=prompt),
            ])

        try:
            result: GradeResult = retry_llm_call(_invoke, label=f"grader chunk {idx}")
            decision = {"relevant": result.relevant, "reason": result.reason}
            cache_key = grader_cache_prefix + _hash_key(grader_model, query, chunk_text)
            _client.set(cache_key, decision)
            return idx, decision
        except Exception as e:
            if is_rate_limited(e):
                stop_grading.set()
            raise_if_rate_limited(e, "grader", llm=llm)
            logger.warning(f"Grading failed for chunk {idx} after retries, marking as irrelevant: {e}")
            # Don't cache errors — a transient failure should not poison
            # the verdict for future retries.
            return idx, {"relevant": False, "reason": f"Grading error: {type(e).__name__}: {str(e)[:200]}"}

    if pending_indices:
        # Cap parallelism at the smaller of (chunks needing LLM grading, configured limit)
        # so we never spawn more workers than necessary.
        max_workers = min(_GRADER_PARALLELISM, len(pending_indices))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            # Each worker needs its own copy of request/callback context.
            futures = [pool.submit(copy_context().run, _grade_one, idx) for idx in pending_indices]
            try:
                for future in as_completed(futures):
                    idx, decision = future.result()
                    decisions[idx] = decision
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    # Reassemble in original chunk order (preserves reranker ordering downstream).
    grading_results = []
    relevant_chunks = []
    for i, chunk in enumerate(chunks):
        d = decisions.get(i, {"relevant": False, "reason": "missing decision (defensive)"})
        grading_results.append({"chunk_id": i, "relevant": d["relevant"], "reason": d["reason"]})
        if d["relevant"]:
            relevant_chunks.append(chunk)

    entity_msg = f", {rejected_by_entity} rejected by entity mismatch" if rejected_by_entity else ""
    cache_msg = f", {cache_hits} cache hits" if cache_hits else ""
    logger.info(
        f"Grading: {len(relevant_chunks)}/{len(chunks)} chunks relevant "
        f"(min={settings.GRADING_MIN_RELEVANT_CHUNKS}{entity_msg}{cache_msg}, "
        f"{len(pending_indices)} sent to LLM in parallel)"
    )
    from src.services.event_log import emit
    emit("grader", n_input=len(chunks), n_relevant=len(relevant_chunks),
         n_rejected_entity=rejected_by_entity, n_cache_hit=cache_hits,
         n_sent_to_llm=len(pending_indices),
         grader_model=grader_model, parallelism=_GRADER_PARALLELISM)

    return {
        "relevant_chunks": relevant_chunks,
        "grading_results": grading_results,
        "candidate_diagnostics": candidate_diagnostics,
    }
