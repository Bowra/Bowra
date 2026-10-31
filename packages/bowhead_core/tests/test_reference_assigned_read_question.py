"""Ordinary assigned Skill reads retain their original question and execution."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx2
import pytest
import respx
import test_reference_http_start as reference_http

http_start = reference_http.http_start
original_receiver_case = reference_http.receiver_case
start_case = reference_http.start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("original_receiver_case", ["admission"], indirect=True),
]


@pytest.fixture
async def receiver_case(original_receiver_case, baseline, monkeypatch):
    fixture = original_receiver_case
    if baseline == "document_plugin":
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.agent_behavior_materializer import default_agent_behavior_materializer
        from bowhead_core.agent_dependency_compiler import AgentDependencyCompiler, AgentDependencyContext
        from bowhead_core.subagent_authoring_catalog import SubagentAuthoringCatalogSnapshot, native_authoring_tools
        from bowhead_core.system_plugins import expand_builtin_plugins
        from test_agent_execution_binding import _insert_release

        from bowhead_core import agent_dependency_compiler
        from bowhead_skill import SkillCatalog
        from bowhead_skill.catalog import builtin_catalog_dir

        async with open_shared_ctx(fixture.case.fixture.db_path) as db:
            sequence = (await (await db.execute("SELECT max(release_sequence)+1 FROM team_subagent_release")).fetchone())[0]
        prompt = "Use the assigned document method, and ask for missing input."
        definition = {
            "tool_refs": [],
            "builtin_plugin_ids": ["builtin:document-authoring"],
            "skill_grants": [],
            "system_prompt": prompt,
            "model": "analyst",
            "max_iterations": 8,
            "output_schema": None,
        }
        resolved = expand_builtin_plugins(definition)
        root = builtin_catalog_dir()
        monkeypatch.setattr(agent_dependency_compiler, "builtin_catalog_dir", lambda: root)
        context = AgentDependencyContext(
            agent_id="osa_exact", team_id=fixture.scope.team_id, owner_user_id=fixture.case.fixture.owner_id, audience_mode="teamwide"
        )
        tool_catalog = SubagentAuthoringCatalogSnapshot(
            team_id=context.team_id,
            actor_user_id=context.owner_user_id,
            native_tools=native_authoring_tools(),
            team_tools=(),
            fingerprint="sha256:" + "1" * 64,
            dependency_guard=(),
            built_at=datetime.now(UTC),
        )
        compiled = await AgentDependencyCompiler(builtin_skills=SkillCatalog(roots=[root])).compile(
            resolved, context=context, tool_catalog=tool_catalog
        )
        assert compiled.publishable, compiled.reason_codes
        await _insert_release(
            fixture.case.fixture,
            sequence=sequence,
            prompt=prompt,
            definition_overrides=definition,
            capability_fingerprint=compiled.capability_fingerprint,
            dependency_fingerprint=compiled.dependency_fingerprint,
        )
        async with open_shared_ctx(fixture.case.fixture.db_path) as db:
            import aiosqlite

            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM team_subagent_revision WHERE id=?", (f"arv_exact_{sequence}",))).fetchone()
            materializer = default_agent_behavior_materializer()
            descriptor = materializer.compile_team_revision(
                revision_id=row["id"],
                definition=resolved,
                source_definition_hash=row["definition_hash"],
                dependency=compiled,
            )
            await db.execute("BEGIN IMMEDIATE")
            await materializer.persist_team_revision_conn(
                db,
                revision_id=row["id"],
                team_id=fixture.scope.team_id,
                subagent_id="osa_exact",
                descriptor=descriptor,
                created_at="2026-10-30T00:00:00+00:00",
            )
            await db.commit()
        fixture.question_revision = sequence
    return fixture


def model_reply(request, *, name=None, arguments=None, text="Quarter confirmed."):
    body = json.loads(request.content)
    message = {"role": "assistant", "content": "" if name else text}
    if name:
        message["tool_calls"] = [
            {
                "index": 0,
                "id": "call-" + name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    reason = "tool_calls" if name else "stop"
    usage = {"prompt_tokens": 11, "completion_tokens": 7}
    if not body.get("stream"):
        return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})
    frames = [
        {"choices": [{"delta": message, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage},
    ]
    return httpx2.Response(
        200,
        text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


@pytest.mark.parametrize("baseline", ["read_only", "document_plugin"])
async def test_assigned_baseline_read_uses_original_shared_question_without_replaying(http_start, monkeypatch, baseline, tmp_path):
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.documents.tool import register_document_tool
    from bowhead_gateway.routes import clarifications

    from bowhead_core import events

    case = http_start
    reference_http.enable_lead_writer(case, monkeypatch)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", case.app.state.orchestrator.event_registry)
    submitted = reference_http.payload(case, skills=False)
    artifacts = ArtifactStore(db_path=case.path, backend=LocalBackend(tmp_path / "pinned-files"))
    pinned = await artifacts.put_bytes(
        content=b"Uninspected synthetic template",
        filename="prior-template.pptx",
        mime="application/octet-stream",
        created_by=case.fixture.scope.user_id,
    )
    submitted["session_id"] = "s_pinned_question"
    await case.fixture.case.store.ensure_session(
        session_id=submitted["session_id"], workspace_id=case.fixture.scope.workspace_id, user_id=case.fixture.scope.user_id
    )
    await case.fixture.case.store.pin_session_attachments(session_id=submitted["session_id"], artifact_ids=[pinned.id])
    async with open_shared_ctx(case.path) as db:
        await db.execute("UPDATE session_attachments SET pinned_at='2026-01-01' WHERE artifact_id=?", (pinned.id,))
        await db.commit()
    case.app.state.artifact_store = case.app.state.orchestrator.artifact_store = artifacts
    register_document_tool(case.app.state.orchestrator.tool_registry, service=lambda: None)
    method = "method"
    if baseline == "document_plugin":
        from bowhead_core.skill_bridge import register_skill_tools
        from bowhead_core.skill_read_resolution import build_reference_skill_sources
        from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata

        from bowhead_skill import SkillCatalog, SkillRuntime
        from bowhead_skill.catalog import builtin_catalog_dir

        sequence = case.fixture.question_revision
        selected = next(node["resource"] for node in submitted["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")
        selected.update(version=f"arv_exact_{sequence}", release_id=f"arl_exact_{sequence}")
        root = builtin_catalog_dir()
        catalog = SkillCatalog(roots=[root])
        orchestrator = case.app.state.orchestrator
        register_skill_tools(orchestrator.tool_registry, catalog, SkillRuntime())
        orchestrator.reference_skill_sources_factory = lambda scope: build_reference_skill_sources(
            case.path, scope=scope, builtin_index=build_builtin_skill_metadata, builtin_root=root
        )
        method = "pptx-authoring"
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return model_reply(
                request,
                name="subagent_spawn",
                arguments={
                    "profile": case.fixture.case.args["reference_start"].profile_name,
                    "task": "Read the assigned method, then ask which quarter to use.",
                    "delegation_role": "primary",
                },
            )
        if len(calls) == 2:
            return model_reply(request, name="skill_read", arguments={"name": method})
        if len(calls) == 3:
            return model_reply(
                request,
                name="subagent_return",
                arguments={
                    "payload": {
                        "exec_summary": "Waiting for the requested quarter.",
                        "error": {
                            "kind": "needs_user_input",
                            "message": "The quarter is required.",
                            "clarification_payload": {"questions": [{"question": "Which quarter?", "type": "free_text", "options": []}]},
                        },
                    }
                },
            )
        return model_reply(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json=submitted)
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await reference_http.settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        reads = [
            row
            for row in reference_http.rows(case, "turn_events")
            if row["turn_id"] == turn_id and row["event_type"] == "tool_result" and json.loads(row["payload_json"]).get("tool_name") == "skill_read"
        ]
        assert len(reads) == 1
        read = json.loads(reads[0]["payload_json"])
        assert read["ok"] is True
        assert read["result_inline"]["read_contract"] == "skill-baseline-read.v1"
        assert turn.status == "clarification_pending", turn.error_kind
        cards = [row for row in reference_http.rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1
        execution_id = next(row["id"] for row in reference_http.rows(case, "turn_agent_execution") if row["turn_id"] == turn_id)
        later_pin = await artifacts.put_bytes(
            content=b"Later synthetic file", filename="later-template.pptx", mime="application/octet-stream", created_by=case.fixture.scope.user_id
        )
        await case.fixture.case.store.pin_session_attachments(session_id=submitted["session_id"], artifact_ids=[later_pin.id])
        resolved = await case.client.post(f"/api/v1/clarifications/{cards[0]['request_id']}/resolve", json={"chosen_value": "2026-Q3"})
        assert resolved.status_code == 200, resolved.text
        await reference_http.settled(turn_id)
    turn = await case.fixture.case.store.get_turn(turn_id)
    assert turn.status == "completed" and "Quarter confirmed" in turn.final_answer
    assert [row["id"] for row in reference_http.rows(case, "turn_agent_execution") if row["turn_id"] == turn_id] == [execution_id]
    reads = [
        row
        for row in reference_http.rows(case, "turn_events")
        if row["turn_id"] == turn_id and row["event_type"] == "tool_result" and json.loads(row["payload_json"]).get("tool_name") == "skill_read"
    ]
    assert len(reads) == 1
    assert json.loads(reads[0]["payload_json"])["result_inline"]["read_contract"] == "skill-baseline-read.v1"
    assert len(calls) == 4
    for call in calls[1:]:
        projections = [message for message in call["messages"] if "pinned_document_inputs" in str(message.get("content", ""))]
        assert bool(projections) == (baseline == "document_plugin")
        if projections:
            assert pinned.id in str(projections[0]["content"])
            assert later_pin.id not in str(projections[0]["content"])
