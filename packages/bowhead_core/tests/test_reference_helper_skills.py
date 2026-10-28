"""Native helper reads preserve its own baseline without selected extensions."""

import copy
import json
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.skill_reference_context import ReferenceHelperSkillScope
from bowhead_core.subagent import PROFILES, SubagentRunner
from bowhead_skill.runtime_scope import ExactSkillRuntimeScope
from test_reference_http_start import answer_response, enable_lead_writer, payload, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_skill_extension_runtime import tool_response
from test_skill_model_access import receiver
from test_system_reference_baseline import configure

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True),
]


@pytest.mark.parametrize("declared", [True, False])
async def test_helper_can_read_own_skill_without_inheriting_selected_method(http_start, monkeypatch, declared):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    _, profile = await configure(case.fixture, monkeypatch)
    monkeypatch.setitem(
        PROFILES,
        "compute",
        replace(
            profile,
            system_prompt="HELPER_BASELINE_ONLY",
            tool_whitelist=("skill_read", "subagent_return"),
            skill_runtime_grants=tuple(grant for grant in profile.skill_runtime_grants if grant.name == "unused") if declared else None,
            skill_runtime_scope=ExactSkillRuntimeScope(("unused",), ()) if declared else None,
        ),
    )
    monkeypatch.setattr("bowhead_core.assigned_skill_execution.builtin_catalog_dir", lambda: case.fixture.root)
    calls = {"lead": [], "helper": [], "primary": []}
    lead_evidence = []
    invocations, helper_contexts = [], []
    execute = SubagentRunner._execute_tool
    require_helper = ReferenceHelperSkillScope.require

    async def record_tool(self, *args, **kwargs):
        result = await execute(self, *args, **kwargs)
        invocations.append(result)
        return result

    async def check_helper(self, ctx):
        await require_helper(self, ctx)
        if not helper_contexts:
            helper_contexts.append((self, copy.copy(ctx)))
            for field, value in (
                ("user_id", "another_user"),
                ("reference_skill_required", True),
                ("reference_skill_access", object()),
                ("skill_runtime_grants", ()),
                ("reference_source_db_path", str(case.path) + ".other"),
                ("reference_source_db_path", None),
            ):
                changed = copy.copy(ctx)
                setattr(changed, field, value)
                with pytest.raises(ValueError):
                    await require_helper(self, changed)
            with receiver("another_helper"), pytest.raises(ValueError):
                await require_helper(self, ctx)
            changed_scope = replace(self, binding=replace(self.binding, policy_fingerprint="different_policy"))
            changed = copy.copy(ctx)
            changed.reference_helper_skill_scope = changed_scope
            with pytest.raises(ValueError):
                await require_helper(changed_scope, changed)

    monkeypatch.setattr(SubagentRunner, "_execute_tool", record_tool)
    monkeypatch.setattr(ReferenceHelperSkillScope, "require", check_helper)

    async def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        stream = body.get("stream", False)
        if "subagent_spawn" in names:
            calls["lead"].append(body)
            current_turn = rows(case, "chat_reference_plans")[0]["turn_id"]
            evidence = await case.client.get(f"/api/v1/turns/{current_turn}/reference-evidence")
            assert evidence.status_code == 200, evidence.text
            lead_evidence.append(evidence.json())
            if len(calls["lead"]) == 1:
                return tool_response(
                    "subagent_spawn",
                    {"profile": "compute", "agent_id": "system:compute", "task": "Read your own unused SOP.", "delegation_role": "helper"},
                    stream=stream,
                )
            if len(calls["lead"]) == 2:
                return tool_response(
                    "subagent_spawn",
                    {"agent_id": "osa_exact", "task": "Analyze profit using the selected method.", "delegation_role": "primary"},
                    stream=stream,
                )
            return answer_response(request)
        if "skill_read" not in names:
            return answer_response(request)
        if "HELPER_BASELINE_ONLY" in json.dumps(body):
            calls["helper"].append(body)
            if len(calls["helper"]) == 1:
                assert "Required selected method data" not in json.dumps(body)
                return tool_response("skill_read", {"name": "unused", "args": ["2026"]}, stream=stream)
            if len(calls["helper"]) == 2:
                denied_read = tool_response("skill_read", {"name": "method"}, stream=stream)
                return httpx2.Response(200, headers=denied_read.headers, text=denied_read.text.replace("call-skill_read", "call-denied-method"))
            return answer_response(request)
        calls["primary"].append(body)
        assert "Required selected method data" in json.dumps(body)
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=payload(case))
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
    assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
    assert {key: len(value) for key, value in calls.items()} == {"lead": 3, "helper": 3, "primary": 1}
    executions = [row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
    assert len(executions) == 2 and all(row["status"] == "completed" for row in executions)
    selected_execution = next(row["id"] for row in executions if row["agent_id"] == "osa_exact")
    assert {row["execution_id"] for row in rows(case, "chat_reference_model_contexts")} == {selected_execution}
    evidence = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
    assert evidence.status_code == 200, evidence.text
    facts = evidence.json()
    orchestration = facts["orchestration"]
    assert orchestration["contract"] == "lead-delegation.v1"
    assert orchestration["turn_status"] == "completed"
    assert orchestration["execution_generation"] == 1
    assert orchestration["model_receipt_scope"] == "reference_primary"
    assert orchestration["agent_executions"] == [
        {
            "execution_id": row["id"],
            "parent_execution_id": row["parent_execution_id"],
            "is_primary": row["id"] == selected_execution,
            "dispatch_source": row["dispatch_source"],
            "status": "completed",
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }
        for row in sorted(executions, key=lambda row: row["id"] != selected_execution)
    ]
    assert {row["execution_id"] for row in facts["model_invocations"]} == {selected_execution}
    assert facts["composition"]["execution_status"] == "completed"
    assert lead_evidence[-1]["composition"]["execution_status"] == "completed"
    assert lead_evidence[-1]["orchestration"]["turn_status"] == "running"
    assert not lead_evidence[0]["model_invocations"]
    for snapshot in [*lead_evidence, facts]:
        assert snapshot["selection_identity"] == facts["selection_identity"]
        assert snapshot["cost_attribution"]["status"] == "unavailable"
        assert snapshot["cost_attribution"]["amount_usd"] is None
        assert "orchestration_usage_unavailable" in snapshot["evidence_completeness"]["reason_codes"]
    assert lead_evidence[-1]["projection_etag"] != facts["projection_etag"]
    if not declared:
        assert "BASELINE_SOP_unused" not in json.dumps(calls["helper"])
        reads = [invocation for invocation in invocations if invocation.tool_name == "skill_read"]
        assert len(reads) == 2 and all(invocation.result["error_kind"] == "reference_skill_unavailable" for invocation in reads)
        assert not helper_contexts
        return
    assert "BASELINE_SOP_unused 2026" in json.dumps(calls["helper"][1]), [m for m in calls["helper"][1]["messages"] if m["role"] == "tool"]
    denied = next(message for message in calls["helper"][2]["messages"] if message.get("tool_call_id") == "call-denied-method")
    assert "isn't included in this Agent" in denied["content"]
    assert [invocation.arguments["name"] for invocation in invocations if invocation.tool_name == "skill_read"] == ["unused"]
    assert len(helper_contexts) == 1
    scope, ctx = helper_contexts[0]
    with receiver(scope.subagent_id), pytest.raises(ValueError):
        await require_helper(scope, ctx)
    store = case.fixture.case.store
    await store.ensure_session(session_id="s_ordinary", workspace_id=ctx.workspace_id, user_id=ctx.user_id)
    await store.record_turn(turn_id="t_ordinary", session_id="s_ordinary", user_text="ordinary")
    copied = copy.copy(ctx)
    copied.turn_id, copied.session_id = "t_ordinary", "s_ordinary"
    with receiver(scope.subagent_id), pytest.raises(ValueError):
        await copied.ensure_reference_skill_context()
    copied.reference_source_db_path, copied.session_store = None, None
    with receiver(scope.subagent_id), pytest.raises(ValueError):
        await copied.ensure_reference_skill_context()
