"""Terminal nodes for non-retrieval paths (blocked, out-of-scope, no-info, clarification)."""

from langchain_core.messages import AIMessage

from src.config.prompts import BLOCKED_RESPONSE, CLARIFICATION_RESPONSE, OUT_OF_SCOPE_RESPONSE
from src.graph.nodes.response_formatter import not_found_message, scope_note
from src.models.state import RAGState
from src.services.escalation import is_escalated

# Replies to a question asked with extra access that found nothing. They skip Admin review, so they
# must not name the companies, years or filings that only the extra access covers.
ESCALATED_UNAVAILABLE = (
    "I couldn't match your question to a company and year within the extra access you requested. "
    "Name the company, and the year if you need one, then ask again."
)
ESCALATED_NOT_FOUND = "I couldn't find this in the documents covered by the extra access you requested."


def blocked_response_node(state: RAGState) -> dict:
    if state.get("human_decision") == "unavailable":
        answer = "This answer requires approval, but approval storage is unavailable. Please try again later."
    elif state.get("human_decision") == "rejected":
        reason = (state.get("human_decision_reason") or "").strip() or "No reason was provided."
        quoted_reason = "\n".join("> " + line for line in reason.splitlines())
        answer = "The reviewer rejected this answer.\n\n**Message from reviewer:**\n\n" + quoted_reason
    else:
        answer = BLOCKED_RESPONSE
    return {
        "requires_human_approval": state.get("human_decision") in ("rejected", "unavailable"),
        "final_response": answer,
        "messages": [AIMessage(content=answer, additional_kwargs={"request_id": state.get("request_id")})],
        "response_metadata": {
            "reason": (
                "approval_" + state["human_decision"]
                if state.get("human_decision")
                else state.get("guardrail_status", "blocked")
            ),
            "sources": [],
        },
    }


def out_of_scope_node(state: RAGState) -> dict:
    return {
        "requires_human_approval": False,
        "messages": [
            AIMessage(content=OUT_OF_SCOPE_RESPONSE, additional_kwargs={"request_id": state.get("request_id")})
        ],
        "final_response": OUT_OF_SCOPE_RESPONSE,
        "response_metadata": {"reason": "out_of_scope"},
    }


def clarification_node(state: RAGState) -> dict:
    return {
        "requires_human_approval": False,
        "messages": [
            AIMessage(content=CLARIFICATION_RESPONSE, additional_kwargs={"request_id": state.get("request_id")})
        ],
        "final_response": CLARIFICATION_RESPONSE,
        "response_metadata": {"reason": "clarification"},
    }


def no_info_node(state: RAGState) -> dict:
    """Report no relevant evidence in the permitted search scope. Never probe outside it."""
    if is_escalated(state):
        # Only an approved answer may reveal what the extra access covers.
        answer = ESCALATED_UNAVAILABLE if state.get("metadata_unavailable") else ESCALATED_NOT_FOUND
        return {
            "requires_human_approval": False,
            "messages": [AIMessage(content=answer, additional_kwargs={"request_id": state.get("request_id")})],
            "final_response": answer,
            "response_metadata": {"reason": "no_relevant_info", "sources": []},
        }
    answer = not_found_message(state) + scope_note(state)
    if state.get("metadata_unavailable"):
        answer = state.get("unavailable_message") or (
            "I couldn't find the requested company or filing year in the documents available within your access."
        )
    return {
        "requires_human_approval": False,
        "messages": [AIMessage(content=answer, additional_kwargs={"request_id": state.get("request_id")})],
        "final_response": answer,
        "response_metadata": {"reason": "no_relevant_info", "sources": []},
    }
