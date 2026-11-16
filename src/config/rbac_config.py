# Document types and confidentiality levels each role may search.
ROLE_PERMISSIONS: dict[str, dict] = {
    "analyst": {
        "allowed_doc_types": ["10k", "10q", "8k"],
        "allowed_confidentiality": ["public"],
    },
    "finance": {
        "allowed_doc_types": ["10k", "10q", "8k", "invoice", "expense_policy"],
        "allowed_confidentiality": ["public", "internal"],
    },
    "hr": {
        "allowed_doc_types": ["expense_policy"],
        "allowed_confidentiality": ["public", "internal"],
    },
    "c_level": {
        "allowed_doc_types": ["10k", "10q", "8k", "invoice", "expense_policy", "board_report"],
        "allowed_confidentiality": ["public", "internal", "confidential"],
    },
    "admin": {
        "allowed_doc_types": ["*"],
        "allowed_confidentiality": ["*"],
    },
}


# Unknown roles fall back to analyst permissions.
def get_permissions(role: str) -> dict:
    """Read the fixed role permissions from this file."""
    return ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["analyst"])


# Only Admin releases another user's escalated draft; the API separately checks the reviewer is not the owner.
def can_approve(approver_role: str, requester_role: str) -> bool:
    return approver_role == "admin" and requester_role != "admin"


def approvers_for(requester_role: str) -> list[str]:
    return ["admin"] if requester_role != "admin" else []
