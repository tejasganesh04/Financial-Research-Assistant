"""Review pending answers and approve or reject them."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.types import Command
from pydantic import BaseModel

from src.api.dependencies import get_current_user
from src.api.routes.conversations import _get_graph
from src.config.rbac_config import can_approve
from src.graph.nodes.response_formatter import source_references
from src.models.auth import User
from src.services.conversation_access import conversation_write, pending_approval
from src.services.thread_service import count_hitl_decisions_on_thread, get_thread_owner_role, list_all_threads


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _pool_or_503(http_request: Request):
    pool = getattr(http_request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Checkpoint store not initialized — approvals disabled",
        )
    return pool


def _is_interrupted(graph_state) -> tuple[bool, dict | None]:
    for task in (graph_state.tasks or []):
        if getattr(task, "interrupts", None):
            v = task.interrupts[0].value
            return True, v if isinstance(v, dict) else {"value": str(v)}
    return False, None


@router.get("")
async def list_approvals(
    http_request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """List pending HITL interrupts the caller can approve."""
    pool = _pool_or_503(http_request)

    rows = await list_all_threads(pool)

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        from src.api.routes.conversations import _get_graph
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    pending: list[dict[str, Any]] = []
    for r in rows:
        thread_id = r["thread_id"]
        requester_user_id = r.get("user_id") or "?"
        requester_role = r.get("role") or "?"

        if requester_user_id == user.user_id or not can_approve(user.role, requester_role):
            continue

        cfg = {"configurable": {"thread_id": thread_id}}
        try:
            gs = await graph.aget_state(cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("aget_state failed for thread %s: %s", thread_id, exc)
            continue
        interrupted, payload = _is_interrupted(gs)
        if not interrupted:
            continue

        query = ""
        sources_count = 0
        confidence = None
        if gs and gs.values:
            query = (gs.values.get("original_query") or gs.values.get("contextualized_query") or "").strip()
            sources_count = len(source_references(gs.values.get("relevant_chunks") or []))
            confidence = gs.values.get("hallucination_score")

        pending.append({
            "thread_id": thread_id,
            "requester_user_id": requester_user_id,
            "requester_name": r.get("name"),
            "requester_department": r.get("department"),
            "requester_role": requester_role,
            "query": query[:200],
            "reason": (payload or {}).get("reason"),
            "access_scope": (payload or {}).get("access_scope"),
            "submitted_at": (payload or {}).get("submitted_at"),
            "sources_count": sources_count,
            "confidence": confidence,
        })

    return {"approvals": pending, "count": len(pending)}


@router.get("/{thread_id}")
async def show_approval(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Full review payload for a pending interrupt — including the draft answer
    that was suppressed from the requester's interface."""
    pool = _pool_or_503(http_request)

    from src.services.thread_service import get_thread_owner_role
    owner, requester_role, requester_name, requester_dept = await get_thread_owner_role(pool, thread_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    if owner == user.user_id or not can_approve(user.role, requester_role or ""):
        raise HTTPException(
            status_code=403,
            detail=f"Your role '{user.role}' cannot approve requests from '{requester_role}'.",
        )

    graph = getattr(http_request.app.state, "graph", None)
    if graph is None:
        from src.api.routes.conversations import _get_graph
        graph = _get_graph(checkpointer=http_request.app.state.checkpointer)

    cfg = {"configurable": {"thread_id": thread_id}}
    gs = await graph.aget_state(cfg)
    if gs is None:
        raise HTTPException(status_code=404, detail="Thread state unavailable")
    interrupted, payload = _is_interrupted(gs)
    if not interrupted:
        raise HTTPException(status_code=409, detail="Thread is not awaiting approval")

    state_values = gs.values or {}
    draft = state_values.get("generated_answer") or state_values.get("final_response") or ""
    query = state_values.get("original_query") or state_values.get("contextualized_query") or ""
    sources = source_references(state_values.get("relevant_chunks") or [])
    source_files = sorted({s.get("file") for s in sources if isinstance(s, dict) and s.get("file")})

    # Review context: pending age and earlier decisions help the reviewer assess this request.
    #   submitted_at_age_seconds: pre-computed age so the interface doesn't have to
    #     do timezone math. None if submitted_at is missing or malformed.
    #   prior_decisions_on_thread: catches re-submissions / bouncing patterns
    #     (e.g. requester re-asks a question that was already rejected).
    submitted_at_str = (payload or {}).get("submitted_at")
    submitted_at_age_s: float | None = None
    if submitted_at_str:
        try:
            from datetime import datetime, timezone
            submitted_dt = datetime.fromisoformat(submitted_at_str)
            if submitted_dt.tzinfo is None:
                submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)
            submitted_at_age_s = (datetime.now(timezone.utc) - submitted_dt).total_seconds()
        except (ValueError, TypeError):
            pass
    prior_decisions = await count_hitl_decisions_on_thread(pool, thread_id)

    return {
        "thread_id": thread_id,
        "request_id": state_values.get("request_id"),
        "requester_user_id": owner,
        "requester_name": requester_name,
        "requester_department": requester_dept,
        "requester_role": requester_role,
        "query": query,
        "draft_answer": draft,
        "reason": (payload or {}).get("reason"),
        "access_scope": (payload or {}).get("access_scope"),
        "submitted_at": submitted_at_str,
        "submitted_at_age_seconds": submitted_at_age_s,
        "prior_decisions_on_thread": prior_decisions,
        "sources_count": len(sources),
        "source_files": source_files,
        "sources": sources,
        "confidence": state_values.get("hallucination_score"),
    }


class HITLDecisionRequest(BaseModel):
    thread_id: str
    reason: str | None = None


# REVIEW FLOW: Authorize Admin and verify a real pending task, then resume its checkpoint; do not regenerate the draft.
async def _decide(body: HITLDecisionRequest, request: Request, user: User, decision: str):
    if decision == "rejected" and not (body.reason or "").strip():
        raise HTTPException(status_code=400, detail="A reason is required when rejecting an answer.")
    pool = getattr(request.app.state, "pool", None)
    checkpointer = getattr(request.app.state, "checkpointer", None)
    if pool is None or checkpointer is None:
        raise HTTPException(status_code=503, detail="Approval storage is unavailable.")

    async with conversation_write(body.thread_id):
        owner, role, name, department = await get_thread_owner_role(pool, body.thread_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        if owner == user.user_id or not can_approve(user.role, role or ""):
            raise HTTPException(status_code=403, detail="You cannot approve this request or your own answer.")
        graph = _get_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": body.thread_id}, "metadata": {
            "user_id": owner, "role": role, "name": name, "department": department,
            "thread_id": body.thread_id, "hitl_enabled": True,
        }}
        snapshot = await graph.aget_state(config)
        pending = pending_approval(snapshot)
        if pending is None:
            raise HTTPException(status_code=409, detail="This conversation is not waiting for approval.")
        # Trusted backend flag also preserves approve/reject handling for older paused checkpoints without an escalation scope.
        config["metadata"]["resuming_approval"] = True
        payload = {
            "decision": decision,
            "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "decided_by": user.user_id, "decided_by_role": user.role,
            "reason": (body.reason or "").strip(), "submitted_at": pending.get("submitted_at"),
        }
        try:
            # Resume the saved task with the reviewer decision. The approval node restarts and interrupt() returns this payload.
            result = await graph.ainvoke(Command(resume=payload), config=config)
        except Exception as exc:
            logger.exception("Failed to resume approval for thread %s", body.thread_id)
            raise HTTPException(status_code=503, detail="Unable to complete approval. Check its status before retrying.") from exc
        metadata = result.get("response_metadata") or {}
        from src.services.request_trace import record_approval
        record_approval(body.thread_id, decision=decision, decided_at=payload["decided_at"],
                        submitted_at=payload["submitted_at"], reviewer_role=user.role)
        return {
            "status": decision, "thread_id": body.thread_id,
            "approver_user_id": user.user_id, "approver_role": user.role,
            "decided_at": payload["decided_at"], "submitted_at": payload["submitted_at"],
            "reason": payload["reason"], "response": result.get("final_response", ""),
            "sources": metadata.get("sources", []), "confidence": metadata.get("confidence"),
        }


class DecisionRequest(BaseModel):
    reason: str | None = None


@router.post("/{thread_id}/approve")
async def approve_response(thread_id: str, http_request: Request,
                           body: DecisionRequest = DecisionRequest(),
                           user: User = Depends(get_current_user)):
    return await _decide(HITLDecisionRequest(thread_id=thread_id, reason=body.reason), http_request, user, "approved")


@router.post("/{thread_id}/reject")
async def reject_response(thread_id: str, body: DecisionRequest, http_request: Request,
                          user: User = Depends(get_current_user)):
    return await _decide(HITLDecisionRequest(thread_id=thread_id, reason=body.reason), http_request, user, "rejected")
