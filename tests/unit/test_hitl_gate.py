"""Explicit scope requests and release decisions, with no provider calls."""
from unittest.mock import patch

import pytest

from src.services.escalation import parse_escalation
from src.graph.nodes.hitl_gate import hitl_gate_node
from src.graph.nodes.rbac_gate import rbac_gate
from src.graph.nodes.retrieval import retrieval_node
from src.graph.nodes.terminal_nodes import no_info_node, blocked_response_node
from src.config.rbac_config import can_approve


def test_normal_large_amount_never_requires_review():
    assert parse_escalation("Revenue $500 billion", "finance")[1] is None
    assert hitl_gate_node({"user_role": "finance", "generated_answer": "$500 billion"}) == {"requires_human_approval": False, "human_decision": None}


def test_hr_filings_request_adds_to_existing_permissions():
    question, scope = parse_escalation("/escalate doc_type=filings confidentiality=public\nMicrosoft revenue?", "hr")
    assert question == "Microsoft revenue?"
    assert scope == {"doc_types": ["expense_policy", "10k", "10q", "8k"], "confidentiality": ["public", "internal"], "escalated": True}
    assert rbac_gate({"user_role": "hr", "access_scope": scope})["allowed_doc_types"] == scope["doc_types"]
    assert rbac_gate({"user_role": "hr"})["allowed_doc_types"] == ["expense_policy"]


@pytest.mark.parametrize("role", ["analyst", "finance", "c_level", "admin"])
def test_request_within_existing_rights_needs_no_review(role):
    _, scope = parse_escalation("/escalate doc_type=filings confidentiality=public\nRevenue?", role)
    assert not scope["escalated"]


def test_omitted_dimension_keeps_existing_role_limits():
    _, scope = parse_escalation("/escalate doc_type=filings\nRevenue?", "hr")
    assert scope["confidentiality"] == ["public", "internal"]
    _, scope = parse_escalation("/escalate confidentiality=confidential\nPolicy?", "hr")
    assert scope["doc_types"] == ["expense_policy"]
    assert scope["escalated"]


@pytest.mark.parametrize("command", ["/escalate", "/escalate doc_type=*\nQ", "/escalate doc_type=unknown\nQ", "/escalate confidentiality=secret\nQ", "/escalate doc_type=10k doc_type=8k\nQ", "/escalate doc_type=10k", "/escalate role=admin\nQ", "/escalate doc_type=10k,\nQ"])
def test_invalid_commands_rejected(command):
    with pytest.raises(ValueError):
        parse_escalation(command, "hr")


@pytest.mark.parametrize("decision", ["approved", "rejected"])
@pytest.mark.parametrize("legacy", [True, False])
def test_admin_decision_is_honored_including_old_pending_checkpoints(decision, legacy):
    state = {} if legacy else {"access_scope": {"escalated": True}}
    config = {"metadata": {"hitl_enabled": True, "resuming_approval": legacy}}
    with patch("src.graph.nodes.hitl_gate.interrupt", return_value={"decision": decision, "decided_by": "admin", "submitted_at": "original"}) as pause:
        result = hitl_gate_node(state, config)
    assert pause.call_args.args[0]["type"] == "approval_required"
    assert result["human_decision"] == decision
    assert result["hitl_submitted_at"] == "original"


def test_only_admin_can_review():
    for requester in ["hr", "finance", "analyst", "c_level"]:
        assert can_approve("admin", requester)
        for reviewer in ["hr", "finance", "analyst", "c_level"]:
            assert not can_approve(reviewer, requester)


def test_search_keeps_requested_scope_without_relaxation():
    _, scope = parse_escalation("/escalate doc_type=filings confidentiality=confidential\nRevenue?", "hr")
    state = {"user_role": "hr", "access_scope": scope, "contextualized_query": "Revenue?", "target_companies": ["microsoft"], "target_fiscal_years": [2023], **rbac_gate({"user_role": "hr", "access_scope": scope})}
    with patch("src.graph.nodes.retrieval.get_qdrant_client"), patch("src.graph.nodes.retrieval.embed_text", return_value=[0.0]), patch("src.graph.nodes.retrieval.hybrid_search", return_value=[]) as search:
        retrieval_node(state)
    assert search.call_count == 1
    for call in search.call_args_list:
        conditions = {c.key: c.match.any for c in call.kwargs["rbac_filter"].must if hasattr(c.match, "any")}
        assert conditions["doc_type"] == ["expense_policy", "10k", "10q", "8k"]
        assert conditions["confidentiality"] == ["public", "internal", "confidential"]
        assert conditions["company"] == ["microsoft"]
        assert conditions["fiscal_year"] == [2023]


@pytest.mark.parametrize("scope", [None, {"escalated": True}])
def test_empty_search_never_probes_outside_requested_scope(scope):
    # No search/embedding dependency is available to this terminal node.
    from src.graph.nodes import terminal_nodes
    assert not hasattr(terminal_nodes, "hybrid_search")
    assert not hasattr(terminal_nodes, "embed_text")
    result = no_info_node({"access_scope": scope, "user_role": "hr"})
    assert not result["requires_human_approval"]
    assert result["response_metadata"] == {"reason": "no_relevant_info", "sources": []}
    assert "Access restricted" not in result["final_response"]
    assert "10k" not in result["final_response"]
    assert not blocked_response_node({"access_scope": scope})["requires_human_approval"]


def test_internal_request_keeps_public_access_and_does_not_mutate_role():
    from src.config.rbac_config import get_permissions
    _, scope = parse_escalation("/escalate doc_type=filings confidentiality=internal\nMicrosoft FY2016 revenue?", "hr")
    assert scope["doc_types"] == ["expense_policy", "10k", "10q", "8k"]
    assert scope["confidentiality"] == ["public", "internal"]
    assert scope["escalated"]
    assert get_permissions("hr")["allowed_doc_types"] == ["expense_policy"]
    assert parse_escalation("Normal follow-up", "hr")[1] is None


def test_admin_wildcards_are_preserved_without_redundant_values():
    _, scope = parse_escalation("/escalate doc_type=filings confidentiality=internal\nRevenue?", "admin")
    assert scope == {"doc_types": ["*"], "confidentiality": ["*"], "escalated": False}
