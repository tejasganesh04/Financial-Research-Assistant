from typing import Annotated, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# GRAPH DATA: TypedDict documents the dictionary shape; it does not perform Pydantic-style runtime validation.
class RAGState(TypedDict):
    """Shared state flowing through every node in the RAG agent graph."""

    # --- Input ---
    # REDUCER: Merge incoming messages by ID. New IDs append; an existing ID updates that message.
    messages: Annotated[list[BaseMessage], add_messages]
    # Other fields have no reducer: provided updates replace old values; omitted fields keep their checkpointed values.
    original_query: str
    request_id: str | None
    access_scope: dict | None

    # --- Auth ---
    user_id: str
    user_role: str  # "finance", "hr", "c_level", "analyst", "admin"
    allowed_doc_types: list[str]  # ["10k", "invoice", "expense_policy", ...]

    # --- Guardrails ---
    guardrail_status: str  # "clean" or "injection_detected"
    contextualized_query: str  # Standalone rewrite, or the unchanged question when no rewrite is needed.

    # --- Routing ---
    query_intent: str  # "retrieval", "clarification", "out_of_scope"
    # Complexity classification: "simple_lookup" (single-fact retrieval,
    # fast path) vs "research_required" (multi-section synthesis, calc with formula,
    # comparative — routed to the research agent subgraph). None means classifier
    # didn't set it (treat as simple_lookup downstream).
    query_complexity: str | None

    # --- Metadata extraction ---
    # Validated company/year restrictions. OR within a list, AND between dimensions.
    # Empty lists mean no restriction on that dimension. Missing explicit filings exit without search.
    target_companies: list[str]
    target_fiscal_years: list[int]
    metadata_filter_reason: str
    metadata_unavailable: bool

    # --- Retrieval ---
    # These chunks are temporary work for one question; the API clears them before starting the next question.
    retrieved_chunks: list[dict]  # Hybrid candidates (top ~50 from dense+BM25 RRF)
    retrieval_query: str

    # --- Reranking ---
    reranked_chunks: list[dict]  # Cross-encoder top-K selected from retrieved_chunks
    candidate_diagnostics: list[dict]  # Optional per-candidate validation diagnostics

    # --- Grading ---
    relevant_chunks: list[dict]
    grading_results: list[dict]

    # --- Research agent ---
    # Set when the research agent runs (query_complexity == "research_required").
    # `agent_synthesis` is a structured-text reasoning block the agent produces
    # to guide the main generator; raw `relevant_chunks` are still populated and
    # remain the substrate the hallucination checker grounds against.
    agent_synthesis: str | None
    agent_turns_used: int | None
    agent_sub_questions: list[str] | None

    # --- Generation ---
    # A draft is kept here while checking/review is pending; it is not yet a released assistant message.
    generated_answer: str

    # --- Hallucination Check ---
    hallucination_status: str  # "grounded", "hallucinated", "unverified"
    hallucination_score: float
    # Failed check's explanation for the next generator attempt; cleared on success/error and each new question.
    hallucination_feedback: str

    # --- HITL ---
    requires_human_approval: bool
    human_decision: Optional[str]  # "approved", "rejected", None
    # Reviewer identity, timestamp and reason are returned through the conversation result/history endpoints.
    human_decision_at: Optional[str]      # ISO-8601 timestamp
    human_decision_by: Optional[str]      # approver user_id
    human_decision_by_role: Optional[str] # approver role
    human_decision_reason: Optional[str]  # rejection reason (mandatory on reject)
    hitl_submitted_at: Optional[str]      # ISO-8601 when the gate fired

    # --- Control Flow ---
    retrieval_retry_count: int
    generation_retry_count: int

    # --- Output ---
    # The formatter or a terminal node supplies the response that the requester is allowed to receive.
    final_response: str
    response_metadata: dict
