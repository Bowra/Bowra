"""Reference Plan through the production proposal and confirmation entry points."""

import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx2
import pytest
import respx
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.plan_store import PlanStore
from test_reference_http_start import answer_response, enable_lead_writer, payload, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
]


@pytest.mark.parametrize(
    "receiver_case,scenario",
    [
        ("admission_office_unused_sop", "success"),
        ("admission_office_optional_extension", "success"),
        ("admission_office_unused_sop", "agent_only"),
        ("admission_office_unused_sop", "skill_read"),
        *[("admission_office_unused_sop", scenario) for scenario in ("answer_delivery", "answer_legacy", "report_delivery")],
        ("admission_office_unused_invoke", "unsupported"),
        *[
            ("admission_office_unused_sop", scenario)
            for scenario in ("edit", "revoked", "revoked_between", "purged", "unknown", "cancelled", "cancelled_running", "missing_access", "fences")
        ],
    ],
    indirect=["receiver_case"],
)
async def test_reference_plan_native_confirmation(http_start, monkeypatch, scenario):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    monkeypatch.delenv("FEATURE_CHAT_REFERENCE_PLAN", raising=False)
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    monkeypatch.setenv("BOWHEAD_PLANNING_VERIFY", "0")
    monkeypatch.setenv("BOWHEAD_WRITE_REPORT", "0")
    monkeypatch.setenv("BOWHEAD_PSI_EAGER_COMPOSE", "1")
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    monkeypatch.setenv("BOWHEAD_PLANNING_V2", "0" if scenario == "answer_legacy" else "1")
    orchestrator = case.app.state.orchestrator
    store = PlanStore(str(case.path))
    from bowhead_core.canvas_store import CanvasStore

    orchestrator.canvas_store = CanvasStore(str(case.path))
    await orchestrator.canvas_store.initialize()
    orchestrator.plan_store = store
    case.app.state.plan_store = store
    orchestrator.multi_agent_registry = AgentRegistry.bootstrap()
    selected = case.fixture.case.args["reference_start"].profile_name
    from bowhead_gateway.routes import plans

    case.app.include_router(plans.router)
    calls = []
    primary_calls = []
    selected_body = "BODY_optional_method" if case.fixture.optional else "BODY_method"
    evidence_url = None
    if scenario == "missing_access":
        from bowhead_core.subagent import SubagentRunner

        run = SubagentRunner.run

        async def lose_access(self, **kwargs):
            if kwargs.get("required_model_input") is not None:
                kwargs["parent_ctx"].reference_skill_access = None
                kwargs["parent_ctx"].reference_skill_required = False
            return await run(self, **kwargs)

        monkeypatch.setattr(SubagentRunner, "run", lose_access)

    async def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if selected_body in json.dumps(body) or (scenario == "agent_only" and "Use the exact assigned financial methods." in json.dumps(body)):
            primary_calls.append(body)
            live = await case.client.get(evidence_url)
            assert live.status_code == 200, live.text
            assert any(row["status"] == "pending" for row in live.json()["model_invocations"]), live.json()
            if scenario == "fences":
                from uuid import uuid4

                from bowhead_core._db import open_shared_ctx
                from bowhead_core.chat_reference_plan import ReferencePlanStep
                from bowhead_core.chat_reference_storage import ReferenceScope

                current = next(row for row in rows(case, "chat_reference_model_invocations") if row["status"] == "pending")
                execution = next(row for row in rows(case, "turn_agent_execution") if row["id"] == current["execution_id"])
                step_id = execution["dispatch_key"].split(":")[1]
                owner = next(row for row in rows(case, "chat_turn_spawn_dispatch") if row["turn_id"] == execution["turn_id"])
                target = ReferencePlanStep(plan["id"], plan["current_rev"], step_id, execution["turn_id"], execution["id"])
                request_scope = ReferenceScope(**{**case.fixture.scope.model_dump(), "session_id": plan["session_id"], "policy_epoch": 1})
                async with open_shared_ctx(case.path) as db:
                    await db.execute("BEGIN IMMEDIATE")
                    await target.require(db, scope=request_scope, source_turn_id=proposal_id, owner_id=owner["owner_id"], generation=1)
                    with pytest.raises(ValueError, match="fence_lost"):
                        await replace(target, step_id="ps_other").require(
                            db, scope=request_scope, source_turn_id=proposal_id, owner_id=owner["owner_id"], generation=1
                        )
                    await db.execute("UPDATE chat_turn_spawn_dispatch SET owner_id='another-worker' WHERE turn_id=?", (execution["turn_id"],))
                    with pytest.raises(ValueError, match="fence_lost"):
                        await target.require(db, scope=request_scope, source_turn_id=proposal_id, owner_id=owner["owner_id"], generation=1)
                    await db.execute(
                        "UPDATE chat_turn_spawn_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE turn_id=?", (execution["turn_id"],)
                    )
                    context = next(row for row in rows(case, "chat_reference_model_contexts") if row["context_id"] == current["context_id"])
                    context["context_id"] = "ric_" + uuid4().hex
                    with pytest.raises(sqlite3.IntegrityError):
                        await db.execute(
                            "INSERT INTO chat_reference_model_contexts (" + ",".join(context) + ") VALUES (" + ",".join("?" for _ in context) + ")",
                            tuple(context.values()),
                        )
                    await db.rollback()
            if scenario == "unknown":
                raise httpx2.ReadTimeout("provider response was lost", request=request)
            if scenario == "revoked_between":
                with sqlite3.connect(case.path) as db:
                    db.execute("UPDATE agent_assignment SET personal_state='disabled'")
            if scenario == "cancelled_running":
                cancelled = await case.client.post(
                    f"/api/v1/plans/{plan['id']}/cancel", json={"workspace_id": plan["workspace_id"], "user_id": plan["user_id"]}
                )
                assert cancelled.status_code == 200, cancelled.text
            if scenario == "skill_read":
                if len(primary_calls) == 1:
                    from test_skill_extension_runtime import tool_response

                    return tool_response("skill_read", {"name": "method", "file": "method.md"}, stream=body.get("stream", False))
                if len(primary_calls) == 2:
                    assert any(message.get("role") == "tool" and "BODY_method" in message.get("content", "") for message in body["messages"])
        response = answer_response(request)
        if len(calls) == 1:
            data = response.json()
            data["choices"][0]["message"]["content"] = json.dumps(
                {
                    "deliverable": "report" if scenario == "report_delivery" else "answer",
                    "steps": [
                        {
                            "id": "first",
                            "agent_name": selected,
                            "objective": "Calculate the profit margin from the supplied numbers.",
                            "needs_corpus": False,
                            "needs_web": False,
                            "depends_on": [],
                        },
                        {
                            "id": "second",
                            "agent_name": selected,
                            "objective": "Explain the change in profit margin.",
                            "needs_corpus": False,
                            "needs_web": False,
                            "depends_on": ["first"],
                        },
                        {
                            "id": "helper",
                            "agent_name": "compute",
                            "objective": "Check publicly documented arithmetic definitions.",
                            "needs_corpus": False,
                            "needs_web": True,
                            "depends_on": ["second"],
                        },
                    ],
                }
            )
            return httpx2.Response(200, json=data)
        if scenario in {"answer_delivery", "answer_legacy", "report_delivery"} and any(
            tool.get("function", {}).get("name") == "subagent_return" for tool in body.get("tools", [])
        ):
            from test_skill_extension_runtime import tool_response

            return tool_response(
                "subagent_return",
                {
                    "payload": {
                        "delivery_surface": "report",
                        "title": "利润率分析",
                        "exec_summary": "北区利润率为百分之十，南区利润率为百分之十五。",
                        "kpis": [{"label": "北区利润率", "value": "10%"}],
                        "insights": [
                            {
                                "title": "比较结果",
                                "thesis": "南区盈利能力更强",
                                "narrative": "本次分析使用虚构数字，北区利润率为百分之十，南区利润率为百分之十五，南区高于北区。",
                                "evidence": [
                                    {"kind": "callout", "caption": "虚构输入", "callout_text": "北区营收一百、利润十；南区营收二百、利润三十。"}
                                ],
                            }
                        ],
                        "recommendations": [{"priority": "P2", "action": "在同一统计口径下比较。"}],
                    }
                },
                stream=body.get("stream", False),
            )
        return response

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        body = payload(case, skills=scenario != "agent_only")
        if case.fixture.optional:
            from bowhead_core.chat_references import InputDocument

            for node in body["input_document"]["nodes"]:
                if node.get("resource", {}).get("kind") == "skill":
                    node["resource"] = case.fixture.optional.model_dump(mode="json")
                    node["parameters"] = {}
                    break
            for node in body["input_document"]["nodes"]:
                if node.get("resource", {}).get("kind") == "skill":
                    node["parameters"] = {}
            body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
        if scenario in {"answer_delivery", "answer_legacy", "report_delivery"}:
            from bowhead_core.chat_references import InputDocument

            body["input_document"]["nodes"][0]["text"] = "请分两步比较北区和南区的利润率，先展示计划并等待确认。"
            body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
            body["user_profile"] = {"language": "en-US"}
        body["planning_mode"] = "planning"
        response = await case.client.post("/api/v1/chat/send", json=body)
        if scenario == "unsupported":
            assert response.status_code == 409, response.text
            assert not calls
            assert not rows(case, "plans")
            return
        assert response.status_code == 200, response.text
        proposal_id = response.json()["turn_id"]
        await settled(proposal_id)
        proposals = rows(case, "plans")
        assert len(proposals) == 1, (rows(case, "turns"), calls)
        plan = proposals[0]
        evidence_url = f"/api/v1/turns/{proposal_id}/reference-evidence"
        original = (await case.client.get(evidence_url)).json()
        assert original["plan"]["status"] == "proposed", original
        assert {item["outcome"] for item in original["references"]} == {"pending"}
        assert plan["status"] == "proposed", plan
        assert next(row for row in rows(case, "turns") if row["id"] == proposal_id)["status"] == "clarification_pending"
        monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PLAN", "0")
        if scenario == "edit":
            graph = await store.list_steps(plan["id"])
            invalid = [replace(step, included=False) if step.id == graph[0].id else step for step in graph]
            assert not await store.replace_steps(plan["id"], invalid, expected_rev=1)
            invalid = [replace(step, agent_name="compute") if step.id == graph[0].id else step for step in graph]
            assert not await store.replace_steps(plan["id"], invalid, expected_rev=1)
            edited = await case.client.patch(
                f"/api/v1/plans/{plan['id']}/steps",
                json={
                    "workspace_id": plan["workspace_id"],
                    "user_id": plan["user_id"],
                    "expected_rev": 1,
                    "steps": [
                        {
                            "id": step.id,
                            "agent": step.agent_name,
                            "objective": step.objective + " Check the units.",
                            "depends_on": list(step.depends_on),
                            "needs_web": step.needs_web,
                        }
                        for step in graph
                    ],
                },
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["current_rev"] == 2
            stale = await case.client.post(
                "/api/v1/chat/send",
                headers={"Content-Type": "application/json"},
                json={
                    "workspace_id": plan["workspace_id"],
                    "session_id": plan["session_id"],
                    "message": "Run it.",
                    "plan_execution_id": plan["id"],
                    "expected_plan_revision": 1,
                },
            )
            assert stale.status_code == 409, stale.text
            plan["current_rev"] = 2
        elif scenario == "revoked":
            with sqlite3.connect(case.path) as db:
                db.execute("UPDATE agent_assignment SET personal_state='disabled'")
        elif scenario == "purged":
            with sqlite3.connect(case.path) as db:
                db.execute("UPDATE chat_reference_plans SET retention_state='purged' WHERE turn_id=?", (proposal_id,))
        elif scenario == "cancelled":
            cancelled = await case.client.post(
                f"/api/v1/plans/{plan['id']}/cancel", json={"workspace_id": plan["workspace_id"], "user_id": plan["user_id"]}
            )
            assert cancelled.status_code == 200, cancelled.text
        response = await case.client.post(
            "/api/v1/chat/send",
            headers={"Content-Type": "application/json"},
            json={
                "workspace_id": case.fixture.scope.workspace_id,
                "session_id": plan["session_id"],
                "message": "Run the approved plan.",
                "plan_execution_id": plan["id"],
                "expected_plan_revision": plan["current_rev"],
            },
        )
        if scenario == "cancelled":
            assert response.status_code == 409, response.text
            assert not primary_calls
            assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == []
            assert (await case.client.get(evidence_url)).json()["plan"]["status"] == "cancelled"
            return
        assert response.status_code == 200, response.text
        execution_turn_id = response.json()["turn_id"]
        execution_bus = await orchestrator.event_registry.get_or_create(execution_turn_id, producer=False)
        try:
            await settled(response.json()["turn_id"])
        except asyncio.CancelledError as exc:
            assert scenario == "cancelled_running" and str(exc) == "durable_user_cancel"
        if scenario in {"revoked_between", "cancelled_running"}:
            current = await store.get_plan(plan["id"], user_id=plan["user_id"], workspace_id=plan["workspace_id"])
            assert current.status == ("blocked" if scenario == "revoked_between" else "cancelled")
            assert len(primary_calls) == 1
            facts = (await case.client.get(evidence_url)).json()
            assert {item["outcome"] for item in facts["references"]} == {current.status}
            return
        if scenario == "unknown":
            assert len(primary_calls) == 1
            assert rows(case, "chat_reference_model_invocations")[0]["status"] == "delivery_unknown"
            assert not await store.skip_blocked_step(plan["id"], (await store.list_steps(plan["id"]))[0].id, expected_rev=plan["current_rev"])
            retry = await case.client.post(
                "/api/v1/chat/send",
                headers={"Content-Type": "application/json"},
                json={
                    "workspace_id": plan["workspace_id"],
                    "session_id": plan["session_id"],
                    "message": "Retry the plan.",
                    "plan_execution_id": plan["id"],
                    "expected_plan_revision": plan["current_rev"],
                },
            )
            assert retry.status_code == 200, retry.text
            await settled(retry.json()["turn_id"])
            assert len(primary_calls) == 1
        if scenario in {"revoked", "purged", "missing_access", "unknown"}:
            failed = await store.get_plan(plan["id"], user_id=plan["user_id"], workspace_id=plan["workspace_id"])
            assert failed.status == "blocked"
            if scenario != "unknown":
                assert not primary_calls
                assert not rows(case, "chat_reference_model_invocations")
            return
    plan = await store.get_plan(plan["id"], user_id=case.fixture.scope.user_id, workspace_id=case.fixture.scope.workspace_id)
    assert plan.status == "completed", [(row["agent_name"], row["status"], row["error"], row["error_kind"]) for row in rows(case, "plan_steps")]
    # Production maintenance keeps revisiting the original proposal after its
    # separate execution Turn has finished. It must retain the Plan handoff.
    assert await case.fixture.case.store.recover_expired_chat_spawn_dispatches() == []
    receipts = [row for row in rows(case, "chat_reference_model_invocations") if row["turn_id"] == proposal_id]
    assert len(primary_calls) == len(receipts) == (3 if scenario == "skill_read" else 2)
    assert len({row["execution_id"] for row in receipts}) == 2
    assert {row["status"] for row in receipts} == {"completed"}
    assert "Calculate the profit margin" in json.dumps(primary_calls[0])
    assert "Explain the change" in json.dumps(primary_calls[-1])
    assert sum("Check publicly documented" in json.dumps(body) and selected_body not in json.dumps(body) for body in calls) >= 1
    root = next(row for row in rows(case, "turn_agent_execution") if row["turn_id"] == proposal_id and row["dispatch_key"] == "reference:primary")
    assert root["status"] == "admitted" and root["started_at"] is None
    facts = (await case.client.get(evidence_url)).json()
    assert facts["plan"]["status"] == "completed", facts
    if scenario == "success":
        assert len(facts["physical_calls"]["entries"]) == len(calls)
        assert all(row["state"] == "completed" for row in facts["physical_calls"]["entries"])
    assert len(facts["plan"]["steps"]) == 2
    assert {item["outcome"] for item in facts["references"]} == {"fulfilled"}
    assert facts == (await case.client.get(evidence_url)).json()
    assert facts["selection_identity"] == original["selection_identity"]
    charges = facts["billing"]["entries"]
    assert facts["billing"]["unit"] == "credits" and facts["billing"]["coverage"] == "partial"
    # Completion releases the active claim; immutable step executions keep the billing link.
    assert facts["plan"]["execution_turn_id"] is None
    assert execution_turn_id in {row["turn_id"] for row in charges}
    assert len({row["ledger_id"] for row in charges}) == len(charges)
    assert {row["turn_id"] for row in charges} <= {proposal_id, execution_turn_id}
    assert {e["execution_id"] for step in facts["plan"]["steps"] for e in step["executions"]} == {row["execution_id"] for row in receipts}
    for member in facts["composition"]["skill_members"]:
        assert member["stage"] == "context_assembled"
        assert set(member["context_ids"]) == {row["context_id"] for row in receipts}
    if case.fixture.optional:
        assert len(rows(case, "chat_reference_extension_checks")) == 2

    if scenario in {"answer_delivery", "answer_legacy", "report_delivery"}:
        execution_id = response.json()["turn_id"]
        turn = next(row for row in rows(case, "turns") if row["id"] == execution_id)
        events = [row for row in rows(case, "turn_events") if row["turn_id"] == execution_id]
        done = [json.loads(row["payload_json"]) for row in events if row["event_type"] == "done"]
        sections = await orchestrator.canvas_store.list_sections_appended_by_turn(execution_id)
        content = [section for section in sections if section.kind != "meta" and not section.superseded]
        assert turn["status"] == "completed", turn
        assert len(done) == 1
        final = [json.loads(row["payload_json"])["text"] for row in events if row["event_type"] == "final_answer"]
        assert final == [turn["final_answer"]]
        assert [event.payload["text"] for event in execution_bus.snapshot() if event.event_type == "final_answer"] == final
        assert [event.payload for event in execution_bus.snapshot() if event.event_type == "done"] == done
        assert execution_bus._pi_turn_state["final_answer"] == turn["final_answer"]
        assert "output_language=zh-CN" in json.dumps(calls[0])
        assert all("output_language=zh-CN" in json.dumps(call) for call in primary_calls)
        if scenario in {"answer_delivery", "answer_legacy"}:
            assert not content, [(section.kind, section.id) for section in content]
            assert done[0]["response_kind"] == "chat"
            assert "北区利润率为百分之十" in turn["final_answer"]
            assert "报告已生成" not in turn["final_answer"]
        else:
            assert content
            assert done[0]["response_kind"] == "canvas"
