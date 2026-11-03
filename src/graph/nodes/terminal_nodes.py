"""Terminal nodes for non-retrieval paths (blocked, out-of-scope, no-info, clarification)."""

from langchain_core.messages import AIMessage

from src.config.prompts import BLOCKED_RESPONSE, CLARIFICATION_RESPONSE, NO_INFO_RESPONSE, OUT_OF_SCOPE_RESPONSE
from src.models.state import RAGState


def blocked_response_node(state: RAGState) -> dict:
    if state.get("human_decision") == "unavailable":
        answer = "This answer requires approval, but approval storage is unavailable. Please try again later."
    elif state.get("human_decision") == "rejected":
        reason = (state.get("human_decision_reason") or "").strip() or "No reason was provided."
        quoted_reason = "\n".join("> " + line for line in reason.splitlines())
        answer = "The reviewer rejected this answer.\n\n**Message from reviewer:**\n\n" + quoted_reason
    else:
        answer = BLOCKED_RESPONSE
    return {"requires_human_approval": state.get("human_decision") in ("rejected", "unavailable"), "final_response": answer, "messages": [AIMessage(content=answer, additional_kwargs={"request_id": state.get("request_id")})],
            "response_metadata": {"reason": ("approval_" + state["human_decision"]
                if state.get("human_decision") else state.get("guardrail_status", "blocked")), "sources": []}}


def out_of_scope_node(state: RAGState) -> dict:
    return {"requires_human_approval": False, "messages": [AIMessage(content=OUT_OF_SCOPE_RESPONSE, additional_kwargs={"request_id": state.get("request_id")})], "final_response": OUT_OF_SCOPE_RESPONSE, "response_metadata": {"reason": "out_of_scope"}}


def clarification_node(state: RAGState) -> dict:
    return {"requires_human_approval": False, "messages": [AIMessage(content=CLARIFICATION_RESPONSE, additional_kwargs={"request_id": state.get("request_id")})], "final_response": CLARIFICATION_RESPONSE, "response_metadata": {"reason": "clarification"}}


def no_info_node(state: RAGState) -> dict:
    """Report no relevant evidence in the permitted search scope. Never probe outside it."""
    answer = NO_INFO_RESPONSE
    if state.get("metadata_unavailable"):
        answer = "I couldn't find the requested company or filing year in the documents available within your search permissions."
    return {
        "requires_human_approval": False,
        "messages": [AIMessage(content=answer, additional_kwargs={"request_id": state.get("request_id")})],
        "final_response": answer,
        "response_metadata": {"reason": "no_relevant_info", "sources": []},
    }
