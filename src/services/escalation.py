"""Parse a deliberate, first-line access request. Models never grant permissions."""
import re

from src.config.rbac_config import get_permissions

DOC_TYPES = {"10k", "10q", "8k", "invoice", "expense_policy", "board_report"}
CONFIDENTIALITY = {"public", "internal", "confidential"}
# Syntax check only: first line must be /escalate followed by doc_type=value and/or confidentiality=value.
# Values are comma-separated without spaces. IGNORECASE accepts upper/lowercase; Python below checks allowed values.
COMMAND = re.compile(r"/escalate(?:\s+(?:doc_type|confidentiality)=[a-z0-9_,]+)+", re.IGNORECASE)


# Called before the graph: return (clean question, effective scope). It does not call a model or change the JWT role.
def parse_escalation(message: str, role: str) -> tuple[str, dict | None]:
    """Return the clean question and expanded search scope, or no scope override.

    Example: /escalate doc_type=filings confidentiality=public\n<question>
    Requested values add to the role's ordinary permissions for this turn.
    Omitted fields retain their ordinary limits. Command wildcards are rejected.
    """
    # 1. An ordinary message has no override. Only a command at the start of the stripped message is parsed.
    text = message.strip()
    if not text.lower().startswith("/escalate"):
        return message, None
    # 2. Split at the first newline. The command belongs on the first line and a nonempty question follows it.
    header, separator, question = text.partition("\n")
    # Reject malformed commands before any retrieval/generation; the route converts ValueError into HTTP 400.
    if not COMMAND.fullmatch(header.strip()) or not separator or not question.strip():
        raise ValueError("Use /escalate doc_type=filings confidentiality=public on the first line, followed by your question on a new line.")
    requested = {}
    # 3. Read each field=value, reject duplicate fields, and split comma-separated values.
    for part in header.lower().split()[1:]:
        field, value = part.split("=", 1)
        if field in requested:
            raise ValueError(f"Specify {field} only once.")
        values = value.split(",")
        if field == "doc_type":
            # filings is a shortcut for the three actual Qdrant document-type labels: 10k, 10q and 8k.
            values = [kind for v in values for kind in (["10k", "10q", "8k"] if v == "filings" else [v])]
            allowed = DOC_TYPES
        else:
            allowed = CONFIDENTIALITY
        # The regex checks shape, but this allowlist rejects unknown values, empty entries and requested wildcards.
        if not values or any(v not in allowed for v in values):
            raise ValueError(f"Unknown {field}. Allowed values: {', '.join(sorted(allowed))}" + (", filings" if field == "doc_type" else ""))
        requested[field] = list(dict.fromkeys(values))
    # 4. Start with the role's normal permissions. Add requested types/levels without removing existing access.
    permissions = get_permissions(role)
    # Return new deduplicated lists so the shared role table is not modified. Admin's existing wildcard stays unchanged.
    def extend(normal: list[str], extra: list[str]) -> list[str]:
        return ["*"] if "*" in normal else list(dict.fromkeys(normal + extra))

    types = extend(permissions["allowed_doc_types"], requested.get("doc_type", []))
    levels = extend(permissions["allowed_confidentiality"], requested.get("confidentiality", []))
    # 5. Require review only when a list goes beyond ordinary permissions. Already-allowed requests remain ordinary.
    expanded = any("*" not in normal and not set(chosen).issubset(normal) for chosen, normal in [
        (types, permissions["allowed_doc_types"]), (levels, permissions["allowed_confidentiality"]),
    ])
    # Return the command-free question and combined scope. The graph uses this scope for this turn only.
    return question.strip(), {"doc_types": types, "confidentiality": levels, "escalated": expanded}


def is_escalated(state: dict) -> bool:
    return bool((state.get("access_scope") or {}).get("escalated"))
