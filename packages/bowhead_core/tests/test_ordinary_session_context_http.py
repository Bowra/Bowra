"""Existing-Session inputs survive a native post-helper question without replay."""

import json
import sqlite3
from pathlib import Path

import pytest
import respx
from bowhead_core.documents.package_io import OFFICE_MIMES
from bowhead_gateway.routes import sessions
from test_ordinary_lead_question_http import call, response
from test_ordinary_office_compute_question_http import (
    test_office_and_system_calculation_resume_without_repeating_effects as run_fresh_turn,
)
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import rows, settled
from test_reference_http_start import start_case as start_case
from test_reference_office_continuation import office_bytes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_office_sandbox_unused_invoke"], indirect=True),
]


@pytest.mark.parametrize(
    "pin_policy,answer_mode,boundary",
    [
        *[("retain", "custom", "parallel_" + case) for case in ("resume", "deleted", "confirmation_race", "later_pin", "missing_proof")],
        *[(policy, answer, "resume") for policy in ("retain", "unpin_visible", "unpin_all") for answer in ("skip", "candidate", "custom")],
        *[
            ("retain", "skip", boundary)
            for boundary in (
                "unpin_after",
                "recreated",
                "text",
                "bytes",
                "deleted",
                "metadata",
                "binding",
                "owner",
                "space",
                "incomplete",
                "confirmation",
                "confirmation_race",
                "output_origin",
                "no_current",
                "overlap",
                "no_collaboration",
                "later_pin",
                "bundle",
            )
        ],
    ],
)
async def test_existing_session_retains_original_context_through_question(http_start, monkeypatch, pin_policy, answer_mode, boundary):
    case, scope = http_start, http_start.fixture.scope
    parallel = boundary.startswith("parallel_")
    boundary = boundary.removeprefix("parallel_")
    existing_turn_ids = {row["id"] for row in rows(case, "turns")}
    await run_fresh_turn(case, monkeypatch, "skip", True, None)
    original_turn = next(row for row in rows(case, "turns") if row["id"] not in existing_turn_ids)
    session_id = original_turn["session_id"]
    store = case.app.state.session_store
    original_pins = await store.list_pinned_attachments(session_id=session_id)
    assert len(original_pins) == 2
    case.app.include_router(sessions.router, prefix="/api/v1/sessions")
    visible = await case.client.get(f"/api/v1/sessions/{session_id}/attachments")
    assert visible.status_code == 200, visible.text
    visible_ids = [item["id"] for item in visible.json()["items"]]
    assert len(visible_ids) == 1
    removed = original_pins if pin_policy == "unpin_all" else visible_ids if pin_policy == "unpin_visible" else []
    for artifact_id in removed:
        unpinned = await case.client.delete(f"/api/v1/sessions/{session_id}/attachments/{artifact_id}")
        assert unpinned.status_code == 204, unpinned.text
    case.client.headers.pop("Content-Type", None)
    uploaded = await case.client.post(
        "/api/v1/files/upload",
        headers={"X-Space-Id": scope.workspace_id},
        files={"file": ("second-profit.pptx", office_bytes("pptx"), OFFICE_MIMES[".pptx"])},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    case.client.headers["Content-Type"] = "application/json"
    artifacts = case.app.state.artifact_store
    old_upload_id = visible_ids[0]
    old_output_id = next(identity for identity in original_pins if identity != old_upload_id)
    if boundary == "no_collaboration":

        async def no_collaboration(*_args, **_kwargs):
            return None

        monkeypatch.setattr("bowhead_core.agent_collaboration_runtime.prepare_lead_collaboration", no_collaboration)
    if boundary == "overlap":
        pinned_current = await case.client.post(f"/api/v1/sessions/{session_id}/attachments", json={"artifact_id": attachment["file_id"]})
        assert pinned_current.status_code == 201, pinned_current.text
    if boundary in {"text", "bytes"}:
        case.client.headers.pop("Content-Type", None)
        text_upload = await case.client.post(
            "/api/v1/files/upload",
            headers={"X-Space-Id": scope.workspace_id},
            files={"file": ("prior-context.txt", b"Retained text context.", "text/plain")},
        )
        assert text_upload.status_code == 200, text_upload.text
        pinned_text = await case.client.post(f"/api/v1/sessions/{session_id}/attachments", json={"artifact_id": text_upload.json()["file_id"]})
        assert pinned_text.status_code == 201, pinned_text.text
        case.client.headers["Content-Type"] = "application/json"
        old_upload_id = text_upload.json()["file_id"]
    if boundary == "missing_proof":
        from bowhead_core.ordinary_lead_continuation import canonical_json

        from bowhead_core import ordinary_lead_runtime

        capture = ordinary_lead_runtime.capture_ordinary_lead_loop

        async def lose_input_proof(**kwargs):
            checkpoint = await capture(**kwargs)
            material = json.loads(checkpoint.material_json)
            assert material["input_context"] is not None
            material["input_context"] = None
            return checkpoint.model_copy(update={"material_json": canonical_json(material)})

        monkeypatch.setattr(ordinary_lead_runtime, "capture_ordinary_lead_loop", lose_input_proof)
    calls = {"lead": 0, "office": 0, "compute": 0}

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            calls["lead"] += 1
            index = calls["lead"]
            if parallel:
                if index > 1:
                    index += 1
                else:
                    message = call(
                        "helper-1",
                        "subagent_spawn",
                        {
                            "profile": case.fixture.case.args["reference_start"].profile_name,
                            "task": "Read the second attachment.",
                            "delegation_role": "helper",
                        },
                    )
                    message["tool_calls"].extend(
                        call(
                            "helper-2",
                            "subagent_spawn",
                            {
                                "profile": "compute",
                                "task": "Independently calculate 40 + 2.",
                                "delegation_role": "helper",
                            },
                        )["tool_calls"]
                    )
                    message["tool_calls"][1]["index"] = 1
                    return response(body, message, "tool_calls")
            if index <= 2:
                message = call(
                    f"helper-{index}",
                    "subagent_spawn",
                    {
                        "profile": case.fixture.case.args["reference_start"].profile_name if index == 1 else "compute",
                        "task": "Read the second attachment." if index == 1 else "Calculate 40 + 2.",
                        "delegation_role": "helper",
                    },
                )
            elif index == 3:
                message = call(
                    "question", "clarification_ask", {"questions": [{"question": "Which audience?", "type": "single_select", "options": ["A", "B"]}]}
                )
            else:
                if boundary == "later_pin":
                    assert "Later unrelated context." not in json.dumps(body) and "later.txt" not in json.dumps(body)
                return response(body, {"role": "assistant", "content": "The second synthetic profit is 42."}, "stop")
        elif "attachment_read" in names:
            calls["office"] += 1
            message = (
                call("read-office", "attachment_read", {"artifact_id": attachment["file_id"]})
                if calls["office"] == 1 and boundary != "no_current"
                else call("return-office", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Second profit is 42."}})
            )
        elif "skill_invoke" in names:
            calls["compute"] += 1
            message = (
                call("calculate", "skill_invoke", {"name": "python_sandbox", "script": "runner.py", "input": {"code": "print(40 + 2)"}})
                if calls["compute"] == 1
                else call("return-compute", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Calculated 42."}})
            )
        else:
            return response(body, {"role": "assistant", "content": "Synthetic inspection"}, "stop")
        return response(body, message, "tool_calls")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        sent = await case.client.post(
            "/api/v1/chat/send",
            json={
                "session_id": session_id,
                "workspace_id": scope.workspace_id,
                "message": "Read the second Office file, calculate its profit, then ask for an audience.",
                "attachments": [] if boundary == "no_current" else [attachment["file_id"]],
                "attachment_refs": []
                if boundary == "no_current"
                else [
                    {
                        "fileId": attachment["file_id"],
                        "filename": attachment["filename"],
                        "mime": attachment["mime"],
                        "size": attachment["size"],
                        "referenceKind": "upload",
                    }
                ],
            },
        )
        assert sent.status_code == 200, sent.text
        turn_id = sent.json()["turn_id"]
        await settled(turn_id)
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        executions = [row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id]
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        lead = next((row for row in executions if row["dispatch_key"] == "collaboration-lead:" + turn_id), None)
        helpers = [row for row in executions if row != lead]
        assert len(helpers) == 2 and all(row["status"] == "completed" for row in helpers)
        assert calls == {"lead": 2 if parallel else 3, "office": 1 if boundary == "no_current" else 2, "compute": 2}
        assert turn["status"] == "clarification_pending" and len(cards) == 1, (turn["status"], turn["error_kind"])
        assert (lead["status"] if lead else None) == (None if boundary == "no_collaboration" else "running")
        source = await store.get_chat_turn_user_source(turn_id=turn_id, user_id=scope.user_id, workspace_id=scope.workspace_id)
        assert source.attachment_ids == (() if boundary == "no_current" else (attachment["file_id"],))
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint

        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=cards[0]["request_id"]))
        assert checkpoint.contract == (
            "ordinary-lead-loop.v11" if parallel else "ordinary-lead-loop.v9" if pin_policy == "unpin_all" else "ordinary-lead-loop.v10"
        )
        artifact_ids = {row["id"] for row in rows(case, "artifacts")}
        answer = (
            {"skip": True}
            if answer_mode == "skip"
            else {
                "answers": [
                    {
                        "question": "Which audience?",
                        "answer": "B" if answer_mode == "candidate" else "Finance",
                        "source": answer_mode,
                        **({"option_index": 1} if answer_mode == "candidate" else {}),
                    }
                ]
            }
        )

        async def change_material():
            if boundary == "bytes":
                art = await artifacts.get(old_upload_id)
                content = Path(art.path).read_bytes()
                Path(art.path).write_bytes(content[:-1] + b"!")
            elif boundary == "deleted":
                await artifacts.soft_delete(old_upload_id, user_id=scope.user_id)
            else:
                with sqlite3.connect(case.path) as db:
                    if boundary in {"confirmation", "confirmation_race"}:
                        db.execute("DELETE FROM artifact_upload_confirmations WHERE artifact_id=?", (old_upload_id,))
                    elif boundary == "binding":
                        db.execute("DELETE FROM artifact_upload_bindings WHERE artifact_id=?", (old_upload_id,))
                    elif boundary == "output_origin":
                        db.execute("UPDATE artifacts SET created_by_turn=? WHERE id=?", ("t_unknown", old_output_id))
                    elif boundary == "bundle":
                        db.execute(
                            "INSERT INTO chat_attachment_bundle_members VALUES (?,?,?)",
                            (attachment["file_id"], old_upload_id, "2026-10-10T00:00:00Z"),
                        )
                    else:
                        column, value = {
                            "metadata": ("filename", "changed.pptx"),
                            "owner": ("created_by", "usr_other"),
                            "space": ("workspace_id", "ws_other"),
                            "incomplete": ("status", "pending_upload"),
                        }[boundary]
                        db.execute(f"UPDATE artifacts SET {column}=? WHERE id=?", (value, old_upload_id))

        success = boundary in {"resume", "unpin_after", "text", "recreated", "no_current", "overlap", "no_collaboration", "later_pin"}
        if boundary == "confirmation_race":
            from bowhead_core import ordinary_lead_runtime

            validate = ordinary_lead_runtime.validate_ordinary_retained_materials

            async def change_after_preflight(ctx, retained):
                await validate(ctx, retained)
                if getattr(ctx, "ordinary_lead_state", None) is not None:
                    await change_material()

            monkeypatch.setattr(ordinary_lead_runtime, "validate_ordinary_retained_materials", change_after_preflight)
        elif boundary == "unpin_after":
            for identity in original_pins:
                unpinned = await case.client.delete(f"/api/v1/sessions/{session_id}/attachments/{identity}")
                assert unpinned.status_code == 204, unpinned.text
        elif boundary == "later_pin":
            # A new pin after pause must not become part of the old checkpoint.
            case.client.headers.pop("Content-Type", None)
            upload_later = await case.client.post(
                "/api/v1/files/upload",
                headers={"X-Space-Id": scope.workspace_id},
                files={"file": ("later.txt", b"Later unrelated context.", "text/plain")},
            )
            assert upload_later.status_code == 200, upload_later.text
            later_id = upload_later.json()["file_id"]
            later = await case.client.post(f"/api/v1/sessions/{session_id}/attachments", json={"artifact_id": later_id})
            assert later.status_code == 201, later.text
            artifact_ids.add(later_id)
            assert later_id not in checkpoint.context_json and later_id not in checkpoint.material_json
            case.client.headers["Content-Type"] = "application/json"
        elif boundary == "recreated":
            from bowhead_core.clarification_store import ClarificationStore
            from bowhead_core.events import EventBusRegistry
            from bowhead_core.orchestrator import Orchestrator
            from bowhead_core.session_store import SessionStore
            from bowhead_core.subagent import SubagentRunner
            from bowhead_core.turn_log_store import OrderedTurnLogSink

            from bowhead_core import events

            old = case.app.state.orchestrator
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
            orch.dispatch_owner_id = "recreated-session-context-worker"
            orch.subagent_runner = SubagentRunner(router=old.router, tool_registry=old.tool_registry, team_subagent_store=old.user_store)
            orch.reference_skill_sources_factory = old.reference_skill_sources_factory
            orch.access_control_store = old.access_control_store
            orch.canvas_store = old.canvas_store
            orch.skill_catalog = old.skill_catalog
            orch.skill_instance_store = old.skill_instance_store
            case.app.state.orchestrator = orch
            case.app.state.session_store = orch.session_store
            case.app.state.clarification_store = orch.clarification_store
            monkeypatch.setattr(events, "_default_registry", orch.event_registry)
        elif not success and boundary != "missing_proof":
            await change_material()
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
        resolved = await case.client.post(f"/api/v1/clarifications/{cards[0]['request_id']}/resolve", json=answer)
        if not success:
            assert resolved.status_code == (200 if boundary == "confirmation_race" else 409), resolved.text
            if resolved.status_code == 200:
                await settled(turn_id)
            refused = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert refused["status"] == ("failed" if boundary == "confirmation_race" else "clarification_pending")
            if boundary == "confirmation_race":
                assert next(row for row in rows(case, "turn_agent_execution") if row["id"] == lead["id"])["status"] == "failed"
            assert calls == {"lead": 2 if parallel else 3, "office": 2, "compute": 2}
            return
        assert resolved.status_code == 200, resolved.text
        await settled(turn_id)
        completed = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert completed["status"] == "completed"
        if lead is not None:
            completed_lead = next(row for row in rows(case, "turn_agent_execution") if row["id"] == lead["id"])
            assert completed_lead["status"] == "completed"
        assert completed["execution_generation"] == 2 and calls == {
            "lead": 3 if parallel else 4,
            "office": 1 if boundary == "no_current" else 2,
            "compute": 2,
        }
        assert [
            row for row in rows(case, "turn_agent_execution") if row["turn_id"] == turn_id and (lead is None or row["id"] != lead["id"])
        ] == helpers
        assert {row["id"] for row in rows(case, "artifacts")} == artifact_ids
        assert await store.get_chat_turn_user_source(turn_id=turn_id, user_id=scope.user_id, workspace_id=scope.workspace_id) == source
        assert next(row for row in rows(case, "turns") if row["id"] == original_turn["id"]) == original_turn
