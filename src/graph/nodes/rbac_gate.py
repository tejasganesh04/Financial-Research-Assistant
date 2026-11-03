import logging

from src.config.rbac_config import get_permissions
from src.models.state import RAGState

logger = logging.getLogger(__name__)


# Get document-type filters from the role, or the combined role-plus-escalation scope prepared by the API.
def rbac_gate(state: RAGState) -> dict:
    """Validate user role and set allowed document types."""
    role = state.get("user_role", "analyst")
    permissions = get_permissions(role)
    scope = state.get("access_scope") or {}
    allowed_types = scope.get("doc_types", permissions["allowed_doc_types"])

    logger.info(f"RBAC gate: role={role}, allowed_doc_types={allowed_types}")

    return {
        "allowed_doc_types": allowed_types,
    }
