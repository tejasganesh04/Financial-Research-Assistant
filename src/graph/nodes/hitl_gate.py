"""Hold an escalated answer until Admin authorizes its release."""
import logging
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from src.models.state import RAGState
from src.services.escalation import is_escalated

logger = logging.getLogger(__name__)


# HUMAN REVIEW: Expanded searches pause here after drafting. The API authorizes Admin before resuming this task.
def hitl_gate_node(state: RAGState, config: RunnableConfig | None = None) -> dict:
    metadata = (config or {}).get("metadata", {})
    # Preserve the approve/reject behavior for requests paused before this policy changed.
    if not is_escalated(state) and not metadata.get("resuming_approval"):
        return {"requires_human_approval": False, "human_decision": None}
    if not metadata.get("hitl_enabled", False):
        return {"requires_human_approval": True, "human_decision": "unavailable"}

    scope = state.get("access_scope") or {}
    submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # On first execution this pauses the graph. On resume the node restarts and this call returns the reviewer's value.
    decision = interrupt({
        "type": "approval_required",
        "reason": "This answer uses a search scope outside the requester's role. Admin approval is required before release.",
        "access_scope": scope,
        "request_id": state.get("request_id"),
        "submitted_at": submitted_at,
    })
    # Execution resumes here after the human responds. NOTE: this whole
    # function runs again from the top on resume, so the local
    # `submitted_at` was just recomputed (it's now() of the resume call,
    # NOT the original pause time). The API forwards the ORIGINAL value
    # through the resume dict's "submitted_at" field to preserve the
    # audit trail. Fall back to the local value only if the approver used
    # the legacy bare-string resume.
    logger.info(f"HITL decision received: {decision}")
    if isinstance(decision, dict):
        human_decision = "approved" if decision.get("decision") == "approved" else "rejected"
        decision_at = decision.get("decided_at")
        decision_by = decision.get("decided_by")
        decision_by_role = decision.get("decided_by_role")
        decision_reason = decision.get("reason") or ""
        original_submitted_at = decision.get("submitted_at") or submitted_at
    else:
        human_decision = "approved" if decision == "approved" else "rejected"
        decision_at = None
        decision_by = None
        decision_by_role = None
        decision_reason = ""
        original_submitted_at = submitted_at

    return {
        "requires_human_approval": True,
        "human_decision": human_decision,
        "human_decision_at": decision_at,
        "human_decision_by": decision_by,
        "human_decision_by_role": decision_by_role,
        "human_decision_reason": decision_reason,
        "hitl_submitted_at": original_submitted_at,
    }

