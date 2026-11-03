"""Small shared checks for conversation ownership and pending approvals."""
from contextlib import asynccontextmanager

from fastapi import HTTPException
from langchain_core.messages import HumanMessage

from src.config.settings import settings

# The local API uses one worker. Reject overlapping writes to the same thread.
# Single-worker reservation list. Different conversations can run concurrently; this is not a multi-server lock.
_busy_threads: set[str] = set()


# ENTER: reject an already-busy thread and mark it busy. EXIT: remove the mark even after an error or cancellation.
@asynccontextmanager
async def conversation_write(thread_id: str):
    if thread_id in _busy_threads:
        raise HTTPException(status_code=409, detail="This conversation is already processing a request.")
    _busy_threads.add(thread_id)
    try:
        yield
    finally:
        _busy_threads.discard(thread_id)


# A paused checkpoint records interrupt data on its tasks; an approval_required payload identifies our review pause.
def pending_approval(snapshot) -> dict | None:
    for task in (getattr(snapshot, "tasks", ()) or ()):
        for item in (getattr(task, "interrupts", ()) or ()):
            value = item.value
            if isinstance(value, dict) and value.get("type") == "approval_required":
                return value
    return None


# NEW-QUESTION CHECK: Called before preparation and again inside the write reservation.
# Reads and approval decisions use other checks, so full conversations remain readable/reviewable.
async def check_chat_access(graph, thread_id: str, user_id: str):
    try:
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Conversation storage is unavailable. Please try again later.") from exc
    values = (snapshot.values if snapshot else None) or {}
    # No saved values means a new thread. Existing values must belong to the authenticated poster, including Admin.
    if values and values.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="This conversation belongs to another user.")
    if pending_approval(snapshot) is not None:
        raise HTTPException(status_code=409, detail="This conversation is waiting for approval. Start a new conversation.")
    # Count stored user questions, not assistant messages, graph steps or approval decisions.
    questions = sum(isinstance(message, HumanMessage) for message in (values.get("messages") or []))
    if questions >= settings.MAX_CONVERSATION_QUESTIONS:
        raise HTTPException(
            status_code=409,
            detail=f"This conversation has reached {settings.MAX_CONVERSATION_QUESTIONS} questions. Please start a new conversation.",
        )
    return snapshot
