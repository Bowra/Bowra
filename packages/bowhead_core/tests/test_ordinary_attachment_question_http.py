"""Real Office upload and ordinary helper read survive the original question."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.documents.package_io import OFFICE_MIMES
from bowhead_core.ordinary_lead_checkpoint import OrdinaryLeadCheckpoint
from bowhead_core.ordinary_lead_continuation import (
    OrdinaryContinuationUnavailable,
    canonical_json,
    read_ordinary_card_in_tx,
    reopen_ordinary_checkpoint,
)
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import clarifications, files
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_reference_office_continuation import office_bytes

from bowhead_core import attachment_read, events, ordinary_lead_runtime
from bowhead_router import AgentBinding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_office_unused_sop"], indirect=True),
]


@pytest.mark.parametrize(
    "kind,boundary,helper_count",
    [
        ("docx", "resume", 1),
        ("pptx", "recreated", 2),
        ("docx", "unknown", 1),
        *[("docx", boundary, 1) for boundary in ("bytes", "deleted", "confirmation", "binding", "owner", "space", "confirmation_race")],
    ],
)
async def test_native_attachment_read_continues_without_reparsing(http_start, monkeypatch, kind, boundary, helper_count):
    case, scope = http_start, http_start.fixture.scope
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    case.app.include_router(files.router, prefix="/api/v1/files")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "ordinary-office"))
    await artifacts.initialize()
    orch.artifact_store = case.app.state.artifact_store = artifacts
    case.client.headers.pop("Content-Type", None)
    uploaded = await case.client.post(
        "/api/v1/files/upload",
        headers={"X-Space-Id": scope.workspace_id},
        files={"file": ("profit." + kind, office_bytes(kind), OFFICE_MIMES["." + kind])},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    artifact = await artifacts.get(attachment["file_id"])
    assert artifact is not None
    case.client.headers["Content-Type"] = "application/json"
    parses, lead_calls, helper_calls = [], [], []
    extract = attachment_read.extract_attachment_bounded

    async def parse(*args):
        parses.append(args[0].id)
        return await extract(*args)

    monkeypatch.setattr(attachment_read, "extract_attachment_bounded", parse)
    question = {"question": "Which audience?", "type": "free_text", "options": []}

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if index <= helper_count:
                message = call(
                    f"helper-{index}",
                    "subagent_spawn",
                    {
                        "profile": case.fixture.case.args["reference_start"].profile_name,
                        "task": "Read the attached Office document and report its text.",
                        "delegation_role": "helper",
                    },
                )
            elif index == helper_count + 1:
                message = call("question", "clarification_ask", {"questions": [question]})
            else:
                assert "Board members" in json.dumps(body)
                return response(body, {"role": "assistant", "content": "The retained profit is 42 for the board members."}, "stop")
            return response(body, message, "tool_calls")
        if "attachment_read" in names:
            helper_calls.append(body)
            if len(helper_calls) % 2:
                message = call("read-office", "attachment_read", {"artifact_id": artifact.id})
            else:
                assert "Original Office profit is 42." in json.dumps(body)
                if boundary == "unknown":
                    return httpx2.Response(503, json={"error": {"message": "Synthetic lost helper response"}})
                message = call(
                    "return-office", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Original Office profit is 42."}}
                )
            return response(body, message, "tool_calls")
        return response(body, {"role": "assistant", "content": "Office inspection"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": scope.workspace_id,
                "message": "Read the attached document, then ask which audience to explain it to.",
                "attachments": [artifact.id],
                "attachment_refs": [
                    {"fileId": artifact.id, "filename": artifact.filename, "mime": artifact.mime, "size": artifact.size, "referenceKind": "upload"}
                ],
            },
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1, [(row["status"], row["error"]) for row in rows(case, "turns") if row["id"] == turn_id]
        card_id = cards[0]["request_id"]
        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=card_id))
        assert checkpoint.contract == "ordinary-lead-loop.v9"
        if boundary == "resume":
            for mutation in ("legacy", "other_attachment", "missing_proof"):
                value = json.loads(checkpoint.encode())
                material = json.loads(value["material_json"])
                proof = material["helpers"][0]["tools"][0]
                if mutation == "legacy":
                    value["contract"] = "ordinary-lead-loop.v7"
                elif mutation == "other_attachment":
                    proof["attachment"]["artifact_id"] = "art_not_read"
                else:
                    del proof["attachment"]
                value["material_json"] = canonical_json(material)
                with pytest.raises(OrdinaryContinuationUnavailable):
                    OrdinaryLeadCheckpoint.decode(canonical_json(value).encode())
        assert len(checkpoint.helper_materials()) == helper_count
        for helper in checkpoint.helper_materials():
            assert helper["tools"][0]["kind"] == "attachment_read"
        assert parses == [artifact.id] * helper_count

        async def change_material():
            if boundary == "bytes":
                content = Path(artifact.path).read_bytes()
                Path(artifact.path).write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
            elif boundary == "deleted":
                await artifacts.soft_delete(artifact.id, user_id=scope.user_id)
            else:
                with sqlite3.connect(case.path) as db:
                    if boundary in {"confirmation", "confirmation_race"}:
                        db.execute("DELETE FROM artifact_upload_confirmations WHERE artifact_id=?", (artifact.id,))
                    elif boundary == "binding":
                        db.execute("DELETE FROM artifact_upload_bindings WHERE artifact_id=?", (artifact.id,))
                    elif boundary in {"owner", "space"}:
                        column = "created_by" if boundary == "owner" else "workspace_id"
                        db.execute(f"UPDATE artifacts SET {column}=? WHERE id=?", ("changed-owner-or-space", artifact.id))

        if boundary == "confirmation_race":
            validate = ordinary_lead_runtime.validate_ordinary_retained_materials

            async def change_after_preflight(ctx, retained):
                await validate(ctx, retained)
                if getattr(ctx, "ordinary_lead_state", None) is not None:
                    await change_material()

            monkeypatch.setattr(ordinary_lead_runtime, "validate_ordinary_retained_materials", change_after_preflight)
        elif boundary == "recreated":
            from bowhead_core.clarification_store import ClarificationStore
            from bowhead_core.events import EventBusRegistry
            from bowhead_core.orchestrator import Orchestrator
            from bowhead_core.session_store import SessionStore
            from bowhead_core.subagent import SubagentRunner
            from bowhead_core.turn_log_store import OrderedTurnLogSink

            old = orch
            orch = Orchestrator(
                router=old.router,
                datarc=None,
                session_store=SessionStore(case.path),
                tool_registry=old.tool_registry,
                event_registry=EventBusRegistry(default_log_sink=OrderedTurnLogSink(case.app.state.turn_log_store)),
                user_store=old.user_store,
                clarification_store=ClarificationStore(case.path),
                artifact_store=artifacts,
            )
            orch.dispatch_owner_id = "recreated-ordinary-office-worker"
            orch.subagent_runner = SubagentRunner(router=old.router, tool_registry=old.tool_registry, team_subagent_store=old.user_store)
            orch.reference_skill_sources_factory = old.reference_skill_sources_factory
            orch.access_control_store = case.app.state.access_control_store
            case.app.state.orchestrator = orch
            case.app.state.session_store = orch.session_store
            case.app.state.clarification_store = orch.clarification_store
            monkeypatch.setattr(events, "_default_registry", orch.event_registry)
        elif boundary not in {"resume", "unknown"}:
            await change_material()
        orch.artifact_store = ArtifactStore(str(case.path), artifacts.backend)
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
        answered = await case.client.post(
            f"/api/v1/clarifications/{card_id}/resolve",
            json={"answers": [{"question": question["question"], "source": "custom", "answer": "Board members"}]},
        )
        success = boundary in {"resume", "recreated", "unknown"}
        assert answered.status_code == (200 if success or boundary == "confirmation_race" else 409), answered.text
        if answered.status_code == 200:
            await settled(turn_id)
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert turn["status"] == ("completed" if success else "failed" if boundary == "confirmation_race" else "clarification_pending")
        assert len(lead_calls) == helper_count + 1 + int(success)
        assert len(helper_calls) == 2 * helper_count
        assert parses == [artifact.id] * helper_count
