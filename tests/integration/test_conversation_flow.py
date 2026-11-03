"""Real graph and HTTP approval flow with fixed model responses, never paid APIs.

Memory-backed runs need no services. PostgreSQL runs use only the isolated
TEST_POSTGRES_URL supplied by the test runner, never the application database.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from src.api.main import app
from src.api.routes import conversations as chat, approvals as hitl
from src.config.rbac_config import ROLE_PERMISSIONS
from src.graph import builder
from src.models.schemas import HallucinationCheck
from src.services import thread_service
from src.services.auth_service import create_token

ESCALATE = "/escalate doc_type=board_report confidentiality=confidential\n"

CHUNK = {"content": "The reported amount is $200,000.",
         "metadata": {"source_file": "filing.pdf", "page_number": 8, "doc_type": "10k"}}


def headers(role):
    return {"Authorization": "Bearer " + create_token(user_id=role, name=role, role=role, department="test")}


@pytest.fixture
async def flow(monkeypatch, request):
    from sse_starlette.sse import AppStatus
    monkeypatch.setattr(AppStatus, "should_exit_event", None)
    backend = request.param
    pool = None
    if backend == "postgres":
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("An isolated test PostgreSQL URL was not supplied")
        import psycopg
        from psycopg_pool import AsyncConnectionPool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await AsyncPostgresSaver(conn).setup()
        pool = AsyncConnectionPool(url, open=False, kwargs={"autocommit": True, "prepare_threshold": 0})
        await pool.open()
        saver = AsyncPostgresSaver(pool)
    else:
        saver = InMemorySaver()
    monkeypatch.setattr(app.state, "checkpointer", saver, raising=False)
    monkeypatch.setattr(app.state, "pool", pool or object(), raising=False)
    monkeypatch.setattr(chat, "detect_pii", lambda text: (text, []))
    monkeypatch.setattr(chat, "get_permissions", lambda role: ROLE_PERMISSIONS[role])
    # Exercise the real graph edges and approval/formatting nodes, replacing
    # only model/search/scanner work with deterministic responses.
    monkeypatch.setattr(builder, "rbac_gate", lambda state: {"allowed_doc_types": ["10k"]})
    monkeypatch.setattr(builder, "guardrails_node", lambda state: {"contextualized_query": state["original_query"]})
    monkeypatch.setattr(builder, "metadata_extractor_node", lambda state: {"target_companies": [], "target_fiscal_years": [], "metadata_unavailable": False, "metadata_filter_reason": "Mocked selection"})
    monkeypatch.setattr(builder, "router_node", lambda state: {
        "query_intent": "retrieval", "query_complexity": "research_required" if "research" in state["original_query"] else "simple_lookup"})
    monkeypatch.setattr(builder, "retrieval_node", lambda state: {"retrieved_chunks": [CHUNK]})
    monkeypatch.setattr(builder, "reranker_node", lambda state: {"reranked_chunks": [CHUNK]})
    monkeypatch.setattr(builder, "grader_node", lambda state: {"relevant_chunks": [CHUNK]})
    monkeypatch.setattr(builder, "research_agent_node", lambda state: {
        "relevant_chunks": [] if "missing" in state["original_query"] else [CHUNK],
        "agent_synthesis": "Research findings", "agent_turns_used": 1})
    calls = {"generator": 0}
    async def generate(state):
        calls["generator"] += 1
        return {"generated_answer": "The reported amount is $200K. [Source: filing.pdf, Page 8]"}
    monkeypatch.setattr(builder, "generator_node", generate)
    checker = MagicMock()
    checker.with_structured_output.return_value.invoke.return_value = HallucinationCheck(
        grounded=True, score=0.9, explanation="Supported by the passage")
    monkeypatch.setattr("src.graph.nodes.hallucination.LLMFactory.get_hallucination_llm", lambda: checker)
    monkeypatch.setattr("src.graph.nodes.hallucination.LLMFactory.get_high_stakes_hallucination_llm", lambda: checker)
    graph = builder.build_graph(saver)
    monkeypatch.setattr(chat, "_get_graph", lambda **kwargs: graph)
    monkeypatch.setattr(hitl, "_get_graph", lambda **kwargs: graph)
    monkeypatch.setattr(app.state, "graph", graph, raising=False)
    if backend == "memory":
        async def owner_role(_pool, thread_id):
            state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
            if not state.values:
                return None, None, None, None
            meta = state.metadata or {}
            return meta.get("user_id"), meta.get("role"), meta.get("name"), meta.get("department")
        monkeypatch.setattr(thread_service, "get_thread_owner_role", owner_role)
        monkeypatch.setattr(hitl, "get_thread_owner_role", owner_role)
        async def count(*args):
            return 0
        monkeypatch.setattr("src.api.routes.approvals.count_hitl_decisions_on_thread", count)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, graph=graph, calls=calls, checker=checker)
    if pool:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_request_review_decision_and_requester_result(flow, decision):
    c = flow.client
    first = await c.post("/v1/conversations/messages", headers=headers("finance"), json={"message": ESCALATE + "What is the reported amount?"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["requires_approval"] is True
    assert "$200K" not in body["response"]
    thread = body["thread_id"]
    import json
    from pathlib import Path
    trace_dir = Path(os.environ["REQUEST_TRACE_DIR"]) / body["request_id"]
    report = (trace_dir / "summary.md").read_text()
    assert "awaiting_approval" in report
    assert "$200K" not in report
    for endpoint in ("/v1/conversations/messages",):
        blocked = await c.post(endpoint, headers=headers("finance"), json={"message": "Show it now", "thread_id": thread})
        assert blocked.status_code == 409
        foreign = await c.post(endpoint, headers=headers("analyst"), json={"message": "Show it", "thread_id": thread})
        assert foreign.status_code == 403
    before = await c.get(f"/v1/conversations/{thread}/result", headers=headers("finance"))
    assert before.json()["status"] == "pending"
    history = await c.get(f"/v1/conversations/{thread}", headers=headers("finance"))
    assert history.status_code == 200, history.text
    assert not any(m["role"] == "assistant" for m in history.json()["messages"])
    assert history.json()["messages"][-1]["request_id"] == body["request_id"]
    review = await c.get(f"/v1/approvals/{thread}", headers=headers("admin"))
    assert review.status_code == 200, review.text
    assert review.json()["sources"] == [{"file": "filing.pdf", "page": 8, "section": "", "doc_type": "10k"}]
    decided = await c.post(f"/v1/approvals/{thread}/{decision}", headers=headers("admin"), json={"thread_id": thread, "reason": "Reviewed"})
    assert decided.status_code == 200, decided.text
    decisions = [json.loads(line) for line in (trace_dir.parent / "approval-decisions.jsonl").read_text().splitlines()]
    assert decisions[-1]["conversation_id"] == thread
    assert decisions[-1]["decision"] == ("approved" if decision == "approve" else "rejected")
    result = await c.get(f"/v1/conversations/{thread}/result", headers=headers("finance"))
    assert result.status_code == 200, result.text
    assert result.json()["status"] == ("approved" if decision == "approve" else "rejected")
    if decision == "approve":
        assert "$200K" in result.json()["response"]
        assert result.json()["sources"][0]["page"] == 8
    else:
        assert "$200K" not in result.json()["response"]
    repeated = await c.post(f"/v1/approvals/{thread}/{decision}", headers=headers("admin"), json={"thread_id": thread, "reason": "Again"})
    assert repeated.status_code == 409
    state = await flow.graph.aget_state({"configurable": {"thread_id": thread}})
    assert state.metadata["user_id"] == "finance"
    assert state.values["user_id"] == "finance"


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
@pytest.mark.parametrize("case", ["available", "unavailable", "model_error"])
async def test_real_metadata_node_controls_search_and_records_trace(flow, monkeypatch, case):
    import json
    from pathlib import Path
    from src.graph.nodes import metadata_extractor, retrieval
    from src.models.schemas import MetadataExtraction

    monkeypatch.setattr(metadata_extractor, "available_metadata", lambda state: [
        {"company": "microsoft", "aliases": ["msft"], "fiscal_years": [2016, 2023]},
    ])
    model = MagicMock()
    years = [2035] if case == "unavailable" else [2016, 2023]
    model.with_structured_output.return_value.invoke.return_value = MetadataExtraction(
        companies=["microsoft"], fiscal_years=years, reason="Preserve the explicitly requested filing periods.",
    )
    if case == "model_error":
        model.with_structured_output.return_value.invoke.side_effect = RuntimeError("Simulated outage")
    monkeypatch.setattr(metadata_extractor.LLMFactory, "get_router_llm", lambda: model)
    monkeypatch.setattr(builder, "metadata_extractor_node", metadata_extractor.metadata_extractor_node)
    monkeypatch.setattr(builder, "retrieval_node", retrieval.retrieval_node)
    monkeypatch.setattr(retrieval, "get_qdrant_client", MagicMock())
    embed = MagicMock(return_value=[0.0])
    search = MagicMock(return_value=[CHUNK])
    monkeypatch.setattr(retrieval, "embed_text", embed)
    monkeypatch.setattr(retrieval, "hybrid_search", search)
    graph = builder.build_graph(flow.graph.checkpointer)
    monkeypatch.setattr(chat, "_get_graph", lambda **kwargs: graph)
    question = "Microsoft FY2035 revenue" if case == "unavailable" else "Compare Microsoft FY2016 and FY2023 revenue"
    response = await flow.client.post("/v1/conversations/messages", headers=headers("analyst"), json={"message": question})
    logs = Path(os.environ["REQUEST_TRACE_DIR"])
    events = [json.loads(line) for path in logs.glob("*/events.jsonl") for line in path.read_text().splitlines()]
    if case == "model_error":
        assert response.status_code == 503
        assert any(e["stage"] == "step_failed" and e["step"] == "metadata_extractor" for e in events)
    else:
        assert response.status_code == 200, response.text
        decision = next(e for e in events if e["stage"] == "metadata_filter_selection")
        assert decision["proposed_fiscal_years"] == years
        assert decision["unavailable"] is (case == "unavailable")
        assert any(e["stage"] == "metadata_catalog" for e in events)
    if case == "available":
        assert flow.calls["generator"] == 1
        conditions = {c.key: c.match.any for c in search.call_args.kwargs["rbac_filter"].must}
        assert conditions["company"] == ["microsoft"] and conditions["fiscal_year"] == [2016, 2023]
        assert conditions["confidentiality"] == ["public"]
        assert any(e["stage"] == "search_filter" and e["fiscal_years"] == years for e in events)
    else:
        assert flow.calls["generator"] == 0
        search.assert_not_called()
        embed.assert_not_called()
        if case == "unavailable":
            assert "requested company or filing year" in response.json()["response"]
            assert not any(e.get("step") == "router" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_old_query_field_checkpoint_still_supports_review_and_new_questions(flow, monkeypatch):
    """Load a pre-rename pause with the new schema without rewriting stored history."""
    from typing import TypedDict
    from src.models.auth import User
    from src.models.state import RAGState

    old_fields = {key: value for key, value in RAGState.__annotations__.items() if key != "contextualized_query"}
    old_fields["sanitized_query"] = str
    old_state = TypedDict("LegacyRAGState", old_fields)
    with monkeypatch.context() as legacy:
        legacy.setattr(builder, "RAGState", old_state)
        old_graph = builder.build_graph(flow.graph.checkpointer)
    thread = str(uuid4())
    config = {"configurable": {"thread_id": thread}, "metadata": {
        "user_id": "finance", "role": "finance", "name": "finance", "department": "test", "hitl_enabled": True,
    }}
    scope = {"doc_types": ["10k", "board_report"], "confidentiality": ["public", "confidential"], "escalated": True}
    state = chat._build_initial_state("Earlier question", User(
        user_id="finance", name="finance", role="finance", department="test",
    ), access_scope=scope)
    state.pop("contextualized_query")
    state.update(sanitized_query="Earlier standalone question", generated_answer="The reported amount is $200K.",
                 relevant_chunks=[CHUNK], hallucination_status="grounded", hallucination_score=0.9)
    await old_graph.aupdate_state(config, state, as_node="hallucination_checker")
    await old_graph.ainvoke(None, config=config)
    paused = await old_graph.aget_state(config)
    assert any(task.interrupts for task in paused.tasks)

    approved = await flow.client.post(f"/v1/approvals/{thread}/approve", headers=headers("admin"), json={})
    assert approved.status_code == 200, approved.text
    assert "$200K" in approved.json()["response"]
    followup = await flow.client.post(f"/v1/conversations/{thread}/messages", headers=headers("finance"),
                                      json={"message": "lookup amount"})
    assert followup.status_code == 200, followup.text
    current = await flow.graph.aget_state(config)
    assert current.values["contextualized_query"] == "lookup amount"
    assert len(current.values["messages"]) == 4
    assert flow.calls["generator"] == 1  # Review did not call a model; only the new question generated an answer.


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
@pytest.mark.parametrize("decision", [None, "approve", "reject"])
async def test_twentieth_question_is_allowed_then_history_and_review_remain_available(flow, decision):
    from langchain_core.messages import AIMessage, HumanMessage
    from src.models.auth import User

    thread = str(uuid4())
    config = {"configurable": {"thread_id": thread}}
    seed = chat._build_initial_state("Earlier question", User(
        user_id="finance", name="finance", role="finance", department="test",
    ))
    seed["messages"] = [message for i in range(19) for message in (
        HumanMessage(content=f"Earlier question {i}"), AIMessage(content=f"Earlier answer {i}"),
    )]
    await flow.graph.aupdate_state(config, seed, as_node="response_formatter")
    question = (ESCALATE if decision else "") + "What is the reported amount?"
    response = await flow.client.post(f"/v1/conversations/{thread}/messages", headers=headers("finance"),
                                      json={"message": question})
    assert response.status_code == 200, response.text
    assert flow.calls["generator"] == 1

    if decision:
        assert response.json()["requires_approval"] is True
        reviewed = await flow.client.post(f"/v1/approvals/{thread}/{decision}", headers=headers("admin"),
                                          json={"reason": "Reviewed at the conversation limit"})
        assert reviewed.status_code == 200, reviewed.text

    for path in ("/v1/conversations/messages", "/v1/conversations/messages/stream",
                 f"/v1/conversations/{thread}/messages", f"/v1/conversations/{thread}/messages/stream"):
        rejected = await flow.client.post(path, headers=headers("finance"),
                                          json={"message": "One more question", "thread_id": thread})
        assert rejected.status_code == 409, rejected.text
        assert "20 questions" in rejected.json()["detail"]
    assert flow.calls["generator"] == 1
    state = await flow.graph.aget_state(config)
    assert sum(isinstance(m, HumanMessage) for m in state.values["messages"]) == 20
    history = await flow.client.get(f"/v1/conversations/{thread}", headers=headers("finance"))
    assert history.status_code == 200, history.text
    assert len(history.json()["messages"]) == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_research_then_lookup_does_not_reuse_findings(flow):
    c = flow.client
    first = (await c.post("/v1/conversations/messages", headers=headers("analyst"), json={"message": "research amount"})).json()
    thread = first["thread_id"]
    second = await c.post("/v1/conversations/messages", headers=headers("analyst"), json={"message": "lookup amount", "thread_id": thread})
    assert second.status_code == 200
    state = await flow.graph.aget_state({"configurable": {"thread_id": thread}})
    assert state.values["agent_synthesis"] is None
    history = await c.get(f"/v1/conversations/{thread}", headers=headers("analyst"))
    assert len(history.json()["messages"]) == 4
    assert first["request_id"] != second.json()["request_id"]
    assert [m["request_id"] for m in history.json()["messages"]] == [
        first["request_id"], first["request_id"], second.json()["request_id"], second.json()["request_id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_empty_research_does_not_generate_an_answer(flow):
    result = await flow.client.post("/v1/conversations/messages", headers=headers("analyst"), json={"message": "research missing"})
    assert result.status_code == 200
    assert flow.calls["generator"] == 0
    assert not result.json().get("confidence")


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_checker_outage_finishes_with_warning_instead_of_recursion_error(flow):
    flow.checker.with_structured_output.return_value.invoke.side_effect = RuntimeError("checker unavailable")
    response = await flow.client.post("/v1/conversations/messages", headers=headers("analyst"), json={"message": "lookup amount"})
    assert response.status_code == 200, response.text
    assert flow.calls["generator"] <= 3
    assert "could not be fully verified" in response.json()["response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["postgres"], indirect=True)
async def test_pending_approval_and_completed_history_survive_fresh_process(flow):
    """Resume with a new Python process and database connection, with no models."""
    import subprocess,sys,json
    initial=await flow.client.post('/v1/conversations/messages',headers=headers('finance'),json={'message':ESCALATE+'lookup amount'})
    assert initial.status_code==200
    thread=initial.json()['thread_id']
    code=r'''
import asyncio,json,os,sys
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from src.graph.builder import build_graph
from src.services.conversation_access import pending_approval
async def main():
    async with AsyncConnectionPool(os.environ['TEST_POSTGRES_URL'],open=False,
            kwargs={'autocommit':True,'prepare_threshold':0}) as pool:
        await pool.open()
        graph=build_graph(AsyncPostgresSaver(pool))
        cfg={'configurable':{'thread_id':sys.argv[1]},'metadata':{
            'user_id':'finance','role':'finance','name':'finance','department':'test','hitl_enabled':True}}
        state=await graph.aget_state(cfg)
        assert pending_approval(state) is not None
        result=await graph.ainvoke(Command(resume={'decision':'approved','decided_by':'admin',
            'decided_by_role':'admin','reason':'Restart verification'}),config=cfg)
        print('RESULT '+json.dumps({'response':result['final_response'],
            'sources':result['response_metadata']['sources']}))
asyncio.run(main())
'''
    child=subprocess.run([sys.executable,'-c',code,thread],capture_output=True,text=True,timeout=45)
    assert child.returncode==0,child.stderr[-2000:]
    result=json.loads(next(line[7:] for line in child.stdout.splitlines() if line.startswith('RESULT ')))
    assert '$200K' in result['response']
    assert result['sources'][0]['page']==8
    received=await flow.client.get(f'/v1/conversations/{thread}/result',headers=headers('finance'))
    assert received.status_code==200,received.text
    assert received.json()['status']=='approved'
    # Reading from another freshly constructed graph must retain the released messages.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    fresh=builder.build_graph(AsyncPostgresSaver(app.state.pool))
    stored=await fresh.aget_state({'configurable':{'thread_id':thread}})
    assert stored.values['human_decision']=='approved'
    assert len(stored.values['messages'])==2


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_failed_request_is_clear_and_same_conversation_can_recover(flow,monkeypatch):
    attempts=[]
    async def flaky(state):
        attempts.append(1)
        if len(attempts)==1:raise RuntimeError('Simulated provider outage')
        return {'generated_answer':'Recovered answer with source'}
    monkeypatch.setattr(builder,'generator_node',flaky)
    graph=builder.build_graph(app.state.checkpointer)
    monkeypatch.setattr(chat,'_get_graph',lambda **kwargs:graph)
    thread=str(uuid4())
    failed=await flow.client.post('/v1/conversations/messages',headers=headers('analyst'),json={'message':'lookup amount','thread_id':thread})
    assert failed.status_code==503
    again=await flow.client.post('/v1/conversations/messages',headers=headers('analyst'),json={'message':'lookup amount','thread_id':thread})
    assert again.status_code==200,again.text
    assert 'Recovered answer' in again.json()['response']


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_missing_approval_storage_blocks_finance_before_generation(flow,monkeypatch):
    monkeypatch.setattr(app.state,'checkpointer',None)
    url='/v1/conversations/messages'
    response=await flow.client.post(url,headers=headers('finance'),json={'message':ESCALATE+'lookup amount'})
    assert response.status_code==503
    assert flow.calls['generator']==0


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory", "postgres"], indirect=True)
async def test_path_based_followup_keeps_ownership_checks(flow):
    first = await flow.client.post("/v1/conversations/messages", headers=headers("analyst"),
                                   json={"message": "lookup amount"})
    thread = first.json()["thread_id"]
    path = f"/v1/conversations/{thread}/messages"
    forbidden = await flow.client.post(path, headers=headers("finance"), json={"message": ESCALATE + "lookup amount"})
    assert forbidden.status_code == 403
    response = await flow.client.post(path, headers=headers("analyst"), json={"message": "lookup amount"})
    assert response.status_code == 200, response.text
    assert response.json()["thread_id"] == thread


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_history_store_outage_never_starts_generation(flow,monkeypatch):
    from unittest.mock import AsyncMock
    flow.graph.aget_state=AsyncMock(side_effect=ConnectionError('Simulated database outage'))
    # Do not expose internal errors or start a paid model request.
    try:
        response=await flow.client.post('/v1/conversations/messages',headers=headers('analyst'),json={'message':'lookup amount','thread_id':'existing'})
    except ConnectionError:
        pytest.fail('Conversation storage outage escaped as an unhandled exception')
    assert response.status_code==503
    assert flow.calls['generator']==0


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_approval_database_outage_is_clear_and_does_not_release_answer(flow,monkeypatch):
    import psycopg
    from unittest.mock import AsyncMock
    initial=await flow.client.post('/v1/conversations/messages',headers=headers('finance'),json={'message':ESCALATE+'lookup amount'})
    thread=initial.json()['thread_id']
    with monkeypatch.context() as patcher:
        patcher.setattr(hitl,'get_thread_owner_role',AsyncMock(side_effect=psycopg.OperationalError('Simulated outage')))
        decision=await flow.client.post(f'/v1/approvals/{thread}/approve',headers=headers('admin'),json={})
        assert decision.status_code==503
        assert '$200K' not in decision.text
    pending=await flow.client.get(f'/v1/conversations/{thread}/result',headers=headers('finance'))
    assert pending.json()['status']=='pending'


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_search_outage_is_not_reported_as_missing_information(flow,monkeypatch):
    from src.graph.nodes.retrieval import retrieval_node
    from unittest.mock import MagicMock
    monkeypatch.setattr(builder,'retrieval_node',retrieval_node)
    monkeypatch.setattr('src.graph.nodes.retrieval.get_permissions',lambda role:ROLE_PERMISSIONS[role])
    monkeypatch.setattr('src.graph.nodes.retrieval.get_qdrant_client',MagicMock(side_effect=ConnectionError('Simulated search outage')))
    graph=builder.build_graph(app.state.checkpointer)
    monkeypatch.setattr(chat,'_get_graph',lambda **kwargs:graph)
    result=await flow.client.post('/v1/conversations/messages',headers=headers('analyst'),json={'message':'lookup amount'})
    assert result.status_code==503
    assert 'no relevant' not in result.text.lower()
    assert flow.calls['generator']==0


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
async def test_admin_only_and_escalation_does_not_carry_into_followup(flow):
    first = await flow.client.post('/v1/conversations/messages', headers=headers('hr'), json={'message': '/escalate doc_type=filings confidentiality=internal\nRevenue?'})
    assert first.status_code == 200, first.text
    thread = first.json()['thread_id']
    assert first.json()['requires_approval']
    pending = await flow.graph.aget_state({'configurable': {'thread_id': thread}})
    assert pending.values['access_scope'] == {
        'doc_types': ['expense_policy', '10k', '10q', '8k'],
        'confidentiality': ['public', 'internal'], 'escalated': True,
    }
    for role in ['c_level', 'finance', 'analyst', 'hr']:
        denied = await flow.client.get(f'/v1/approvals/{thread}', headers=headers(role))
        assert denied.status_code == 403
        denied = await flow.client.post(f'/v1/approvals/{thread}/approve', headers=headers(role), json={})
        assert denied.status_code == 403
    calls_before = flow.calls['generator']
    approved = await flow.client.post(f'/v1/approvals/{thread}/approve', headers=headers('admin'), json={})
    assert approved.status_code == 200, approved.text
    assert flow.calls['generator'] == calls_before
    normal = await flow.client.post(f'/v1/conversations/{thread}/messages', headers=headers('hr'), json={'message': 'A normal follow-up'})
    assert normal.status_code == 200
    state = await flow.graph.aget_state({'configurable': {'thread_id': thread}})
    assert state.values['access_scope'] is None
    assert not state.values['requires_human_approval']


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
@pytest.mark.parametrize("progress", [False, True])
async def test_complete_answer_preserves_redaction_usage_and_trace(flow, monkeypatch, progress):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from src.graph.nodes.generator import generator_node
    from pathlib import Path
    monkeypatch.setattr(chat, "detect_pii", lambda text: (text.replace("tejas@example.com", "<EMAIL_ADDRESS>"), []))
    model = FakeMessagesListChatModel(responses=[AIMessage(
        content="The reported amount is $200K. [Source: filing.pdf, Page 8]",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )])
    monkeypatch.setattr("src.graph.nodes.generator.LLMFactory.get_generator_llm", lambda: model)
    monkeypatch.setattr(builder, "generator_node", generator_node)
    graph = builder.build_graph(app.state.checkpointer)
    monkeypatch.setattr(chat, "_get_graph", lambda **kw: graph)
    result = await flow.client.post('/v1/conversations/messages' + ('/stream' if progress else ''), headers=headers('analyst'), json={
        'message': 'My email is tejas@example.com. What is the reported amount?',
    })
    assert result.status_code == 200, result.text
    if progress:
        import json
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
        assert not any(e['type'] == 'token' for e in events)
        body = {**events[-1], 'sanitized_message': events[0]['sanitized_message']}
        assert body['type'] == 'final'
    else:
        assert result.headers['content-type'].startswith('application/json')
        body = result.json()
    assert '$200K' in body['response']
    assert body['sources'][0]['page'] == 8
    assert body['tokens']['total'] == 15
    assert '<EMAIL_ADDRESS>' in body['sanitized_message']
    assert 'tejas@example.com' not in result.text
    report = Path(os.environ['REQUEST_TRACE_DIR']) / body['request_id'] / 'summary.md'
    assert 'answered' in report.read_text()
    state = await graph.aget_state({'configurable': {'thread_id': body['thread_id']}})
    assert state.values['messages'][0].content == body['sanitized_message']


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
@pytest.mark.parametrize("escalated", [False, True])
async def test_stage_events_never_send_partial_answers(flow, escalated):
    import json
    role = 'hr' if escalated else 'analyst'
    question = '/escalate doc_type=filings confidentiality=internal\nRevenue?' if escalated else 'Revenue?'
    result = await flow.client.post('/v1/conversations/messages/stream', headers=headers(role), json={'message': question})
    assert result.status_code == 200, result.text
    events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ')]
    assert events[0]['type'] == 'request_started'
    assert any(e['type'] == 'node_start' and e['node'] == 'retrieval' for e in events)
    assert any(e['type'] == 'node_start' and e['node'] == 'hallucination_checker' for e in events)
    assert not any(e['type'] == 'token' for e in events)
    assert all(e['request_id'] == events[0]['request_id'] for e in events)
    if escalated:
        assert events[-1]['type'] == 'pending_approval'
        assert '$200K' not in result.text
        assert not any(e['type'] == 'final' for e in events)
    else:
        assert events[-1]['type'] == 'final'
        assert '$200K' in events[-1]['response']
        assert events[-1]['sources'][0]['page'] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["memory"], indirect=True)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("node", ["metadata_extractor", "retrieval", "grader", "generator", "hallucination_checker"])
async def test_provider_limit_reaches_api_and_trace_without_releasing_answer(flow, monkeypatch, node, streaming):
    import json
    from pathlib import Path
    from langchain_core.messages import AIMessage
    from src.services.provider_errors import ProviderRateLimitError

    model = "voyage-finance-2" if node == "retrieval" else "openai/gpt-oss-20b"
    provider = "Voyage" if node == "retrieval" else "Groq"

    def limited(state):
        raise ProviderRateLimitError(stage=node, provider=provider, model=model, retry_after_seconds=17)

    function = {"metadata_extractor": "metadata_extractor_node", "retrieval": "retrieval_node",
                "grader": "grader_node", "generator": "generator_node", "hallucination_checker": "hallucination_checker_node"}[node]
    monkeypatch.setattr(builder, function, limited)
    graph = builder.build_graph(flow.graph.checkpointer)
    monkeypatch.setattr(chat, "_get_graph", lambda **kwargs: graph)
    thread = str(uuid4())
    path = "/v1/conversations/messages" + ("/stream" if streaming else "")
    result = await flow.client.post(path, headers=headers("analyst"), json={"message": "Revenue?", "thread_id": thread})
    if streaming:
        assert result.status_code == 200  # SSE was already open; error status travels in the event.
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
        last = events[-1]
        assert last["type"] == "error" and last["status"] == 429
        assert last["code"] == "provider_rate_limited" and last["node"] == node
        assert last["model"] == model and last["retry_after_seconds"] == 17
        assert not any(event["type"] in ("final", "pending_approval") for event in events)
        request_id = last["request_id"]
        assert request_id == events[0]["request_id"]
    else:
        assert result.status_code == 429
        assert node in result.json()["detail"] and model in result.json()["detail"]
        assert result.headers["Retry-After"] == "17"
        request_id = result.headers["X-Request-ID"]
    folder = Path(os.environ["REQUEST_TRACE_DIR"]) / request_id
    saved = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert any(event["stage"] == "provider_limit" and event["node"] == node and event["model"] == model for event in saved)
    assert saved[-1]["outcome"] == "failed"
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread}})
    assert not any(isinstance(message, AIMessage) for message in snapshot.values["messages"])
