"""Regression checks for the conversation flow. No external services or API calls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from src.api.main import app
from src.api.routes.conversations import _build_initial_state
from src.api.routes.conversations import _messages_from_state
from src.config.rbac_config import ROLE_PERMISSIONS
from src.graph.edges import route_after_hallucination, route_after_hitl
from src.graph.nodes.hallucination import hallucination_checker_node
from src.graph.nodes.hitl_gate import hitl_gate_node
from src.graph.nodes.response_formatter import response_formatter_node
from src.models.auth import User
from src.models.schemas import ChatRequest, HallucinationCheck
from src.models.state import RAGState
from src.services.auth_service import create_token
from src.services.company_registry import CORPUS_COMPANIES, company_mentions
from src.services.conversation_access import check_chat_access, conversation_write

CHUNK = {"content": "Revenue was $200,000.", "metadata": {"source_file": "filing.pdf", "page_number": 8}}


def token(user="finance", role="finance"):
    return {"Authorization": "Bearer " + create_token(user_id=user, name=user, role=role, department="test")}


def snapshot(pending=False):
    return SimpleNamespace(values={"user_id": "finance", "user_role": "finance"}, tasks=[
        SimpleNamespace(interrupts=[SimpleNamespace(value={"type": "approval_required", "submitted_at": "original"})])
    ] if pending else [])


@pytest.mark.parametrize("message", ["", " \t\n", "\u2003"])
@pytest.mark.parametrize("path", [
    "/v1/conversations/messages",
    "/v1/conversations/messages/stream",
    "/v1/conversations/test-thread/messages",
    "/v1/conversations/test-thread/messages/stream",
])
def test_blank_questions_rejected_before_graph_preparation(message, path):
    with patch("src.api.routes.conversations._get_graph") as get_graph:
        response = TestClient(app).post(path, json={"message": message}, headers=token())
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "message"]
    get_graph.assert_not_called()


@pytest.mark.parametrize("message", ["x" * 2001, "\U0001fa99" * 2001, "/escalate doc_type=filings\n" + "x" * 2000])
@pytest.mark.parametrize("path", [
    "/v1/conversations/messages",
    "/v1/conversations/messages/stream",
    "/v1/conversations/test-thread/messages",
    "/v1/conversations/test-thread/messages/stream",
])
def test_overlong_messages_rejected_before_graph_preparation(message, path):
    with patch("src.api.routes.conversations._get_graph") as get_graph:
        response = TestClient(app).post(path, json={"message": message}, headers=token())
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["loc"] == ["body", "message"]
    assert error["type"] == "string_too_long"
    assert error["ctx"]["max_length"] == 2000
    get_graph.assert_not_called()


@pytest.mark.parametrize("message", ["x" * 2000, "\U0001fa99" * 2000])
def test_message_at_character_limit_is_valid(message):
    assert ChatRequest(message=message).message == message


@pytest.mark.parametrize("path", [
    "/v1/conversations/messages",
    "/v1/conversations/messages/stream",
    "/v1/conversations/test-thread/messages",
    "/v1/conversations/test-thread/messages/stream",
])
def test_full_conversation_rejected_before_redaction_or_graph_execution(path, monkeypatch):
    saved = snapshot()
    saved.values["messages"] = [HumanMessage(content=f"Question {i}") for i in range(20)]
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=saved), ainvoke=AsyncMock(), astream_events=MagicMock())
    monkeypatch.setattr(app.state, "checkpointer", object(), raising=False)
    with patch("src.api.routes.conversations._get_graph", return_value=graph), patch(
        "src.api.routes.conversations.detect_pii"
    ) as redact:
        response = TestClient(app).post(path, json={"message": "Next question", "thread_id": "test-thread"}, headers=token())
    assert response.status_code == 409
    assert "20 questions" in response.json()["detail"]
    redact.assert_not_called()
    graph.ainvoke.assert_not_called()
    graph.astream_events.assert_not_called()


@pytest.mark.asyncio
async def test_conversation_count_ignores_assistant_messages():
    saved = snapshot()
    saved.values["messages"] = [HumanMessage(content=f"Question {i}") for i in range(19)] + [
        AIMessage(content="Answer") for _ in range(100)
    ]
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=saved))
    assert await check_chat_access(graph, "thread", "finance") is saved


@pytest.mark.parametrize("streaming", [False, True])
def test_question_limit_rechecked_inside_write_reservation(streaming, monkeypatch):
    before, after = snapshot(), snapshot()
    before.values["messages"] = [HumanMessage(content="Question") for _ in range(19)]
    after.values["messages"] = [HumanMessage(content="Question") for _ in range(20)]
    graph = SimpleNamespace(aget_state=AsyncMock(side_effect=[before, after]),
                            ainvoke=AsyncMock(), astream_events=MagicMock())
    monkeypatch.setattr(app.state, "checkpointer", object(), raising=False)
    path = "/v1/conversations/messages" + ("/stream" if streaming else "")
    with patch("src.api.routes.conversations._get_graph", return_value=graph), patch(
        "src.api.routes.conversations.detect_pii", return_value=("Question", [])
    ):
        response = TestClient(app).post(path, json={"message": "Question", "thread_id": "thread"}, headers=token())
    if streaming:
        assert response.status_code == 200
        assert '"status": 409' in response.text
        assert '"type": "request_started"' not in response.text
    else:
        assert response.status_code == 409
    assert "20 questions" in response.text
    graph.ainvoke.assert_not_called()
    graph.astream_events.assert_not_called()


def test_missing_approval_storage_never_approves():
    state = hitl_gate_node({"generated_answer": "$200K", "user_role": "finance", "access_scope": {"escalated": True}})
    assert state["human_decision"] == "unavailable"
    assert route_after_hitl(state) == "rejected"


@pytest.mark.parametrize("checker_failure", [True, False])
def test_failed_or_low_confidence_checks_stop_retrying(checker_failure):
    state = {"generated_answer": "Answer", "relevant_chunks": [CHUNK], "user_role": "analyst"}
    llm = MagicMock()
    if checker_failure:
        llm.with_structured_output.return_value.invoke.side_effect = RuntimeError("offline failure")
    else:
        llm.with_structured_output.return_value.invoke.return_value = HallucinationCheck(
            grounded=True, score=0.1, explanation="Uncertain")
    with patch(
        "src.graph.nodes.hallucination.LLMFactory.get_hallucination_llm", return_value=llm
    ):
        for _ in range(10):
            state.update(hallucination_checker_node(state))
            if route_after_hallucination(state) != "retry":
                break
        else:
            pytest.fail("Verification retries did not stop")
    assert route_after_hallucination(state) == "disclaimer"
    assert "could not be fully verified" in response_formatter_node(state)["final_response"]


def test_no_evidence_is_not_full_confidence():
    result = hallucination_checker_node({"generated_answer": "Answer", "relevant_chunks": []})
    assert result["hallucination_score"] == 0
    assert result["hallucination_status"] == "unverified"


def test_each_turn_resets_every_state_field_except_accumulated_messages():
    user = User(user_id="analyst", name="Test", role="analyst", department="test")
    state = _build_initial_state("Question", user)
    assert set(RAGState.__annotations__) <= set(state)
    assert state["agent_synthesis"] is None
    assert state["human_decision_at"] is None


@pytest.mark.asyncio
async def test_cross_user_and_pending_threads_rejected_before_graph_runs():
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=snapshot()))
    with pytest.raises(HTTPException) as exc:
        await check_chat_access(graph, "thread", "stranger")
    assert exc.value.status_code == 403
    graph.aget_state.return_value = snapshot(pending=True)
    with pytest.raises(HTTPException) as exc:
        await check_chat_access(graph, "thread", "finance")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_same_thread_rejects_overlapping_writes_and_unlocks_after_error():
    with pytest.raises(ValueError):
        async with conversation_write("thread"):
            with pytest.raises(HTTPException) as exc:
                async with conversation_write("thread"):
                    pass
            assert exc.value.status_code == 409
            raise ValueError("cancelled operation")
    async with conversation_write("thread"):
        pass


def test_pending_history_hides_current_draft_but_keeps_previous_answer():
    messages = [HumanMessage(content="old question"), AIMessage(content="released answer"),
                HumanMessage(content="new question"), AIMessage(content="secret draft")]
    history = _messages_from_state({"messages": messages, "requires_human_approval": True})
    assert [m["content"] for m in history] == ["old question", "released answer", "new question"]


@pytest.mark.parametrize("slug", CORPUS_COMPANIES)
def test_all_corpus_company_names_recognized_without_metadata_download(slug):
    name = slug.replace("_", " ")
    assert slug in {company for _, _, company in company_mentions(f"What was {name}'s revenue in 2023?")}


def test_cost_is_not_a_company_but_explicit_ticker_is():
    assert not company_mentions("What was the cost of revenue?")
    assert {company for _, _, company in company_mentions("COST revenue?")} == {"costco"}
    assert {company for _, _, company in company_mentions("Compare Microsoft and Nike")} == {"microsoft", "nike"}


def test_role_boundaries_are_simple_and_explicit():
    for role in ("analyst", "finance", "c_level"):
        assert {"10k", "10q", "8k"} <= set(ROLE_PERMISSIONS[role]["allowed_doc_types"])
    assert ROLE_PERMISSIONS["analyst"]["allowed_confidentiality"] == ["public"]
    assert "board_report" not in ROLE_PERMISSIONS["finance"]["allowed_doc_types"]
    assert ROLE_PERMISSIONS["hr"]["allowed_doc_types"] == ["expense_policy"]


@pytest.mark.parametrize("url", ["/v1/conversations/messages"])
@pytest.mark.parametrize("pending,expected", [(False, 403), (True, 409)])
def test_http_chat_rejects_wrong_owner_or_pending_thread(url, pending, expected):
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot(pending))
    old = getattr(app.state, "checkpointer", None)
    app.state.checkpointer = object()
    try:
        with patch("src.api.routes.conversations._get_graph", return_value=graph):
            response = TestClient(app).post(url, json={"message": "Question", "thread_id": "thread"},
                headers=token("finance" if pending else "stranger"))
        assert response.status_code == expected
        graph.ainvoke.assert_not_called()
    finally:
        app.state.checkpointer = old


def test_warmup_requires_explicit_admin_post_without_calling_providers():
    client = TestClient(app)
    assert client.get("/v1/warm").status_code == 404
    assert client.post("/v1/warm").status_code == 404
    assert client.post("/v1/warm", headers=token()).status_code == 404


def test_old_pending_checkpoint_hides_draft_even_before_gate_flag_is_written():
    values = {"messages": [HumanMessage(content="Question"), AIMessage(content="Secret draft")],
              "requires_human_approval": False}
    assert _messages_from_state(values, interrupted=True) == [{"role": "user", "content": "Question"}]
