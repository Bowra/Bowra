"""Explicit system receivers use the shared Lead and original-card lifecycle."""

import json
import sqlite3
from dataclasses import replace

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import clarifications
from test_reference_http_start import answer_response, enable_lead_writer, payload, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_skill_extension_runtime import tool_response
from test_system_reference_baseline import configure

from bowhead_core import events

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission"], indirect=True),
]


async def system_body(case, monkeypatch, *, skills=True):
    document, profile = await configure(case.fixture, monkeypatch, methods=skills)
    monkeypatch.setitem(PROFILES, "compute", replace(profile, tool_whitelist=(*profile.tool_whitelist, "subagent_return")))
    revision = await case.fixture.case.runner.project_system_behavior_candidate(profile_name="compute", parent_ctx=case.fixture.case.ctx)
    body = payload(case, skills=skills)
    selected = next(node.resource for node in document.nodes if getattr(getattr(node, "resource", None), "kind", None) == "agent")
    selected = selected.model_copy(update={"version": revision.revision_ref})
    next(node for node in body["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")["resource"] = selected.model_dump(
        mode="json", exclude_none=True
    )
    return body, selected


@pytest.fixture
async def system_lead(http_start, monkeypatch, skills):
    case = http_start
    body, selected = await system_body(case, monkeypatch, skills=skills)
    enable_lead_writer(case, monkeypatch)
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    return case, body, selected


@pytest.mark.parametrize(
    "pause,skills",
    [(None, False), (None, True), ("lead", True), ("primary", False), ("primary", True), ("read", True), ("revoked", True), ("disabled", True)],
)
async def test_system_lead_retains_original_receiver_and_read_checkpoint(system_lead, monkeypatch, pause, skills):
    case, body, selected = system_lead
    lead_calls, primary_calls = [], []
    question = {"questions": [{"question": "Which period?", "type": "free_text", "options": []}]}
    read_first = pause in {"read", "revoked", "disabled"}

    def respond(request):
        content = json.loads(request.content)
        names = {tool["function"]["name"] for tool in content.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(content)
            if pause == "lead" and len(lead_calls) == 1:
                return tool_response("clarification_ask", question, stream=content.get("stream", False))
            if not primary_calls:
                return tool_response(
                    "subagent_spawn",
                    {"agent_id": selected.id, "task": "Analyze profit with the selected method.", "delegation_role": "primary"},
                    stream=content.get("stream", False),
                )
            return answer_response(request)
        if "skill_read" not in names:
            return answer_response(request)
        primary_calls.append(content)
        assert ("Required selected method data" if skills else "Reference task context") in json.dumps(content)
        if read_first and len(primary_calls) == 1:
            return tool_response("skill_read", {"name": "method"}, stream=content.get("stream", False))
        if pause in {"primary", "read", "revoked", "disabled"} and len(primary_calls) == 1 + int(read_first):
            return tool_response(
                "subagent_return",
                {"payload": {"error": {"kind": "needs_user_input", "message": "Choose a period.", "clarification_payload": question}}},
                stream=content.get("stream", False),
            )
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=body)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        executions = [row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
        assert len(executions) == 1
        execution = executions[0]
        assert (execution["agent_source_kind"], execution["agent_id"], execution["revision_ref"]) == ("system", selected.id, selected.version)
        plan = next(row for row in rows(case, "chat_reference_plans") if row["turn_id"] == turn_id)
        assert json.loads(plan["plan_json"])["orchestration_contract"] == "lead-delegation.v1"
        if pause:
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
            card = next(row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id)
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
            if pause == "revoked":
                await case.fixture.sources.instances.upsert(
                    user_id=case.fixture.scope.user_id,
                    name="method",
                    source_type="builtin",
                    source_path=str(case.fixture.root / "method"),
                    enabled=False,
                )
            elif pause == "disabled":
                from bowhead_core.builtin_subagent_override_store import BuiltinSubagentOverride, BuiltinSubagentOverrideStore

                await BuiltinSubagentOverrideStore(str(case.path)).upsert(
                    BuiltinSubagentOverride(team_id=case.fixture.scope.team_id, name="compute", enabled=False),
                    expected_version=None,
                    actor_user_id=case.fixture.scope.user_id,
                    audit_action="test.disable_receiver",
                )
            before = len(lead_calls) + len(primary_calls)
            resolved = await case.client.post(
                f"/api/v1/clarifications/{card['request_id']}/resolve",
                json={"chosen_values": ["FY2026"], "answers": [{"question": "Which period?", "source": "custom", "answer": "FY2026"}]},
            )
            if pause in {"revoked", "disabled"}:
                assert resolved.status_code == 409, resolved.text
                assert len(lead_calls) + len(primary_calls) == before
                return
            assert resolved.status_code == 200, resolved.text
            await settled(turn_id)
            repeated = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resume_turn")
            assert repeated.status_code == 200, repeated.text
            assert "FY2026" in json.dumps(primary_calls[-1] if pause != "lead" else lead_calls[1])
        assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
        assert len(lead_calls) == (3 if pause == "lead" else 1 if pause else 2)
        assert len(primary_calls) == (3 if read_first else 2 if pause == "primary" else 1)
        with sqlite3.connect(case.path) as db:
            assert db.execute("SELECT primary_execution_id FROM turn_agent_binding WHERE turn_id=?", (turn_id,)).fetchone() == (execution["id"],)
        before = len(lead_calls) + len(primary_calls)
        replay = await case.client.post("/api/v1/chat/send", json=body)
        assert replay.status_code == 200 and replay.json() == accepted.json()
        assert len(lead_calls) + len(primary_calls) == before
