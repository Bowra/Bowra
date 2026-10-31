"""MCP selections retain their exact receiver through normal Lead and safe pauses."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.reference_receiver_metadata import build_reference_receiver_metadata
from bowhead_core.subagent import PROFILES
from bowhead_core.tool_auth_registry import ToolAuthRegistry
from bowhead_gateway.routes import chat, clarifications
from bowhead_router.tool_names import to_wire_tool_name
from test_reference_http_start import answer_response, enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_reference_mcp_preparation import _document, _enable_mcp_agent
from test_skill_extension_runtime import tool_response

from bowhead_core import events, openapi_tool_source

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


@pytest.fixture
async def mcp_case(http_start, monkeypatch):
    return await make_mcp_case(http_start, monkeypatch)


async def make_mcp_case(case, monkeypatch, *, document_tool=False, sequence=3):
    enable_lead_writer(case, monkeypatch)
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    with sqlite3.connect(case.path) as db:
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=NULL WHERE singleton=1")
    _, _, agent, index, source, read, write, access = await _enable_mcp_agent(case.fixture.case, sequence=sequence, document_tool=document_tool)
    metadata = SimpleNamespace(current=lambda: index)
    case.app.state.reference_mcp_metadata = case.app.state.orchestrator.reference_mcp_metadata = metadata
    case.app.state.access_control_store = case.app.state.orchestrator.access_control_store = access
    case.app.state.orchestrator.tool_auth_registry = ToolAuthRegistry(case.app.state.orchestrator.user_store)
    receiver_index = await build_reference_receiver_metadata(case.path)
    case.app.state.reference_receiver_metadata = SimpleNamespace(current=lambda: receiver_index)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "1")
    calls = []

    def executor(team_id, _resolver, **_kwargs):
        async def execute(source_id, operation, arguments):
            assert team_id == case.fixture.scope.team_id
            calls.append((source_id, operation.operation_id, arguments))
            return {"ok": True, "status": 200, "data": json.dumps({"margin": 42})}

        return execute

    monkeypatch.setattr(openapi_tool_source, "make_http_executor", executor)
    document = _document(agent, (source.resource, "Finance source", {}), (read.resource, "Read margin", {"year": 2025}))
    body = {
        "workspace_id": case.fixture.scope.workspace_id,
        "message": document.display_text(),
        "input_document": document.model_dump(mode="json", exclude_none=True),
        "client_submission_id": str(uuid4()),
        "submission_kind": "start",
        "submission_contract_version": "chat-references.v1",
        "model_control_batch": "references",
    }
    return SimpleNamespace(case=case, body=body, agent=agent, source=source, read=read, write=write, calls=calls)


@pytest.mark.parametrize(
    "pause,with_file",
    [
        (None, False),
        ("lead", False),
        ("primary", False),
        ("primary", True),
        ("after_read", False),
        ("after_read", True),
        ("after_source_read", False),
    ],
)
async def test_mcp_lead_uses_exact_tool_and_resumes_original_execution(mcp_case, monkeypatch, pause, with_file):
    f = mcp_case
    case = f.case
    lead_calls, primary_calls = [], []
    read_pause = pause in {"after_read", "after_source_read"}
    if pause == "after_source_read":
        document = _document(f.agent, (f.source.resource, "Finance source", {}))
        f.body.update(message=document.display_text(), input_document=document.model_dump(mode="json", exclude_none=True))
    if with_file:
        from test_reference_prompt_http import _prompt_upload_body

        f.body = await _prompt_upload_body(f)

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if pause == "lead" and len(lead_calls) == 1:
                return tool_response(
                    "clarification_ask",
                    {"questions": [{"question": "Which scope?", "type": "free_text", "options": []}]},
                    stream=body.get("stream", False),
                )
            if not primary_calls:
                return tool_response(
                    "subagent_spawn",
                    {"agent_id": f.agent.id, "task": "Read the selected margin with its fixed parameters.", "delegation_role": "primary"},
                    stream=body.get("stream", False),
                )
            return answer_response(request)
        primary_calls.append(body)
        assert to_wire_tool_name(f.read.id) in names and to_wire_tool_name(f.write.id) not in names
        if (pause == "primary" and len(primary_calls) == 1) or (read_pause and len(primary_calls) == 2):
            return tool_response(
                "subagent_return",
                {
                    "payload": {
                        "error": {
                            "kind": "needs_user_input",
                            "message": "Choose scope.",
                            "clarification_payload": {"questions": [{"question": "Which scope?", "type": "free_text", "options": []}]},
                        }
                    }
                },
                stream=body.get("stream", False),
            )
        if not f.calls or (pause == "after_source_read" and len(primary_calls) == 3):
            return tool_response(to_wire_tool_name(f.read.id), {"year": 2025}, stream=body.get("stream", False))
        if pause and not read_pause:
            _assert_resumed_call_fences(case)
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        response = await case.client.post("/api/v1/chat/send", json=f.body)
        assert response.status_code == 200, response.text
        turn_id = response.json()["turn_id"]
        await settled(turn_id)
        with sqlite3.connect(case.path) as db:
            plan = json.loads(db.execute("SELECT plan_json FROM chat_reference_plans WHERE turn_id=?", (turn_id,)).fetchone()[0])
            execution_id = db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id=?", (turn_id,)).fetchone()[0]
        assert plan["orchestration_contract"] == "lead-delegation.v1"
        if pause:
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
            assert len(f.calls) == int(read_pause)
            with sqlite3.connect(case.path) as db:
                card_id = db.execute("SELECT request_id FROM clarification_requests WHERE turn_id=?", (turn_id,)).fetchone()[0]
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
            resolved = await case.client.post(
                f"/api/v1/clarifications/{card_id}/resolve",
                json={"chosen_values": ["All teams"], "answers": [{"question": "Which scope?", "source": "custom", "answer": "All teams"}]},
            )
            assert resolved.status_code == 200, resolved.text
            await settled(turn_id)
            duplicate = await case.client.post(f"/api/v1/clarifications/{card_id}/resume_turn")
            assert duplicate.status_code == 200, duplicate.text
            resumed_body = primary_calls[-1] if pause != "lead" else lead_calls[1]
            assert "All teams" in json.dumps(resumed_body)
            if read_pause:
                tool_results = [message for message in resumed_body["messages"] if message.get("role") == "tool"]
                assert any("42" in str(message.get("content")) for message in tool_results)
            if with_file:
                assert all("period,profit\\nFY2026,42" in json.dumps(body) for body in primary_calls)
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
        count = len(lead_calls) + len(primary_calls)
        replay = await case.client.post("/api/v1/chat/send", json=f.body)
        assert replay.status_code == 200 and replay.json() == response.json()
        assert len(lead_calls) + len(primary_calls) == count
    assert len(lead_calls) == (1 if pause == "primary" or read_pause else 2 if pause is None else 3)
    assert f.calls == [("ots_finance", "getMargin", {"year": 2025})]
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id=?", (turn_id,)).fetchone() == (execution_id,)
        assert db.execute("SELECT execution_id,created_generation,status FROM chat_reference_mcp_calls WHERE turn_id=?", (turn_id,)).fetchall() == [
            (execution_id, 2 if pause in {"primary", "lead"} else 1, "completed")
        ]
    evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
    assert evidence.status_code == 200
    kind = "mcp_source" if pause == "after_source_read" else "mcp_tool"
    assert next(item for item in evidence.json()["references"] if item["resource_kind"] == kind)["outcome"] == "fulfilled"


def _assert_resumed_call_fences(case):
    """A current Source call is legal; altered owner, generation or binding is not."""
    with sqlite3.connect(case.path) as db:
        db.row_factory = sqlite3.Row
        saved = dict(db.execute("SELECT * FROM chat_reference_mcp_calls ORDER BY call_seq DESC LIMIT 1").fetchone())
        composition = json.loads(
            db.execute(
                "SELECT composition_json FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (saved["turn_id"],)
            ).fetchone()[0]
        )
        source = next(item for item in composition["mcp_admissions"] if item["resource"]["kind"] == "mcp_source")
        saved.update(
            call_id=str(uuid4()),
            call_seq=saved["call_seq"] + 1,
            node_id=source["node_id"],
            required_tool=0,
            status="started",
            result_ok=None,
            result_hash=None,
            result_ciphertext=None,
            completed_at=None,
            failure_code=None,
        )
        sql = f"INSERT INTO chat_reference_mcp_calls ({','.join(saved)}) VALUES ({','.join('?' for _ in saved)})"
        for changes in (
            {},
            {"owner_id": "different-worker"},
            {"created_generation": 1},
            {"created_generation": 3},
            {"connection_binding_id": "different-connection"},
        ):
            db.execute("SAVEPOINT call_boundary")
            try:
                if changes:
                    with pytest.raises(sqlite3.IntegrityError, match="reference MCP call scope mismatch"):
                        db.execute(sql, tuple((saved | changes).values()))
                else:
                    db.execute(sql, tuple(saved.values()))
            finally:
                db.execute("ROLLBACK TO SAVEPOINT call_boundary")
                db.execute("RELEASE SAVEPOINT call_boundary")


@pytest.mark.parametrize(
    "pause,outcome",
    [(pause, outcome) for pause in ("lead", "primary", "after_read") for outcome in ("dismiss", "revoke")] + [("after_read", "purge")],
)
async def test_mcp_pause_rechecks_authority_and_supports_dismissal(mcp_case, pause, outcome):
    f, model_calls = mcp_case, []
    case = f.case

    def respond(request):
        body = json.loads(request.content)
        model_calls.append(body)
        lead = any(tool["function"]["name"] == "subagent_spawn" for tool in body.get("tools", []))
        if lead and pause != "lead":
            return tool_response(
                "subagent_spawn", {"agent_id": f.agent.id, "task": "Read margin.", "delegation_role": "primary"}, stream=body.get("stream", False)
            )
        if pause == "after_read" and not f.calls:
            return tool_response(to_wire_tool_name(f.read.id), {"year": 2025}, stream=body.get("stream", False))
        question = {"questions": [{"question": "Which scope?", "type": "free_text", "options": []}]}
        return tool_response(
            "clarification_ask" if lead else "subagent_return",
            question if lead else {"payload": {"error": {"kind": "needs_user_input", "message": "Choose scope.", "clarification_payload": question}}},
            stream=body.get("stream", False),
        )

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=f.body)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
        card = next(row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id)
        count = len(model_calls)
        if outcome == "dismiss":
            result = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/dismiss")
            assert result.status_code == 200, result.text
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "cancelled"
            assert {row["status"] for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id} == {"cancelled"}
        else:
            with sqlite3.connect(case.path) as db:
                if outcome == "purge":
                    db.execute("UPDATE chat_reference_mcp_calls SET retention_state='purged',result_ciphertext=NULL WHERE turn_id=?", (turn_id,))
                else:
                    db.execute("UPDATE agent_assignment SET personal_state='disabled'")
            result = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"chosen_values": ["All teams"]})
            assert result.status_code == 409, result.text
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
        assert len(model_calls) == count and len(f.calls) == int(pause == "after_read")


@pytest.mark.parametrize("outcome", ["source_unused", "required_unused"])
async def test_mcp_completion_obligations(mcp_case, outcome):
    f, lead_calls, primary_calls = mcp_case, [], []
    case = f.case
    if outcome == "source_unused":
        document = _document(f.agent, (f.source.resource, "Finance source", {}))
        f.body.update(message=document.display_text(), input_document=document.model_dump(mode="json", exclude_none=True))

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                return tool_response(
                    "subagent_spawn",
                    {"agent_id": f.agent.id, "task": "Read margin if needed.", "delegation_role": "primary"},
                    stream=body.get("stream", False),
                )
        else:
            primary_calls.append(body)
            assert to_wire_tool_name(f.read.id) in names and to_wire_tool_name(f.write.id) not in names
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=f.body)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    assert len(lead_calls) >= 1 and primary_calls
    assert (await case.fixture.case.store.get_turn(turn_id)).status == ("completed" if outcome == "source_unused" else "failed")
    assert not [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
    assert f.calls == []


@pytest.mark.parametrize("lead_queue", [False, True], ids=["legacy", "lead"])
async def test_queued_mcp_executes_accepted_contract_after_switch_change(mcp_case, monkeypatch, lead_queue):
    f, lead_calls, primary_calls = mcp_case, 0, 0
    case = f.case
    primary_started, release_primary = asyncio.Event(), asyncio.Event()

    async def respond(request):
        nonlocal lead_calls, primary_calls
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls += 1
            if lead_calls % 2:
                return tool_response(
                    "subagent_spawn", {"agent_id": f.agent.id, "task": "Read margin.", "delegation_role": "primary"}, stream=body.get("stream", False)
                )
        elif to_wire_tool_name(f.read.id) in names:
            primary_calls += 1
            if primary_calls == 1:
                primary_started.set()
                await asyncio.wait_for(release_primary.wait(), 120)
            if primary_calls % 2:
                return tool_response(to_wire_tool_name(f.read.id), {"year": 2025}, stream=body.get("stream", False))
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        parent = await case.client.post("/api/v1/chat/send", json=f.body)
        assert parent.status_code == 200, parent.text
        parent_id = parent.json()["turn_id"]
        started = asyncio.create_task(primary_started.wait())
        try:
            tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + parent_id)]
            assert tasks
            await asyncio.wait([started, *tasks], timeout=120, return_when=asyncio.FIRST_COMPLETED)
            assert primary_started.is_set(), await case.fixture.case.store.get_turn(parent_id)
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1" if lead_queue else "0")
            body = {
                **f.body,
                "client_submission_id": str(uuid4()),
                "session_id": parent.json()["session_id"],
                "submission_kind": "steer",
                "expected_parent_turn_id": parent_id,
                "expected_execution_generation": 1,
            }
            accepted = await case.client.post("/api/v1/chat/send", json=body)
            assert accepted.status_code == 200 and accepted.json()["status"] == "buffered", accepted.text
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0" if lead_queue else "1")
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "0")
            replay = await case.client.post("/api/v1/chat/send", json=body)
            assert replay.status_code == 200 and replay.json() == accepted.json(), replay.text
        finally:
            release_primary.set()
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        await settled(parent_id)
        relation = await case.fixture.case.store.get_chat_follow_up(parent_turn_id=parent_id)
        assert relation is not None
        child_id = relation[0]
        tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + child_id)]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 120)
        assert (await case.fixture.case.store.get_turn(child_id)).status == "completed"
        plan = next(row for row in rows(case, "chat_reference_plans") if row["turn_id"] == child_id)
        assert json.loads(plan["plan_json"]).get("orchestration_contract") == ("lead-delegation.v1" if lead_queue else None)
        assert lead_calls == (4 if lead_queue else 2) and primary_calls == 4
        assert f.calls == [("ots_finance", "getMargin", {"year": 2025})] * 2
        calls = [row for row in rows(case, "chat_reference_mcp_calls") if row["turn_id"] == child_id]
        assert len(calls) == 1 and calls[0]["status"] == "completed"


async def test_mcp_lead_admission_requires_compatible_schema_before_writing(mcp_case):
    f = mcp_case
    tables = ("turns", "turn_agent_execution", "chat_reference_plans")
    before = {table: rows(f.case, table) for table in tables}
    with sqlite3.connect(f.case.path) as db:
        db.execute("DROP TRIGGER trg_reference_lead_mcp_plan")
    result = await f.case.client.post("/api/v1/chat/send", json=f.body)
    assert result.status_code == 503, result.text
    assert result.json()["detail"]["error"]["code"] == "turn_store_unavailable"
    assert {table: rows(f.case, table) for table in tables} == before
    assert f.calls == []
