"""Conversation endpoints and the question request flow.

Walkthrough order: list_threads -> get_thread -> conversation_message_stream -> chat_stream.
Within chat_stream, read preparation 1-7, then the EventSourceResponse return,
then event_generator (8-10), which runs unlocked_event_generator (11-15).
The graph handles its own node order; these wrappers send progress and the complete result.
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from src.api.dependencies import get_current_user
from src.config.rbac_config import approvers_for, get_permissions
from src.graph.builder import build_graph
from src.models.auth import User
from src.models.schemas import ChatRequest, ChatResponse
from src.services.conversation_access import check_chat_access, conversation_write
from src.services.cost_tracker import RequestScopedCostHandler
from src.services.escalation import parse_escalation
from src.services.guardrails_service import detect_pii
from src.services.provider_errors import rate_limit_payload
from src.services.request_trace import RequestTrace
from src.services.thread_service import (
    delete_thread,
    get_thread_owner,
    list_all_threads_paged,
    list_threads_for_user,
    ts_from_checkpoint_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

# Cache compiled graphs keyed by checkpointer identity to avoid recompilation
_graphs: dict = {}


# Reuse the compiled workflow for this checkpointer. Each execution selects its own conversation using thread_id.
def _get_graph(checkpointer=None):
    key = id(checkpointer)
    if key not in _graphs:
        _graphs[key] = build_graph(checkpointer=checkpointer)
    return _graphs[key]


# NEW TURN INPUT: Supply only the new HumanMessage; add_messages keeps older history.
# Reset temporary evidence, answers, retries, approval details and escalation so they do not leak into the next turn.
def _build_initial_state(message: str, user: User, request_id: str | None = None, access_scope: dict | None = None) -> dict:
    """Build the initial RAGState for a chat request."""
    return {
        "messages": [HumanMessage(content=message, additional_kwargs={"request_id": request_id, "access_scope": access_scope})],
        "request_id": request_id,
        "access_scope": access_scope,
        "original_query": message,
        "query_complexity": None,
        "candidate_diagnostics": [],
        "agent_synthesis": None,
        "agent_turns_used": 0,
        "agent_sub_questions": [],
        "human_decision_at": None,
        "human_decision_by": None,
        "human_decision_by_role": None,
        "human_decision_reason": None,
        "hitl_submitted_at": None,
        "user_id": user.user_id,
        "user_role": user.role,
        "allowed_doc_types": [],
        "guardrail_status": "clean",
        "contextualized_query": "",
        "query_intent": "",
        "target_companies": [],
        "target_fiscal_years": [],
        "metadata_filter_reason": "",
        "metadata_unavailable": False,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "retrieval_query": "",
        "relevant_chunks": [],
        "grading_results": [],
        "generated_answer": "",
        "hallucination_status": "",
        "hallucination_score": 0.0,
        "hallucination_feedback": "",
        "requires_human_approval": bool(access_scope and access_scope["escalated"]),
        "human_decision": None,
        "retrieval_retry_count": 0,
        "generation_retry_count": 0,
        "final_response": "",
        "response_metadata": {},
    }


# JSON ALTERNATIVE: Same preparation/permissions, but ainvoke returns a complete response without stage events.
@router.post("/messages", response_model=ChatResponse)
async def chat(request: ChatRequest, user: User = Depends(get_current_user), http_request: Request = None):
    """Process a chat message through the RAG agent pipeline and return the complete answer."""
    request_started = time.monotonic()
    thread_id = request.thread_id or str(uuid.uuid4())

    checkpointer = getattr(http_request.app.state, "checkpointer", None) if http_request else None
    graph = _get_graph(checkpointer=checkpointer)

    if checkpointer is not None:
        await check_chat_access(graph, thread_id, user.user_id)
    elif request.thread_id:
        raise HTTPException(status_code=503, detail="Conversation history is unavailable.")
    try:
        question, access_scope = parse_escalation(request.message, user.role)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    escalated = bool(access_scope and access_scope["escalated"])
    if escalated and checkpointer is None:
        raise HTTPException(status_code=503, detail="Approval storage is unavailable. Please try again later.")
    sanitized_message, _ = detect_pii(question)
    trace = RequestTrace(thread_id, sanitized_message, user.role, get_permissions(user.role), started=request_started)
    trace.record("access_scope", requested_scope=access_scope)
    initial_state = _build_initial_state(sanitized_message, user, trace.request_id, access_scope)
    req_cost = RequestScopedCostHandler(trace=trace)
    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": "rag_query",
        "tags": ["api", f"role:{user.role}"],
        "metadata": {"user_id": user.user_id, "name": user.name, "department": user.department, "role": user.role, "thread_id": thread_id, "hitl_enabled": checkpointer is not None},
        "callbacks": [req_cost, trace],
    }

    with trace.activate():
        try:
            async with conversation_write(thread_id):
                if checkpointer is not None:
                    await check_chat_access(graph, thread_id, user.user_id)
                try:
                    result = await graph.ainvoke(initial_state, config=config)
                except Exception as e:
                    limit = rate_limit_payload(e)
                    if limit:
                        trace.record("provider_limit", **{k: v for k, v in limit.items() if k != "type"})
                        headers = {"X-Request-ID": trace.request_id, "X-Error-Code": limit["code"]}
                        if limit["retry_after_seconds"] is not None:
                            headers["Retry-After"] = str(limit["retry_after_seconds"])
                        raise HTTPException(status_code=429, detail=limit["message"], headers=headers) from e
                    logger.error(f"Graph execution failed: {e}", exc_info=True)
                    raise HTTPException(status_code=503, detail="The answer could not be completed. Check service status and conversation history before retrying.")

                # Check if graph was interrupted by HITL (via graph state, not return values)
                if checkpointer is not None:
                    try:
                        graph_state = await graph.aget_state(config)
                        if graph_state.tasks:
                            for task in graph_state.tasks:
                                if hasattr(task, "interrupts") and task.interrupts:
                                    interrupt_value = task.interrupts[0].value
                                    trace.record("approval_pending", submitted_at=interrupt_value.get("submitted_at"),
                                                 access_scope=interrupt_value.get("access_scope"),
                                                 approvers=approvers_for(user.role))
                                    trace.finish("awaiting_approval")
                                    return ChatResponse(
                                        response="This answer is waiting for approval.",
                                        sources=[],
                                        confidence=None,
                                        requires_approval=True,
                                        thread_id=thread_id, request_id=trace.request_id,
                                        sanitized_message=sanitized_message, access_scope=access_scope,
                                    )
                    except Exception as e:
                        raise HTTPException(status_code=503, detail="Unable to check approval status.") from e

                metadata = result.get("response_metadata", {})
                cost_summary = req_cost.aggregate()
                return ChatResponse(
                    response=result.get("final_response", "No response generated."),
                    sources=metadata.get("sources", []),
                    confidence=metadata.get("confidence"),
                    requires_approval=result.get("requires_human_approval", False),
                    thread_id=thread_id, request_id=trace.request_id,
                    sanitized_message=sanitized_message, access_scope=access_scope,
                    cost_usd=cost_summary["cost_usd"],
                    tokens=cost_summary["tokens"],
                )
        except BaseException as error:
            trace.finish("cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
            raise
        finally:
            trace.finish()


# APPROVAL POLL: Read the saved result/status only. Polling never starts a new answering run.
@router.get("/{thread_id}/result")
async def chat_result(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Read the saved answer or approval status for the requester/Admin without executing the graph."""
    checkpointer = getattr(http_request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="Result polling unavailable (no checkpointer)")

    from src.services.thread_service import get_thread_owner_role
    pool = getattr(http_request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Conversation ownership is unavailable.")
    owner, requester_role, _name, _dept = await get_thread_owner_role(pool, thread_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    if owner != user.user_id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Thread belongs to a different user")

    graph = _get_graph(checkpointer=checkpointer)
    cfg = {"configurable": {"thread_id": thread_id}}
    gs = await graph.aget_state(cfg)
    if gs is None or not gs.values:
        return {"status": "pending", "thread_id": thread_id}

    for task in (gs.tasks or []):
        if getattr(task, "interrupts", None):
            v = task.interrupts[0].value
            return {
                "status": "pending",
                "thread_id": thread_id,
                "reason": (v or {}).get("reason"),
                "access_scope": (v or {}).get("access_scope"),
            }

    if gs.next:
        return {"status": "pending", "thread_id": thread_id}

    values = gs.values or {}
    final_response = values.get("final_response", "")
    metadata = values.get("response_metadata") or {}
    requires_approval = values.get("requires_human_approval", False)
    human_decision = values.get("human_decision")

    decision_audit = {
        "decided_at": values.get("human_decision_at"),
        "decided_by": values.get("human_decision_by"),
        "decided_by_role": values.get("human_decision_by_role"),
        "reason": values.get("human_decision_reason"),
        "submitted_at": values.get("hitl_submitted_at"),
    }

    if requires_approval and human_decision == "rejected":
        return {
            "status": "rejected",
            "thread_id": thread_id,
            "response": final_response,
            "decision": decision_audit,
        }

    payload: dict = {
        "status": "approved" if requires_approval else "ready",
        "thread_id": thread_id,
        "response": final_response,
        "sources": metadata.get("sources", []),
        "confidence": metadata.get("confidence"),
    }
    if requires_approval:
        payload["decision"] = decision_audit
    return payload


# Get the shared startup pool from the HTTP Request object; storage-dependent endpoints return 503 when it is absent.
def _pool_or_503(http_request: Request):
    pool = getattr(http_request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkpoint store not initialized — HITL/threads disabled",
        )
    return pool


# Use the saved original/cleaned question as a compact sidebar title; the database has no separate title column.
def _make_title(state_values: dict[str, Any] | None) -> str:
    """Pick a sidebar title for a thread.

    Preference order:
      1. The graph state's `original_query` (set by the chat route)
      2. The graph state's `contextualized_query` (after guardrails)
      3. Empty string fallback
    Truncated to 80 chars so the sidebar stays compact.
    """
    if not state_values:
        return ""
    q = state_values.get("original_query") or state_values.get("contextualized_query") or ""
    q = q.strip().replace("\n", " ")
    return q[:80] + ("…" if len(q) > 80 else "")


# Read pending interrupt payloads from checkpoint tasks without starting the graph.
def _is_interrupted(graph_state) -> tuple[bool, dict | None]:
    """Return pending status and the first recorded task interrupt payload."""
    interrupted = False
    payload: dict | None = None
    for task in (graph_state.tasks or []):
        if getattr(task, "interrupts", None):
            interrupt_value = task.interrupts[0].value
            interrupted = True
            payload = interrupt_value if isinstance(interrupt_value, dict) else {"value": str(interrupt_value)}
            break
    return interrupted, payload


# Build browser history from saved messages; hide the current draft while review is pending and preserve request IDs.
def _messages_from_state(state_values: dict[str, Any] | None, interrupted: bool = False) -> list[dict[str, Any]]:
    """Reconstruct a {role, content} message list from the graph state.

    Our RAGState (see `src/models/state.py`) keeps the user message in
    `original_query` and the final answer in `final_response`. Anything
    in between (intermediate node outputs) isn't user-visible.
    """
    if not state_values:
        return []
    from langchain_core.messages import AIMessage, HumanMessage
    messages = state_values.get("messages") or []
    pending = (interrupted or state_values.get("requires_human_approval")
               and state_values.get("human_decision") not in ("approved", "rejected"))
    last_question = max((i for i, msg in enumerate(messages) if isinstance(msg, HumanMessage)), default=-1)
    result = []
    for i, msg in enumerate(messages):
        request_id = getattr(msg, "additional_kwargs", {}).get("request_id")
        trace_fields = {"request_id": request_id} if request_id else {}
        scope = getattr(msg, "additional_kwargs", {}).get("access_scope")
        if scope:
            trace_fields["access_scope"] = scope
        if isinstance(msg, HumanMessage):
            result.append({"role": "user", "content": msg.content, **trace_fields})
        elif isinstance(msg, AIMessage) and not (pending and i > last_question):
            # Older checkpoints may contain several generator drafts for a
            # single question. Only the last assistant message is final.
            if i + 1 < len(messages) and isinstance(messages[i + 1], AIMessage):
                continue
            result.append({"role": "assistant", "content": msg.content,
                           "sources": msg.additional_kwargs.get("sources", []),
                           "confidence": msg.additional_kwargs.get("confidence"), **trace_fields})
    if result:
        return result
    # Compatibility for old checkpoints that stored only the last Q&A.
    if state_values.get("original_query"):
        result.append({"role": "user", "content": state_values["original_query"]})
    if state_values.get("final_response") and not pending:
        meta = state_values.get("response_metadata") or {}
        result.append({"role": "assistant", "content": state_values["final_response"],
                       "sources": meta.get("sources", []), "confidence": meta.get("confidence")})
    return result


# SIDEBAR: GET /v1/conversations returns this JWT user's saved threads from PostgreSQL checkpoint metadata.
@router.get("")
async def list_threads(
    http_request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    all_users: bool = Query(
        False,
        alias="all",
        description="Admin only: enumerate every user's threads (default false). Ignored for non-admin roles.",
    ),
    user: User = Depends(get_current_user),
):
    """Return paginated conversation summaries: own threads by default, all owners only for Admin with all=true."""
    # 1. Use the startup database pool; an unavailable store returns HTTP 503.
    pool = _pool_or_503(http_request)

    # 2. The normal browser request lists only the verified user's threads. Explicit Admin all=true lists every owner.
    if all_users and user.role == "admin":
        rows, total = await list_all_threads_paged(pool, limit=limit, offset=offset)
        scope = "all"
    else:
        rows, total = await list_threads_for_user(pool, user.user_id, limit=limit, offset=offset)
        scope = "self"

    # 3. Read each thread's latest checkpoint for its title/pending flag. aget_state does not run answering nodes.
    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    threads = []
    for r in rows:
        cfg = {"configurable": {"thread_id": r["thread_id"]}}
        title = ""
        interrupted = False
        try:
            gs = await graph.aget_state(cfg)
            title = _make_title(gs.values if gs else None)
            interrupted, _ = _is_interrupted(gs)
        except Exception as e:
            logger.warning("aget_state failed for thread %s: %s", r["thread_id"], e)
        threads.append({
            "thread_id": r["thread_id"],
            "title": title,
            "checkpoint_count": r["checkpoint_count"],
            "is_interrupted": interrupted,
            # Owner and checkpoint-derived activity information for the sidebar.
            "owner": {
                "user_id": r["user_id"],
                "name": r["name"],
                "role": r["role"],
                "department": r["department"],
            },
            "created_at": r["created_at"],
            "last_activity_at": r["last_activity_at"],
        })

    # 4. Return sidebar summaries and pagination; React stores this response in its history state.
    return {
        "threads": threads,
        "total": total,
        "limit": limit,
        "offset": offset,
        "scope": scope,
        "viewer_role": user.role,
    }


# OPEN CONVERSATION: Check ownership/Admin viewing access, read the latest checkpoint and return messages/status.
@router.get("/{thread_id}")
async def get_thread(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Read saved messages, pending status and decision audit; only the owner or Admin may view them."""
    pool = _pool_or_503(http_request)

    from src.services.thread_service import get_thread_owner_role
    # 1. Read ownership from saved checkpoint metadata and enforce owner/Admin viewing access.
    owner_user_id, owner_role, owner_name, owner_dept = await get_thread_owner_role(pool, thread_id)
    if owner_user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    if owner_user_id != user.user_id and user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Thread belongs to a different user")

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    cfg = {"configurable": {"thread_id": thread_id}}
    # 2. Load/reconstruct the latest saved state for this thread. This is a database read, not a graph invocation.
    gs = await graph.aget_state(cfg)
    owner_block = {
        "user_id": owner_user_id,
        "name": owner_name,
        "role": owner_role,
        "department": owner_dept,
    }
    if gs is None or not gs.values:
        return {
            "thread_id": thread_id,
            "messages": [],
            "is_interrupted": False,
            "interrupt_payload": None,
            "owner": owner_block,
            "turn_count": 0,
            "audit": None,
            "last_activity_at": None,
        }

    # 3. Detect recorded interrupts, then expose only safe messages and approval metadata to the requester.
    interrupted, payload = _is_interrupted(gs)
    values = gs.values or {}

    # Turn count = number of HumanMessage entries in the state. Cheap (already
    # have the deserialized state from aget_state above).
    try:
        from langchain_core.messages import HumanMessage
        turn_count = sum(1 for m in (values.get("messages") or []) if isinstance(m, HumanMessage))
    except Exception:
        turn_count = 0

    # Include a decision audit only after this thread has recorded an approval decision.
    # and had a decision recorded. All None if the thread never triggered HITL.
    audit = None
    if values.get("hitl_submitted_at") or values.get("human_decision_at"):
        audit = {
            "hitl_submitted_at": values.get("hitl_submitted_at"),
            "decided_at": values.get("human_decision_at"),
            "decided_by": values.get("human_decision_by"),
            "decided_by_role": values.get("human_decision_by_role"),
            "decision": values.get("human_decision"),
            "reason": values.get("human_decision_reason"),
        }

    # Last activity = decoded ts of the most-recent checkpoint id. Cheaper
    # than another SQL roundtrip — gs.config carries the checkpoint_id used.
    last_activity_at = ts_from_checkpoint_id(gs.config.get("configurable", {}).get("checkpoint_id"))

    return {
        "thread_id": thread_id,
        "messages": _messages_from_state(gs.values, interrupted=interrupted),
        "is_interrupted": interrupted,
        "interrupt_payload": {k: payload.get(k) for k in ("reason", "access_scope", "request_id", "submitted_at")} if payload else None,
        "owner": owner_block,
        "turn_count": turn_count,
        "audit": audit,
        "last_activity_at": last_activity_at,
    }


# DELETE CONVERSATION: After ownership checks, remove its checkpoints, state blobs and task writes.
@router.delete("/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread_endpoint(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Delete a conversation. Ownership-gated; admin can delete any."""
    async with conversation_write(thread_id):
        pool = _pool_or_503(http_request)
        owner = await get_thread_owner(pool, thread_id)
        if owner is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
        if owner != user.user_id and user.role != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Thread belongs to a different user")

        n = await delete_thread(pool, thread_id)
        logger.info("Deleted thread %s (%d checkpoint rows) for user %s", thread_id, n, user.user_id)
        return None


# JSON PATH WRAPPER: The URL supplies the conversation ID; pass it with the validated body to the shared handler.
@router.post("/{thread_id}/messages", response_model=ChatResponse)
async def conversation_message(thread_id: str, body: ChatRequest, http_request: Request,
                               user: User = Depends(get_current_user)):
    return await chat(body.model_copy(update={"thread_id": thread_id}), user, http_request)


# Translate our graph node names into readable stage labels. These labels do not decide which node runs next.
_NODE_LABELS = {
    "rbac_gate": "Checking permissions",
    "guardrails": "Running safety checks",
    "metadata_extractor": "Selecting company and filing-year filters",
    "router": "Classifying query",
    "retrieval": "Searching documents",
    "reranker": "Reranking candidates",
    "grader": "Evaluating relevance",
    "query_rewriter": "Refining search query",
    "generator": "Generating answer",
    "hallucination_checker": "Verifying accuracy",
    "hitl_gate": "Checking approval requirements",
    "response_formatter": "Formatting response",
    # Terminal nodes — must be in this dict so their on_chain_end output is
    # captured as final_state (otherwise the SSE `final` event falls back to
    # "No response generated.").
    "no_info_response": "Compiling response",
    "blocked_response": "Compiling response",
    "out_of_scope_response": "Compiling response",
    "clarification_response": "Compiling response",
    "research_agent": "Researching the question",
}


# QUESTION FLOW: Preparation 1-7 happens first. Then EventSourceResponse starts the outer generator (8-10),
# which consumes the inner generator (11-15). Inner is defined first below, but outer runs first.
@router.post("/messages/stream")
async def chat_stream(request: ChatRequest, user: User = Depends(get_current_user), http_request: Request = None):
    """Prepare a question, send live stage updates, then return the complete answer or pending status."""
    # 1. Start the request timer; reuse React's thread ID, with a backend UUID fallback when none was supplied.
    request_started = time.monotonic()
    thread_id = request.thread_id or str(uuid.uuid4())

    # 2. Get the PostgreSQL saver prepared at startup and reuse/build the compiled graph. No node executes yet.
    checkpointer = getattr(http_request.app.state, "checkpointer", None) if http_request else None
    graph = _get_graph(checkpointer=checkpointer)

    # 3. Read the saved conversation: reject another owner or a pending approval before doing any answer work.
    if checkpointer is not None:
        await check_chat_access(graph, thread_id, user.user_id)
    elif request.thread_id:
        raise HTTPException(status_code=503, detail="Conversation history is unavailable.")
    # 4. Strip/validate a first-line /escalate command and calculate role permissions plus requested additions.
    try:
        question, access_scope = parse_escalation(request.message, user.role)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    # Only a scope extending normal permissions needs review. Such requests must have checkpoint storage.
    escalated = bool(access_scope and access_scope["escalated"])
    if escalated and checkpointer is None:
        raise HTTPException(status_code=503, detail="Approval storage is unavailable. Please try again later.")
    # 5. Replace detected personal details with labels. Keep cleaned text; discard the detection list at this call site.
    sanitized_message, _ = detect_pii(question)
    # 6. Create this question's recorder and request UUID. Record normal role permissions and the effective access scope.
    trace = RequestTrace(thread_id, sanitized_message, user.role, get_permissions(user.role), started=request_started)
    trace.record("access_scope", requested_scope=access_scope)
    # 7a. Prepare new graph input and reset temporary fields; LangGraph will merge it with the thread's checkpoint later.
    initial_state = _build_initial_state(sanitized_message, user, trace.request_id, access_scope)
    # 7b. Attach a request-local token/cost recorder. Creating observers does not call a model.
    req_cost = RequestScopedCostHandler(trace=trace)
    # 7c. thread_id selects saved state; metadata carries verified identity; callbacks observe steps/model usage.
    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": "rag_query_stream",
        "tags": ["api", "streaming", f"role:{user.role}"],
        "metadata": {"user_id": user.user_id, "name": user.name, "department": user.department, "role": user.role, "thread_id": thread_id, "hitl_enabled": checkpointer is not None},
        "callbacks": [req_cost, trace],
    }

    # The browser receives stage labels and one terminal event; partial model-answer tokens are never forwarded.

    # INNER GENERATOR (runs at step 11, after the outer generator below).
    # Run LangGraph, measure stages and produce either the complete result or a pending/error event.
    # It does not reserve the conversation itself; the outer generator already holds that reservation.
    async def unlocked_event_generator():
        # 11. Keep the most recent node-output dictionary, not the full saved state; the terminal node supplies response fields.
        final_state = None

        # Measure stage durations while observing execution; timing does not run extra graph work.
        import time as _time
        # Start graph timing. Track stage start times and each node's latest duration; the trace keeps individual attempts.
        _wall_start = _time.monotonic()
        _node_starts: dict[str, float] = {}
        _stage_timings_ms: dict[str, int] = {}

        try:
            # 12. Execute the graph once while reading its events. The saver loads the thread and applies initial_state updates.
            # aclosing closes the event iterator when this block ends, including interruption or early exit.
            async with aclosing(graph.astream_events(
                initial_state, config=config, version="v2"
            )) as graph_events:
                async for event in graph_events:
                    kind = event.get("event", "")
                    name = event.get("name", "")

                    # 13a. For registered graph nodes, record a start time and yield a readable stage label. Ignore other event types.
                    if kind == "on_chain_start" and name in _NODE_LABELS:
                        _node_starts[name] = _time.monotonic()
                        yield json.dumps({
                            "type": "node_start",
                            "node": name,
                            "label": _NODE_LABELS[name],
                        })

                    # 13b. Record the completed stage duration/output and emit its end marker. LangGraph itself schedules the next node.
                    elif kind == "on_chain_end" and name in _NODE_LABELS:
                        if name in _node_starts:
                            _stage_timings_ms[name] = int((_time.monotonic() - _node_starts.pop(name)) * 1000)
                        output = event.get("data", {}).get("output")
                        if isinstance(output, dict):
                            final_state = output
                        yield json.dumps({
                            "type": "node_end",
                            "node": name,
                        })

            # 14. Execution has finished or paused. Read checkpoint task interrupts to distinguish pending review from completion.
            if checkpointer is not None:
                try:
                    graph_state = await graph.aget_state(config)
                    if graph_state.tasks:
                        for task in graph_state.tasks:
                            if hasattr(task, "interrupts") and task.interrupts:
                                interrupt_value = task.interrupts[0].value
                                # Send approval details without the generated draft or citations, then stop this response.
                                yield json.dumps({
                                    "type": "pending_approval",
                                    "reason": interrupt_value.get("reason", "Approval required"),
                                    "access_scope": interrupt_value.get("access_scope"),
                                    "thread_id": thread_id,
                                    "submitted_at": interrupt_value.get("submitted_at"),
                                    "approvers": approvers_for(user.role),
                                    "requester_role": user.role,
                                })
                                return
                # A failure after SSE started is sent as an error event; the HTTP status has already been sent.
                except Exception as e:
                    raise RuntimeError("Unable to check approval status") from e

            # 15. No interrupt remains: total this request's usage and send one complete checked response with sources/timing.
            cost_summary = req_cost.aggregate()
            _wall_clock_s = round(_time.monotonic() - _wall_start, 1)
            if final_state:
                metadata = final_state.get("response_metadata", {})
                yield json.dumps({
                    "type": "final",
                    "response": final_state.get("final_response", ""),
                    "sources": metadata.get("sources", []),
                    "confidence": metadata.get("confidence"),
                    "requires_approval": final_state.get("requires_human_approval", False),
                    "thread_id": thread_id,
                    "cost_usd": cost_summary["cost_usd"],
                    "tokens": cost_summary["tokens"],
                    "wall_clock_s": _wall_clock_s,
                    "stage_timings_ms": _stage_timings_ms,
                })
            else:
                yield json.dumps({
                    "type": "final",
                    "response": "No response generated.",
                    "sources": [],
                    "confidence": None,
                    "requires_approval": False,
                    "thread_id": thread_id,
                    "cost_usd": cost_summary["cost_usd"],
                    "tokens": cost_summary["tokens"],
                    "wall_clock_s": _wall_clock_s,
                    "stage_timings_ms": _stage_timings_ms,
                })

        except Exception as e:
            limit = rate_limit_payload(e)
            if limit:
                trace.record("provider_limit", **{k: v for k, v in limit.items() if k != "type"})
                yield json.dumps(limit)
                return
            logger.error(f"Streaming graph execution failed: {e}", exc_info=True)
            yield json.dumps({
                "type": "error",
                "message": "An error occurred processing your request.",
            })

    # OUTER GENERATOR (runs first when EventSourceResponse starts reading).
    # Own request context, conversation reservation and cleanup; forward the inner generator's updates with request IDs.
    async def event_generator():
        # 8. Make this trace available to request-local helpers through ContextVar, without passing it to every helper.
        with trace.activate():
            try:
                # 9. Reserve this conversation in the single worker. An overlapping write gets 409; leaving releases it.
                async with conversation_write(thread_id):
                    # Recheck ownership/pending status inside the reservation, in case it changed since the first check.
                    if checkpointer is not None:
                        await check_chat_access(graph, thread_id, user.user_id)
                    # 10. First browser event: identify this question and display the cleaned text before any graph node runs.
                    yield json.dumps({"type": "request_started", "request_id": trace.request_id,
                                      "thread_id": thread_id, "access_scope": access_scope,
                                      "sanitized_message": sanitized_message})
                    # Now start the inner generator above (step 11). Each yielded JSON string passes through this loop.
                    async with aclosing(unlocked_event_generator()) as events:
                        async for event in events:
                            payload = json.loads(event)
                            # Give every forwarded stage/result/error the same request ID so the browser links it to the correct local trace.
                            payload["request_id"] = trace.request_id
                            # Finalize paused/failed trace outcomes before forwarding the terminal event. No draft text is added here.
                            if payload.get("type") == "pending_approval":
                                trace.record("approval_pending", submitted_at=payload.get("submitted_at"),
                                             access_scope=payload.get("access_scope"), approvers=payload.get("approvers"))
                                trace.finish("awaiting_approval")
                            elif payload.get("type") == "error":
                                trace.finish("failed")
                            yield json.dumps(payload)
            # Access/busy failures inside the already-open response become SSE errors rather than a second HTTP response.
            except HTTPException as exc:
                trace.finish("failed")
                yield json.dumps({"type": "error", "message": exc.detail, "status": exc.status_code,
                                  "request_id": trace.request_id})
            # Cancellation/connection interruption also finalizes the trace and then propagates for normal cleanup.
            except BaseException:
                trace.finish("interrupted")
                raise
            # 16. Finalize the trace once on every exit. The reservation/context managers also release their resources.
            finally:
                trace.finish()

    # START SENDING HERE: Definitions above did not execute their bodies. The response now consumes outer step 8 first.
    return EventSourceResponse(event_generator())


# BROWSER ENTRY: FastAPI supplies path ID, validated body, raw Request and verified User.
# Copy the path ID into ChatRequest, then call chat_stream above as an ordinary Python function.
@router.post("/{thread_id}/messages/stream")
async def conversation_message_stream(thread_id: str, body: ChatRequest, http_request: Request,
                                      user: User = Depends(get_current_user)):
    return await chat_stream(body.model_copy(update={"thread_id": thread_id}), user, http_request)

