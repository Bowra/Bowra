"""Image references use the normal Lead and retain exact execution on pause."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.chat_references import InputDocument
from bowhead_core.media import image_gen_tool
from bowhead_core.media.config_loader import empty_media_registry
from bowhead_core.media.image_gen import GeneratedImage
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import chat, clarifications
from test_image_generate_tool import _registry
from test_reference_http_start import answer_response, capability_payload, enable_lead_writer, lead_has_dispatched, payload, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_skill_extension_runtime import tool_response

from bowhead_core import events

pytestmark = [pytest.mark.integration, pytest.mark.asyncio, pytest.mark.parametrize("start_case", ["team"], indirect=True)]


@pytest.fixture
async def image_case(http_start, monkeypatch):
    case = http_start
    enable_lead_writer(case, monkeypatch)
    register_clarification_tool(case.app.state.orchestrator.tool_registry)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_CAPABILITY", "1")
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "images"))
    await artifacts.initialize()
    case.app.state.artifact_store = case.app.state.orchestrator.artifact_store = artifacts
    canvas = CanvasStore(case.path)
    await canvas.initialize()
    case.app.state.canvas_store = case.app.state.orchestrator.canvas_store = canvas
    case.generated = []

    async def resolve(team_id):
        assert team_id == case.fixture.scope.team_id
        return _registry()

    async def generate(_client, **kwargs):
        case.generated.append(kwargs)
        return [GeneratedImage(image_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, mime="image/png", width=1, height=1, revised_prompt=None)]

    monkeypatch.setattr(image_gen_tool.ImageGenClient, "generate", generate)
    assert image_gen_tool.register_image_gen_tool(case.app.state.orchestrator.tool_registry, empty_media_registry(), team_media_resolver=resolve)
    return case


def image_payload(case, automatic):
    body = capability_payload(case, automatic_receiver=automatic)
    if case.fixture.mode == "admission_capability_skill":
        skill = next(node for node in payload(case)["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "skill")
        skill["parameters"] = {}
        body["input_document"]["nodes"].insert(1, skill)
        body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    return body


def question():
    return {
        "payload": {
            "error": {
                "kind": "needs_user_input",
                "message": "Choose the image style.",
                "clarification_payload": {"questions": [{"question": "Which style?", "type": "single_select", "options": ["Minimal", "Detailed"]}]},
            }
        }
    }


@pytest.mark.parametrize(
    "receiver_case,automatic,pause,outcome",
    [
        (receiver, automatic, pause, outcome)
        for receiver in ("admission_capability", "admission_capability_skill")
        for automatic, pause, outcome in (
            (False, False, "image"),
            (True, False, "image"),
            (False, True, "image"),
            (True, True, "image"),
            (False, False, "no_image"),
            (True, True, "revoked"),
            (False, True, "dismissed"),
            (True, "lead", "image"),
            (False, False, "effect_then_question"),
        )
    ]
    + [("admission_capability_skill", True, "read", "image"), ("admission_capability", False, "lead", "dismissed")],
    indirect=["receiver_case"],
)
async def test_image_reference_lead_preserves_execution_and_requires_a_real_deliverable(image_case, monkeypatch, automatic, pause, outcome):
    case = image_case
    lead_calls, primary_calls = [], []
    question_round = 2 if pause == "read" else 1
    primary_question = pause is True or pause == "read"

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if pause == "lead" and len(lead_calls) == 1:
                return tool_response(
                    "clarification_ask",
                    {"questions": question()["payload"]["error"]["clarification_payload"]["questions"]},
                    stream=body.get("stream", False),
                )
            if len(lead_calls) == (2 if pause == "lead" else 1):
                if pause == "lead":
                    assert "Minimal" in json.dumps(body)
                return tool_response(
                    "subagent_spawn",
                    {
                        "profile": case.fixture.case.args["reference_start"].profile_name,
                        "task": "Create a Minimal launch image." if pause == "lead" else "Create the requested launch image.",
                        "delegation_role": "primary",
                    },
                    stream=body.get("stream", False),
                )
        elif "image_generate" in names:
            primary_calls.append(body)
            if pause == "read" and len(primary_calls) == 1:
                return tool_response("skill_read", {"name": "method"}, stream=body.get("stream", False))
            if primary_question and len(primary_calls) == question_round or outcome == "effect_then_question" and len(primary_calls) == 2:
                return tool_response("subagent_return", question(), stream=body.get("stream", False))
            if len(primary_calls) == (question_round + 1 if primary_question else 1) and outcome != "no_image":
                return tool_response("image_generate", {"prompt": "A minimal launch illustration."}, stream=body.get("stream", False))
        return answer_response(request)

    def turn_rows(table, turn_id):
        return [row for row in rows(case, table) if row["turn_id"] == turn_id]

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=image_payload(case, automatic))
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        plan = turn_rows("chat_reference_plans", turn_id)[0]
        assert json.loads(plan["plan_json"])["orchestration_contract"] == "lead-delegation.v1"
        assert "requires an image_generate deliverable" in json.dumps(lead_calls[0])
        execution = turn_rows("turn_agent_execution", turn_id)[0]
        capability = turn_rows("turn_capability_execution", turn_id)[0]
        if pause:
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
            assert capability["status"] == ("selected" if pause == "lead" else "running")
            assert execution["status"] == ("admitted" if pause == "lead" else "running")
            assert not case.generated
            card = turn_rows("clarification_requests", turn_id)[0]
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_CAPABILITY", "0")
            if outcome == "dismissed":
                dismissed = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/dismiss")
                assert dismissed.status_code == 200 and dismissed.json()["dismissed"], dismissed.text
                assert (await case.fixture.case.store.get_turn(turn_id)).status == "cancelled"
                assert turn_rows("turn_capability_execution", turn_id)[0]["status"] == "cancelled"
                assert not case.generated
                return
            if outcome == "revoked":
                with sqlite3.connect(case.path) as db:
                    db.execute("UPDATE agent_assignment SET personal_state='disabled'")
            resolved = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resolve", json={"chosen_value": "Minimal"})
            if outcome == "revoked":
                assert resolved.status_code == 409, resolved.text
                assert len(primary_calls) == 1 and not case.generated
                return
            assert resolved.status_code == 200 and resolved.json()["action"] == "turn_resume", resolved.text
            await settled(turn_id)
            duplicate = await case.client.post(f"/api/v1/clarifications/{card['request_id']}/resume_turn")
            assert duplicate.status_code == 200, duplicate.text
            assert "Minimal" in json.dumps(primary_calls[question_round if primary_question else 0])
        final = turn_rows("turn_capability_execution", turn_id)
        assert len(final) == 1 and final[0]["id"] == capability["id"] and final[0]["attempt_generation"] == 1
        assert [row["id"] for row in turn_rows("turn_agent_execution", turn_id)] == [execution["id"]]
        assert len(lead_calls) >= (1 if primary_question else 2)
        if pause == "read":
            restored = primary_calls[question_round]["messages"]
            assert any(message["role"] == "tool" and "BODY_method" in message.get("content", "") for message in restored)
            assert len(primary_calls) == 4
        if outcome in {"no_image", "effect_then_question"}:
            assert final[0]["status"] == "failed"
            assert final[0]["failure_code"] == (
                "capability_output_contract_failed" if outcome == "no_image" else "reference_capability_execution_failed"
            )
            assert len(case.generated) == (0 if outcome == "no_image" else 1)
            assert not turn_rows("clarification_requests", turn_id)
        else:
            assert final[0]["status"] == "completed"
            assert all("requires an image_generate deliverable" in json.dumps(body) for body in primary_calls)
            assert len(case.generated) == 1
            artifacts = await case.app.state.artifact_store.list_deliverables_for_turn(turn_id)
            assert any(artifact.kind == "deliverable" and artifact.producer_kind == "image_generate" for artifact in artifacts)
            final_payloads = [row["payload_json"] for row in turn_rows("turn_events", turn_id) if row["event_type"] == "lead.final.payload"]
            assert any(artifact.id in saved for artifact in artifacts for saved in final_payloads)
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "completed"
            projected = await case.client.get(f"/api/v1/turns/{turn_id}/reference-evidence")
            assert projected.status_code == 200, projected.text
            reference = next(item for item in projected.json()["references"] if item["resource_kind"] == "capability")
            assert reference["outcome"] == "fulfilled" and reference["capability_execution"]["execution_id"] == capability["id"]
        evidence = turn_rows("chat_reference_evidence", turn_id)
        assert [row["kind"] for row in evidence] == ["delegate_started", "output_received"]
        assert all(row["resolution_seq"] == 2 for row in evidence)


@pytest.mark.parametrize("receiver_case", ["admission_capability"], indirect=True)
@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("lead_queue", [False, True], ids=["legacy", "lead"])
async def test_queued_image_keeps_accepted_contract_after_admission_switch_changes(image_case, monkeypatch, automatic, lead_queue):
    case = image_case
    primary_started, release_primary = asyncio.Event(), asyncio.Event()
    lead_calls = primary_calls = 0

    async def respond(request):
        nonlocal lead_calls, primary_calls
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls += 1
            if not lead_has_dispatched(body):
                return tool_response(
                    "subagent_spawn",
                    {
                        "profile": case.fixture.case.args["reference_start"].profile_name,
                        "task": "Handle the requested image task.",
                        "delegation_role": "primary",
                    },
                    stream=body.get("stream", False),
                )
        elif "image_generate" in names:
            primary_calls += 1
            if primary_calls == 1:
                primary_started.set()
                await asyncio.wait_for(release_primary.wait(), 120)
            elif primary_calls == 2:
                return tool_response("image_generate", {"prompt": "A minimal launch illustration."}, stream=body.get("stream", False))
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        parent = await case.client.post("/api/v1/chat/send", json=payload(case, skills=False))
        assert parent.status_code == 200, parent.text
        parent_id, session_id = parent.json()["turn_id"], parent.json()["session_id"]
        started = asyncio.create_task(primary_started.wait())
        try:
            parent_tasks = [task for task in chat._inflight_tasks if task.get_name().endswith(":" + parent_id)]
            assert parent_tasks
            await asyncio.wait([started, *parent_tasks], timeout=120, return_when=asyncio.FIRST_COMPLETED)
            assert primary_started.is_set(), await case.fixture.case.store.get_turn(parent_id)
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "1" if lead_queue else "0")
            body = image_payload(case, automatic)
            body.update(session_id=session_id, submission_kind="steer", expected_parent_turn_id=parent_id, expected_execution_generation=1)
            accepted = await case.client.post("/api/v1/chat/send", json=body)
            assert accepted.status_code == 200 and accepted.json()["status"] == "buffered", accepted.text
            monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0" if lead_queue else "1")
            if lead_queue:
                monkeypatch.setenv("FEATURE_CHAT_REFERENCE_CAPABILITY", "0")
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
        child = await case.fixture.case.store.get_turn(child_id)
        assert child.status == "completed", child
        plan = next(row for row in rows(case, "chat_reference_plans") if row["turn_id"] == child_id)
        assert json.loads(plan["plan_json"]).get("orchestration_contract") == ("lead-delegation.v1" if lead_queue else None)
        # The parent's selected Team Agent answer is delivered directly (5006e9348): one Lead call.
        # A queued Lead child spawns, and its generated image keeps ordinary Lead finalization.
        assert lead_calls == (3 if lead_queue else 1)
        assert primary_calls == 3 and len(case.generated) == 1
        capability = [row for row in rows(case, "turn_capability_execution") if row["turn_id"] == child_id]
        assert len(capability) == 1 and capability[0]["status"] == "completed"
